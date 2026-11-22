"""交班单快照语义的端到端校验。

对着运行中的后端与 PostgreSQL 验证：
1. 交班单留住的正是点开单那一刻已入库的档案、执行进度和签署意见；
2. 开单后入库的记录不混入该单，另开一份才能查到；
3. 先领到编号、开单后才提交入库的晚到记录同样不混入（快照按提交可见性，
   不是按编号截断）；
4. 开单期间各组提交、复核员签署照常进行，且不影响已开的单；
5. 交班单生成完整才可打开（building 状态一律 409）；
6. 翻页中断后恢复仍看到开单时的内容；
7. 逐页拼接与整份下载完全一致，校验码可独立重算对上。

默认连接 docker compose 映射的本机端口，可用环境变量覆盖：
VERIFY_API   后端地址，默认 http://127.0.0.1:19292
VERIFY_DB    数据库 DSN，默认 postgresql://app:local-db-demo@127.0.0.1:17792/workflow
VERIFY_PASSWORD  演示账号密码，默认 Demo-Walkthrough-2026
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg

API = os.getenv('VERIFY_API', 'http://127.0.0.1:19292')
DSN = os.getenv('VERIFY_DB', 'postgresql://app:local-db-demo@127.0.0.1:17792/workflow')
PASSWORD = os.getenv('VERIFY_PASSWORD', 'Demo-Walkthrough-2026')

FAILURES = []


def check(label, condition, detail=''):
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f'  ({detail})' if detail and not condition else ''))
    if not condition:
        FAILURES.append(label)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True,
                      allow_nan=False).encode('utf-8')


def call(method, path, token=None, data=None, expect=200):
    request = urllib.request.Request(API + path, method=method)
    if token:
        request.add_header('Authorization', f'Bearer {token}')
    body = None
    if data is not None:
        request.add_header('Content-Type', 'application/json')
        body = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(request, body) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    if status != expect:
        raise AssertionError(f'{method} {path} 返回 {status}，期望 {expect}: {payload[:200]!r}')
    if method == 'GET' and path.endswith('/download'):
        return payload
    return json.loads(payload) if payload else None


def login(username):
    return call('POST', '/login', data={'username': username, 'password': PASSWORD})['token']


def make_case(token, title, payload):
    return call('POST', '/cases', token, {'title': title, 'payload': payload}, expect=201)


def run_until_complete(token, case_id):
    run = call('POST', f'/cases/{case_id}/runs', token, expect=202)
    for _ in range(120):
        time.sleep(0.5)
        row = next((x for x in call('GET', '/runs', token) if x['id'] == run['id']), None)
        if row and row['status'] in ('complete', 'failed'):
            assert row['status'] == 'complete', f"执行失败: {row['error']}"
            return row
    raise AssertionError('执行超时未完成')


def sheet_sections(sheet_id, token):
    """逐页取回三个栏目，模拟翻页。"""
    collected = {}
    for section in ('cases', 'runs', 'evidence'):
        rows, number = [], 1
        while True:
            page = call('GET', f'/handovers/{sheet_id}/pages/{section}?page={number}', token)
            assert page['page'] == number, f"第 {number} 页请求被改写到 {page['page']}"
            rows.extend(page['rows'])
            if number >= page['pages']:
                break
            number += 1
        assert len(rows) == page['total'] == page['sheet']['counts'][section]
        collected[section] = rows
    return collected


def main():
    operator, reviewer, viewer = login('operator'), login('reviewer'), login('viewer')
    sample = call('GET', '/spec', operator)['sample']

    # 权限：只有经办员（制片入口）能开单，三种角色都能读。
    call('POST', '/handovers', viewer, expect=403)
    call('POST', '/handovers', reviewer, expect=403)
    check('仅经办员可开交班单，复核员与观察员开单被拒', True)

    # 1) 基线：开单前入库的档案、执行进度、签署意见都留在单里。
    case_a = make_case(operator, '交班单基线镜头', sample)
    run_a = run_until_complete(operator, case_a['id'])
    evidence_a = next(x for x in call('GET', '/evidence', operator) if x['run_id'] == run_a['id'])
    call('POST', f"/evidence/{evidence_a['id']}/review", reviewer, {'note': '基线签署意见'})
    sheet1 = call('POST', '/handovers', operator, expect=201)
    check('开单返回生成完整的交班单', sheet1['status'] == 'ready' and sheet1['finished_at'])
    one = sheet_sections(sheet1['id'], viewer)
    check('开单时刻已入库的档案留在单里', any(x['id'] == case_a['id'] for x in one['cases']))
    check('开单时刻已入库的执行进度留在单里',
          any(x['id'] == run_a['id'] and x['status'] == 'complete' for x in one['runs']))
    signed = next((x for x in one['evidence'] if x['id'] == evidence_a['id']), None)
    check('开单时刻已入库的签署意见留在单里',
          signed is not None and signed['reviewer'] == 'reviewer' and signed['review_note'] == '基线签署意见')

    # 2) 开单后入库的记录不混入该单，另开一份才能查到。
    case_b = make_case(operator, '开单后提交的镜头', sample)
    check('开单后入库的档案不进已开的单', all(x['id'] != case_b['id'] for x in one['cases']))
    sheet2 = call('POST', '/handovers', operator, expect=201)
    two = sheet_sections(sheet2['id'], viewer)
    check('另开一份才能查到开单后入库的档案', any(x['id'] == case_b['id'] for x in two['cases']))

    # 3) 先领到编号、开单后才入库的晚到记录，另开一份才能查到。
    late = psycopg.connect(DSN)
    late.execute('BEGIN')
    late_id = late.execute(
        "INSERT INTO business_cases (slug, title, payload, author, created_at)"
        " VALUES (%s, '晚到补录镜头', '{}'::json, 'late-writer', now()::text) RETURNING id",
        (f'late-tx-{uuid.uuid4().hex}',)
    ).fetchone()[0]
    case_c = make_case(operator, '正常提交的镜头', sample)  # 晚到记录先领编号，正常记录先入库
    assert late_id < case_c['id'], '晚到记录应先领到更小的编号'
    sheet3 = call('POST', '/handovers', operator, expect=201)
    late.commit()
    three = sheet_sections(sheet3['id'], viewer)
    three_ids = [x['id'] for x in three['cases']]
    check('晚到记录（先领编号、开单后才入库）不进已开的单',
          late_id not in three_ids and case_c['id'] in three_ids,
          f'晚到编号 {late_id}，单内编号 {three_ids}')
    check('该单已包含编号更大的记录，排除不是按编号截断', max(three_ids) > late_id)
    sheet4 = call('POST', '/handovers', operator, expect=201)
    four = sheet_sections(sheet4['id'], viewer)
    check('晚到记录由另开的一份查到', any(x['id'] == late_id for x in four['cases']))

    # 4) 开单期间复核员照常签署，已开的单保持开单时的签署状态。
    case_d = make_case(operator, '待签署的镜头', sample)
    run_d = run_until_complete(operator, case_d['id'])
    evidence_d = next(x for x in call('GET', '/evidence', operator) if x['run_id'] == run_d['id'])
    sheet5 = call('POST', '/handovers', operator, expect=201)
    call('POST', f"/evidence/{evidence_d['id']}/review", reviewer, {'note': '开单后照常签署'})
    five = sheet_sections(sheet5['id'], viewer)
    kept = next(x for x in five['evidence'] if x['id'] == evidence_d['id'])
    check('已开的单保持开单时的签署状态', kept['reviewed_at'] is None and kept['reviewer'] is None)
    sheet6 = call('POST', '/handovers', operator, expect=201)
    six = sheet_sections(sheet6['id'], viewer)
    fresh = next(x for x in six['evidence'] if x['id'] == evidence_d['id'])
    check('另开一份查到开单后的签署意见', fresh['reviewer'] == 'reviewer' and fresh['review_note'] == '开单后照常签署')

    # 5) 开单期间各组同时提交不被阻塞，开出的每份单都完整自洽。
    with ThreadPoolExecutor(max_workers=12) as pool:
        bursts = [pool.submit(make_case, operator, f'并发镜头 {i}', sample) for i in range(10)]
        bursts += [pool.submit(call, 'POST', '/handovers', operator, None, 201) for _ in range(5)]
        opened = [f.result() for f in bursts]
    check('开单期间各组同时提交与开单全部成功', len(opened) == 15)
    for sheet in (x for x in opened if 'counts' in x):
        body = json.loads(call('GET', f"/handovers/{sheet['id']}/download", viewer))
        check(f"并发出单 #{sheet['id']} 完整自洽",
              body['counts'] == {k: len(v) for k, v in body['sections'].items()}
              and body['digest'] == hashlib.sha256(canonical(
                  {'counts': body['counts'], 'sections': body['sections']})).hexdigest())

    # 6) 逐页拼接与整份下载对得上，校验码可独立重算。
    target = call('POST', '/handovers', operator, expect=201)['id']
    paged = sheet_sections(target, viewer)
    raw = call('GET', f'/handovers/{target}/download', viewer)
    body = json.loads(raw)
    check('下载整份与逐页所见的三个栏目完全一致',
          all(body['sections'][name] == paged[name] for name in ('cases', 'runs', 'evidence')))
    check('整份统计与逐页合计一致',
          body['counts'] == {name: len(rows) for name, rows in paged.items()})
    digest = hashlib.sha256(canonical({'counts': body['counts'], 'sections': body['sections']})).hexdigest()
    meta = call('GET', f'/handovers/{target}', viewer)
    check('校验码与页面展示、整份内容一致', digest == body['digest'] == meta['digest'])

    # 7) 翻页中断后恢复，仍看到开单时的内容。
    if meta['counts']['cases'] > 10:
        first = call('GET', f'/handovers/{target}/pages/cases?page=2', viewer)
        make_case(operator, '翻页间隙入库的镜头', sample)
        again = call('GET', f'/handovers/{target}/pages/cases?page=2', viewer)
        check('翻页中断后恢复仍看到开单时的内容', first == again)
    else:
        check('翻页中断后恢复仍看到开单时的内容', False, '档案不足两页，未能覆盖翻页场景')

    # 8) 生成完整才可打开：building 状态的单据一律不可打开。
    with psycopg.connect(DSN, autocommit=True) as conn:
        building = conn.execute(
            "INSERT INTO handover_sheets (status, snapshot_id, counts, digest, created_by, created_at)"
            " VALUES ('building', '0-0', '{}'::json, 'x', 'verify', now()::text) RETURNING id"
        ).fetchone()[0]
        try:
            for path in (f'/handovers/{building}', f'/handovers/{building}/pages/cases',
                         f'/handovers/{building}/download'):
                call('GET', path, viewer, expect=409)
            listed = next(x for x in call('GET', '/handovers', viewer) if x['id'] == building)
            check('生成中的交班单不可打开（409），列表可见生成中状态', listed['status'] == 'building')
        finally:
            conn.execute('DELETE FROM handover_sheets WHERE id = %s', (building,))

    # 9) 开单留痕。
    actions = {(x['action'], x['reference']) for x in call('GET', '/audit', operator)}
    check('开单写入审计记录', ('open_handover', str(sheet1['id'])) in actions)

    late.close()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} 项未通过: {FAILURES}')
        return 1
    print('全部校验通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
