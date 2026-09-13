/* 静态版前端：数据全在浏览器里，不调任何后端接口。
 *
 * 跟工作区 app.py + intent.py + store.py 的检索规则一一对应：
 *   splitKeyword  ← store.split_keyword（分隔符切分，_MIN_PART_LEN=2）
 *   matchRows     ← store._match_where（分类 LIKE 包含、关键词跨三字段 OR）
 *   sortRows      ← store.keyword_query 的 ORDER BY（NULL 一律排最后）
 *   parseSmart    ← intent.parse_intent_rule（剥停用词 + 分类包含匹配）
 *   pickKeyword   ← intent._pick_keyword（候选词命中不够 5 本就退到下一个）
 *
 * 没有的两样：**语义检索**（要 embedding 模型，浏览器跑不了 33MB 的 FAISS）
 * 和 **LLM 意图解析**（前端直连会把 API key 写在页面里）。所以"智能匹配"
 * 用的是和工作区降级路径同一套规则，能接住「我想看无脑爽文」这种，
 * 接不住太隐晦的说法——这时直接说没匹配到，不假装懂。
 */

const $ = (id) => document.getElementById(id);
let VIEW = 'grid', CATEGORY = '';
const HUES = ['#e35b5b','#e08a3c','#4a8fd4','#59a887','#8a6fc4','#c96a8e','#3fa9a0','#c2903f'];

let CATS = [];      // [{name, count}]，从 meta.json 来
let ALL = [];       // 全部小说，从 novels.json 来
let META = {};
const PAGE_SIZE = 40;
let LAST = null;    // 上一次的查询条件，翻页复用
let CURRENT = [];

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ---------------- 检索规则（与后端保持一致） ---------------- */

/* 分隔符集合照抄 store._SEP_RE。中文标点、全角空格都算分隔符，
   所以「无脑 爽文」「无脑、爽文」都能切成两片去 OR 匹配。 */
const SEP_RE = /[\s\u3000、，。！？；：·・…—\-_~～()（）《》〈〉【】\[\]{}「」『』“”‘’'"|/\\]+/;
const MIN_PART_LEN = 2;
const MIN_HITS = 5;        // 候选词命中够这么多本就算可用，同 intent.MIN_HITS

function splitKeyword(kw){
  const parts = String(kw).split(SEP_RE).filter(p => p.length >= MIN_PART_LEN);
  return parts.length ? parts : (String(kw).length >= MIN_PART_LEN ? [String(kw)] : []);
}

const field = (v) => String(v ?? '');
/* 关键词跨「书名 / 作者 / 简介」三个字段匹配，跟后端 SQL 的
   (title LIKE ? OR author LIKE ? OR description LIKE ?) 一样。 */
function hitPart(r, p){
  return field(r.title).includes(p) || field(r.author).includes(p) ||
         field(r.description).includes(p);
}

/* category 用包含匹配而不是等值：「言情」要能命中「古代言情」「玄幻言情」。 */
function matchRows(category, keyword){
  const parts = keyword ? splitKeyword(keyword) : [];
  return ALL.filter(r => {
    if (category && !field(r.category).includes(category)) return false;
    if (!parts.length) return true;
    // 多片是 OR：用户显式打了分隔符，说明是"这几个词任一"的意思
    return parts.some(p => hitPart(r, p));
  });
}

/* ORDER BY 的 NULL 行为：SQLite 里 ASC 时 NULL 排最前，会把七千多本没排名的
   书顶到第一页。这里统一改成 NULL 永远排最后。 */
function sortRows(rows, sortBy){
  const rank = (r) => (r.rank_num === null || r.rank_num === undefined
                       ? Number.POSITIVE_INFINITY : Number(r.rank_num));
  return rows.slice().sort((a, b) => {
    if (sortBy === 'rank') return rank(a) - rank(b);
    const sa = a.score, sb = b.score;
    const na = (sa === null || sa === undefined), nb = (sb === null || sb === undefined);
    if (na && nb) return rank(a) - rank(b);
    if (na) return 1;
    if (nb) return -1;
    if (Number(sb) !== Number(sa)) return Number(sb) - Number(sa);
    return rank(a) - rank(b);
  });
}

/* 规则兜底的停用词，照抄 intent._STOP_WORDS */
const STOP_WORDS = ["我想看","我想要","我想","想看","想要","看看","有没有","有没有人",
  "推荐","来一本","来本","给我","帮我","找个","找一下","一下",
  "什么","类型","小说","书","的","了","吗","呢","吧","啊",
  "有","是","要","看","个","一本"];

/* 一句话 → {category, keywords, echo}。逻辑同 intent.parse_intent_rule。 */
function parseSmart(q){
  let hit = '';
  for (const c of CATS){
    if (c.name && q.includes(c.name)){ hit = c.name; break; }
  }
  let text = q;
  for (const w of STOP_WORDS) text = text.split(w).join(' ');
  const kws = splitKeyword(text).slice(0, 3);
  return { category: hit, keywords: kws };
}

/* 整词搜不到时的候选片段：从整词开始，按长度递减列出所有连续子串。
   「无脑爽文男主厉害」这种连着打的一句话，库里不会有这句原文，但会有
   「爽文」「男主」这样的片段。后端这时会退到语义检索（按语义找意思像的），
   静态版没有语义检索，只能退到字面片段——语义上更弱，但至少不返回空白。 */
function keywordCandidates(kw){
  const out = [kw];
  for (let len = kw.length - 1; len >= MIN_PART_LEN; len--){
    for (let i = 0; i + len <= kw.length; i++) out.push(kw.slice(i, i + len));
  }
  return out;
}

/* 候选词按"最具体→最泛"试，取第一个命中够的；都不够就用命中最多的那个。
   照抄 intent._pick_keyword——不这么做的话搜「无脑爽文」只能出 1 本。

   同一长度的片段之间**取命中数最多的**：「无脑」和「爽文」一样长，谁更贴合
   原话没法判断，那就让命中数说话——146 本的「爽文」比 17 本的「无脑」更可能
   是用户想要的。多算几次过滤是毫秒级，不值得为省这点时间牺牲结果质量。 */
function pickKeyword(cat, cands){
  const byLen = new Map();
  for (const kw of cands){
    if (!byLen.has(kw.length)) byLen.set(kw.length, []);
    if (!byLen.get(kw.length).includes(kw)) byLen.get(kw.length).push(kw);
  }
  const levels = [...byLen.keys()].sort((a, b) => b - a);   // 长 → 短
  let best = '', bestN = -1, bestL = -1;
  for (const L of levels){
    for (const kw of byLen.get(L)){
      const n = matchRows(cat, kw).length;
      if (n > bestN || (n === bestN && L > bestL)){ best = kw; bestN = n; bestL = L; }
    }
    if (bestN >= MIN_HITS) break;      // 越长的片段越具体，够了就不再退
  }
  return { kw: best, n: Math.max(bestN, 0) };
}

/* ---------------- 渲染 ---------------- */

function coverBg(title){
  let h = 0;
  for (const ch of String(title)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return HUES[h % HUES.length];
}
function phHtml(r, big){
  const bg = coverBg(r.title);
  const dark = bg.replace(/^#/, '');
  const grad = `linear-gradient(140deg, #${dark} 0%, ${bg} 55%, rgba(0,0,0,.22) 100%)`;
  const name = String(r.title).slice(0, big ? 8 : 6);
  return `<div class="ph" style="background:${grad}">
    <div class="ph-t" style="${big ? 'font-size:26px' : ''}">${esc(name)}</div>
    <div class="ph-a">${esc(r.author || '佚名')}</div>
    <div class="ph-c">${esc(r.category || '')}</div>
  </div>`;
}
const coverHtml = (r) => r.cover_url
  ? `<img src="${esc(r.cover_url)}" alt="" loading="lazy">` : '';
function coverBlock(r){
  return `<div class="cover">${phHtml(r, false)}${coverHtml(r)}</div>`;
}

const stars = (s) => {
  if (s === null || s === undefined) return '<span style="color:#c8ccd2">暂无评分</span>';
  const full = Math.round(Number(s) / 2);
  return '<span class="stars">' + '★'.repeat(Math.max(0, Math.min(5, full))) +
         '☆'.repeat(5 - Math.max(0, Math.min(5, full))) + '</span> ' + Number(s).toFixed(1);
};

function renderGrid(rows){
  if (!rows.length) return '<div class="empty">没有匹配的书，换个词试试。</div>';
  return '<div class="grid">' + rows.map((r, i) => `
    <div class="card" data-i="${i}">
      ${coverBlock(r)}
      ${r.rank_num && r.rank_num <= 3 ? `<div class="badge">TOP ${r.rank_num}</div>` : ''}
      <div class="card-b">
        <div class="card-t" title="${esc(r.title)}">${esc(r.title)}</div>
        <div class="card-a">${esc(r.author || '佚名')}</div>
        <div class="card-d">${esc((r.description || '暂无简介').slice(0, 62))}</div>
        <div class="card-f">
          <span class="tag">${esc(r.category || '其他')}</span>
          <span>${esc(r.source_site || '')}</span>
        </div>
      </div>
    </div>`).join('') + '</div>';
}

function renderTable(rows){
  if (!rows.length) return '<div class="empty">没有匹配的书，换个词试试。</div>';
  const head = '<th>排名</th><th>评分</th><th>书名</th><th>作者</th><th>简介</th><th>来源</th>';
  const body = rows.map((r, i) => {
    const top = r.rank_num && r.rank_num <= 3 ? ' top' : '';
    return `<tr data-i="${i}">
      <td><span class="rank-dot${top}">${r.rank_num ?? i + 1}</span></td>
      <td>${r.score === null || r.score === undefined ? '—' : Number(r.score).toFixed(1)}</td>
      <td class="t-title">《${esc(r.title)}》</td>
      <td>${esc(r.author || '—')}</td>
      <td class="t-desc">${esc((r.description || '—').slice(0, 110))}</td>
      <td class="t-link"><a href="${esc(r.source_url || '#')}" target="_blank" rel="noreferrer">${esc((r.source_url || '').replace(/^https?:\/\//, '').slice(0, 34))}</a></td>
    </tr>`;
  }).join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function render(rows, mode, meta){
  CURRENT = rows;
  $('result').innerHTML = VIEW === 'grid' ? renderGrid(rows) : renderTable(rows);

  const total = (meta && meta.total) || rows.length;
  const page = (meta && meta.page) || 1;
  const kind = mode === 'smart' ? '智能匹配（本地规则）'
             : mode === 'all'   ? '全部小说（按评分降序）'
             : '分类/关键词检索';
  $('metaLine').textContent =
    `第 ${page} 页 · 本页 ${rows.length} 本 / 共 ${total} 本 · ${kind} · 点卡片看详情`;
  renderPager(page, !!(meta && meta.has_more), total);

  $('result').querySelectorAll('[data-i]').forEach(el => {
    el.addEventListener('click', () => openSheet(rows[Number(el.dataset.i)]));
  });
}

function renderPager(page, hasMore, total){
  const box = $('pager');
  if (!total || total <= PAGE_SIZE){ box.hidden = true; box.innerHTML = ''; return; }
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  box.hidden = false;
  box.innerHTML = `
    <button data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
    <span>第 ${page} / ${pages} 页 · 共 ${total} 本</span>
    <button data-page="${page + 1}" ${!hasMore ? 'disabled' : ''}>下一页</button>`;
  box.querySelectorAll('button[data-page]').forEach(b => {
    b.addEventListener('click', () => {
      if (!LAST) return;
      runSearch(LAST.q, LAST.mode, LAST.sort, Number(b.dataset.page));
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });
}

function renderCatTabs(){
  $('catTabs').innerHTML = CATS.map(c =>
    `<div class="cat" data-cat="${esc(c.name)}">${esc(c.name)}<span class="cat-n">${c.count}</span></div>`
  ).join('');
  $('allCount').textContent = CATS.reduce((n, c) => n + c.count, 0);
  $('sideCats').querySelectorAll('.cat').forEach(t =>
    t.classList.toggle('on', (t.dataset.cat || '') === (CATEGORY || '')));
}

/* ---------------- 详情 ---------------- */
function openSheet(r){
  if (!r) return;
  const links = [];
  if (r.source_url) links.push(`<a href="${esc(r.source_url)}" target="_blank" rel="noreferrer">${esc(r.source_site || '来源页面')}</a>`);
  if (r.catalog_url) links.push(`<a href="${esc(r.catalog_url)}" target="_blank" rel="noreferrer">国图相关页</a>`);
  $('sheetBody').innerHTML = `
    <div class="sheet-h">
      <div class="sheet-cover">${r.cover_url
        ? `<img src="${esc(r.cover_url)}" alt="">`
        : phHtml(r, true)}</div>
      <div style="min-width:0;flex:1">
        <h2>《${esc(r.title)}》</h2>
        <div class="sheet-meta">${esc(r.author || '佚名')} · ${esc(r.category || '其他')}
          ${r.site_category ? ' · 起点分类：' + esc(r.site_category) : ''}</div>
        <div>${stars(r.score)}</div>
        <div class="sheet-desc" style="margin-top:10px">${esc(r.description || '暂无简介')}</div>
      </div>
    </div>
    <div class="sheet-b">
      <div class="links">${links.join('')}</div>
      <h4>记录信息</h4>
      <div class="kv"><b>榜单排名</b><span>${r.rank_num ?? '—'}</span></div>
      <div class="kv"><b>来源站点</b><span>${esc(r.source_site || '榜单页解析')}</span></div>
      <div class="kv"><b>入库时间</b><span>${esc(r.create_time || '—')}</span></div>
      <div class="kv"><b>来源链接</b><span class="t-link"><a href="${esc(r.source_url || '#')}" target="_blank" rel="noreferrer">${esc(r.source_url || '—')}</a></span></div>
      ${r.catalog_url ? `<div class="kv"><b>国图链接</b><span class="t-link"><a href="${esc(r.catalog_url)}" target="_blank" rel="noreferrer">${esc(r.catalog_url)}</a></span></div>` : ''}
      <p style="color:var(--ink3);font-size:12px;margin:14px 0 0">
        仅收录书名、作者、分类、简介、封面等出版信息，不收录章节正文。
      </p>
    </div>`;
  $('mask').classList.add('on');
}
$('sheetX').onclick = () => $('mask').classList.remove('on');
$('mask').onclick = (e) => { if (e.target.id === 'mask') $('mask').classList.remove('on'); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('mask').classList.remove('on'); });

/* ---------------- 检索入口 ---------------- */

function showMsg(text, kind){ $('msg').innerHTML = text ? `<div class="msg ${kind}">${esc(text)}</div>` : ''; }

/* 一次查询：算出全部匹配 → 排序 → 切当前页。
   八千本在浏览器里过滤是毫秒级，不需要增量或索引。 */
function runSearch(q, mode, sort, page){
  LAST = { q, mode, sort };
  showMsg('', '');
  if (mode !== 'all' && !q){
    $('result').innerHTML = '<div class="empty">输入类型或描述开始检索</div>';
    return;
  }

  let rows, echo = '';
  if (mode === 'smart'){
    const plan = parseSmart(q);
    const cands = plan.keywords.flatMap(keywordCandidates);
    if (plan.category){
      let picked = pickKeyword(plan.category, cands);
      let cat = plan.category;
      // 分类一收窄就只剩几本，多半是这个分类猜错了：「女主重生复仇古言」里的
      // 「重生」确实是个分类名，但用户想要的显然不是只有 1 本的那个分类。
      // 这时丢掉分类重来一次——宁可宽一点，也别只给 1 本。
      if (picked.n < MIN_HITS){
        const wide = pickKeyword('', cands);
        if (wide.n > picked.n){ picked = wide; cat = ''; }
      }
      rows = matchRows(cat, picked.kw);
      echo = !picked.kw ? `按「${cat}」分类检索`
           : cat ? `按「${cat}」分类 + 关键词「${picked.kw}」检索`
                 : `按「${picked.kw}」检索（原话里的分类只命中 ${picked.n} 本，已放开分类）`;
    }else if (cands.length){
      const picked = pickKeyword('', cands);
      // 命中 0 就直说没匹配到：硬凑一个 1 本的片段出来比空结果更误导
      rows = picked.n ? matchRows('', picked.kw) : [];
      echo = picked.n
        ? `按「${picked.kw}」检索（原话里没认出分类，按字面片段匹配）`
        : '没匹配到相关的小说，换个说法试试（比如「无脑爽文」「女主重生复仇」）';
    }else{
      rows = [];
      echo = '没识别出条件，换个说法试试（比如「想看无脑爽文」「女主重生复仇」）';
    }
  }else if (mode === 'all'){
    rows = matchRows('', '');
  }else{
    rows = matchRows('', q);
  }

  rows = sortRows(rows, mode === 'all' ? 'score' : sort);
  const total = rows.length;
  const offset = (page - 1) * PAGE_SIZE;
  const slice = rows.slice(offset, offset + PAGE_SIZE);

  if (echo) showMsg(echo, 'echo');
  if (!total){
    $('result').innerHTML = '';
    $('metaLine').textContent = '—';
    $('pager').hidden = true;
    return;
  }
  render(slice, mode, { total, page, has_more: offset + slice.length < total });
}

function search(){
  const q = $('q').value.trim();
  if (!q){ showMsg('请输入小说类型或描述', 'err'); return; }
  const mode = document.querySelector('input[name=mode]:checked').value;
  runSearch(q, mode, 'rank', 1);
}

function browseCategory(cat){
  CATEGORY = cat;
  renderCatTabs();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  // 全库按 score 排：只有少数书有 rank_num，按排名排会把没排名的全顶到前面
  if (cat) runSearch(cat, 'keyword', 'rank', 1);
  else runSearch('', 'all', 'score', 1);
}

/* ---------------- 启动 ---------------- */
async function boot(){
  try{
    META = await (await fetch('data/meta.json')).json();
    CATS = META.categories || [];
    renderCatTabs();
    $('status').textContent = `库 ${META.novels} 本 · 静态只读版`;
    $('genAt').textContent = `数据更新于 ${META.generated_at || '—'}`;
  }catch(e){
    $('status').textContent = 'meta.json 加载失败';
  }
  $('result').innerHTML = '<div class="empty">正在下载书库（约 5.5MB，首次打开需要几秒）…</div>';
  try{
    ALL = await (await fetch('data/novels.json')).json();
  }catch(e){
    $('result').innerHTML = '<div class="empty">书库加载失败，检查 data/novels.json 是否一起发布了。</div>';
    return;
  }
  // 默认进「全部」，跟工作区默认落在武侠不同：静态版没有采集能力，
  // 落在某一个分类上会让人以为全站就那几百本。
  browseCategory('');
}

$('search').onclick = search;
$('q').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });
$('sideCats').addEventListener('click', e => {
  const t = e.target.closest('.cat');
  if (t) browseCategory(t.dataset.cat || '');
});
document.querySelectorAll('.seg').forEach(s => s.addEventListener('click', () => {
  document.querySelectorAll('.seg').forEach(x => x.classList.remove('on'));
  s.classList.add('on'); VIEW = s.dataset.view;
  if (CURRENT.length) render(CURRENT, document.querySelector('input[name=mode]:checked').value);
}));
boot();
