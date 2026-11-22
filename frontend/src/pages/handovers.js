import {heading} from '../components/common/heading.js';
import {entries, records} from '../components/common/records.js';
import {badge} from '../components/common/status.js';
import {sectionLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, el, esc} from '../utils/dom.js';
import {go} from '../router/hash.js';

const SECTIONS = ['cases', 'runs', 'evidence'];

function listPage(state) {
  const rows = state.handovers.map(sheet=>[
    `<a href="#handovers/${sheet.id}">交班单 #${sheet.id}</a>`,
    badge(sheet.status),
    esc(sheet.created_by),
    esc(date(sheet.created_at)),
    SECTIONS.map(key=>sheet.counts[key]).join(' / '),
  ]);
  const create = state.session.role==='operator' ? '<button id="open-handover">开一份交班单</button>' : '';
  return `${heading(topic.modules[4], '点开单那一刻已入库的档案、执行进度和签署意见留进同一份交班单；开单后才入库的记录，另开一份才能查到。', create)}
    ${records(['交班单', '状态', '开单人', '开单时间', '档案/执行/签署'], rows, '尚未开立交班单。经办员可以点开单按钮留下当前一份。')}`;
}

function detailPage(state, identity) {
  const data = state.sheetPage;
  if (!data || data.sheet.id!==identity) return heading(topic.modules[4]) + '<p class="empty">交班单加载中…</p>';
  const {sheet} = data;
  const tab = key=>`#handovers/${identity}?section=${key}`;
  const flip = number=>`#handovers/${identity}?section=${data.section}&amp;page=${number}`;
  const tabs = SECTIONS.map(key=>`<a href="${tab(key)}" ${key===data.section?'aria-current="page"':''}>${esc(sectionLabels[key])}（${sheet.counts[key]}）</a>`).join('');
  const prev = data.page>1 ? `<a class="secondary" href="${flip(data.page-1)}">上一页</a>` : '';
  const next = data.page<data.pages ? `<a class="secondary" href="${flip(data.page+1)}">下一页</a>` : '';
  const meta = `<dl>
    <dt>状态</dt><dd>${badge(sheet.status)}</dd>
    <dt>开单人</dt><dd>${esc(sheet.created_by)}</dd>
    <dt>开单时间</dt><dd>${esc(date(sheet.created_at))}</dd>
    <dt>生成完成</dt><dd>${esc(date(sheet.finished_at))}</dd>
    <dt>数据库快照号</dt><dd>${esc(sheet.snapshot_id)}</dd></dl>
    <details><summary>内容校验码</summary><code class="digest">${esc(sheet.digest)}</code></details>
    <p class="note">逐页所见与整份下载出自同一份留档内容，校验码一致即对得上。</p>`;
  return `${heading(`交班单 #${identity}`, '开单那一刻已入库的内容，翻页中断后恢复仍保持不变。',
    `<button id="download-handover" data-id="${identity}">下载整份交班单</button>`)}
    <div class="spread"><section><nav class="tabs" aria-label="交班单栏目">${tabs}</nav>
    ${entries(data.rows)}
    <div class="actions pager">${prev}<span class="note">第 ${data.page} / ${data.pages} 页 · 共 ${data.total} 条</span>${next}</div></section>
    <aside class="brief"><h2>开单信息</h2>${meta}</aside></div>
    <p><a href="#handovers">返回交班单列表</a></p>`;
}

export function handoversPage(state, identity) {
  return identity ? detailPage(state, identity) : listPage(state);
}

export function bindHandover(api, fail) {
  const open = document.getElementById('open-handover');
  open?.addEventListener('click', async()=>{
    open.disabled = true;
    try {
      const sheet = await api('/handovers', {method:'POST'});
      go(`handovers/${sheet.id}`);
    } catch (error) { open.disabled = false; fail(error); }
  });
  const download = document.getElementById('download-handover');
  download?.addEventListener('click', async()=>{
    download.disabled = true;
    try {
      const identity = download.dataset.id;
      const response = await api(`/handovers/${identity}/download`, {response:true});
      const url = URL.createObjectURL(await response.blob());
      const link = el('a');
      link.href = url;
      link.download = `handover-${identity}.json`;
      link.click();
      setTimeout(()=>URL.revokeObjectURL(url), 1000);
    } catch (error) { fail(error); }
    finally { download.disabled = false; }
  });
}
