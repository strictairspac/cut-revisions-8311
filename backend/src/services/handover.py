"""交班单：以开单那一刻的数据库快照留档，供逐页翻阅与整份下载。"""

import hashlib
from math import ceil

from fastapi import HTTPException
from sqlalchemy import select, text

from src.config.settings import engine
from src.constants.workflow import SheetStatus
from src.models.tables import cases, evidence, handover_items, handovers, runs
from src.services.workflow import found, log
from src.utils.encoding import canonical
from src.utils.time import utc_time

# 交班单留档的三个栏目：档案、执行进度、签署意见。
SECTIONS = {'cases': cases, 'runs': runs, 'evidence': evidence}
PAGE_SIZE = 10


def open_sheet(actor):
    """开一份交班单。

    单个 REPEATABLE READ 事务在开单时刻导出 PostgreSQL 快照，三个栏目按同一
    快照物化到 handover_items，再把状态从 building 原子翻转为 ready 后提交。
    快照只含开单前已提交（已入库）的行：先领到编号、开单后才提交入库的晚到
    记录对本单不可见，只能由之后另开的交班单查到。读取不阻塞各组提交与签署。
    """
    if engine.dialect.name != 'postgresql':
        raise HTTPException(422, '交班单留档需要 PostgreSQL 数据库')
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as conn:
        with conn.begin():
            snapshot_id = conn.execute(text('SELECT pg_export_snapshot()')).scalar_one()
            sections = {name: [dict(row) for row in conn.execute(select(table).order_by(table.c.id)).mappings()]
                        for name, table in SECTIONS.items()}
            counts = {name: len(rows) for name, rows in sections.items()}
            digest = hashlib.sha256(canonical({'counts': counts, 'sections': sections})).hexdigest()
            sheet_id = conn.execute(handovers.insert().values(
                status=SheetStatus.BUILDING, snapshot_id=snapshot_id, counts=counts, digest=digest,
                created_by=actor, created_at=utc_time())).inserted_primary_key[0]
            items = [{'sheet_id': sheet_id, 'section': name, 'ref': row['id'], 'payload': row}
                     for name, rows in sections.items() for row in rows]
            if items:
                conn.execute(handover_items.insert(), items)
            conn.execute(handovers.update().where(handovers.c.id == sheet_id)
                         .values(status=SheetStatus.READY, finished_at=utc_time()))
            log(conn, actor, 'open_handover', sheet_id)
            return found(conn, handovers, sheet_id)


def ready_sheet(conn, identity):
    sheet = found(conn, handovers, identity)
    if sheet['status'] != SheetStatus.READY:
        raise HTTPException(409, '交班单尚未生成完整，暂时不能打开')
    return sheet


def detail(identity):
    with engine.connect() as conn:
        return ready_sheet(conn, identity)


def page(identity, section, number):
    """逐页翻阅：只读物化后的留档内容，中断后恢复仍看到开单时的内容。"""
    if section not in SECTIONS:
        raise HTTPException(404, '交班单没有该栏目')
    number = max(number, 1)
    with engine.connect() as conn:
        sheet = ready_sheet(conn, identity)
        total = sheet['counts'][section]
        pages = max(ceil(total / PAGE_SIZE), 1)
        number = min(number, pages)
        rows = conn.execute(select(handover_items.c.payload)
                            .where(handover_items.c.sheet_id == identity,
                                   handover_items.c.section == section)
                            .order_by(handover_items.c.ref)
                            .limit(PAGE_SIZE).offset((number - 1) * PAGE_SIZE)).scalars().all()
    return {'sheet': sheet, 'section': section, 'page': number, 'pages': pages,
            'page_size': PAGE_SIZE, 'total': total, 'rows': rows}


def content(identity):
    """整份内容：与逐页翻阅同一份留档，保证页面所见与下载对得上。"""
    with engine.connect() as conn:
        sheet = ready_sheet(conn, identity)
        items = conn.execute(select(handover_items.c.section, handover_items.c.payload)
                             .where(handover_items.c.sheet_id == identity)
                             .order_by(handover_items.c.section, handover_items.c.ref)).all()
    sections = {name: [] for name in SECTIONS}
    for section, payload in items:
        sections[section].append(payload)
    return {'sheet': {key: sheet[key] for key in
                      ('id', 'status', 'snapshot_id', 'created_by', 'created_at', 'finished_at')},
            'counts': sheet['counts'], 'sections': sections, 'digest': sheet['digest']}
