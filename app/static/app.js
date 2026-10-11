// 主题编辑台前端：hash 路由 + 轮询。所有状态都来自后端，刷新后可完整重建。
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
};
const STATUS = { pending: "排队中", running: "运行中", waiting_human: "等待人工", interrupted: "已中断",
  done: "已完成", failed: "失败", cancelled: "已取消", cancelling: "取消中" };
const NOVELTY = { new: "新进展", update: "内容更新", background: "背景资料", duplicate: "重复报道" };
const KIND = { full: "全文", excerpt: "节选", summary: "摘要" };
const GRAPH_NAME = { research: "主题检索", import: "链接导入", recheck: "复查", demo: "演示" };
const taskTitle = (t) => (t.graph === "recheck" ? "复查 · " : "") + (t.topic === "*" ? "全部主题" : t.topic);
const ACTIVE = new Set(["pending", "running", "cancelling"]);
const fmtTime = (t) => t ? new Date(typeof t === "number" ? t * 1000 : t).toLocaleString("zh-CN", { hour12: false }) : "—";
const split = (s) => (s || "").split(/[,，;；\n]/).map((x) => x.trim()).filter(Boolean);

let graphs = [];
let sourcesCache = [];
let current = null;          // 当前任务视图 {id, lastStepId, timer}

async function init() {
  graphs = await api("/api/graphs");
  const sel = $("#new-task [name=graph]");
  sel.innerHTML = graphs.map((g) => `<option value="${esc(g.name)}" ${g.name === "research" ? "selected" : ""}>${esc(GRAPH_NAME[g.name] || g.name)}</option>`).join("");
  await loadSourceChecks();
  $("#new-task").addEventListener("submit", submitTask);
  setupTranslate();
  $("#import-form").addEventListener("submit", submitImport);
  window.addEventListener("hashchange", route);
  route();
  refreshList();
  setInterval(refreshList, 5000);
}

// ---- 中英文关键词自动翻译：主题填写后自动补全，用户可直接修改；手动改过的字段不再被覆盖
const kwState = { edited: { keywords: false, en_keywords: false }, lastTopic: "" };

function setupTranslate() {
  const f = $("#new-task");
  ["keywords", "en_keywords"].forEach((n) => f[n].addEventListener("input", () => { kwState.edited[n] = true; }));
  f.topic.addEventListener("blur", () => { if (f.auto_translate.checked && f.topic.value.trim() !== kwState.lastTopic) translateKeywords(false); });
  $("#kw-btn").onclick = () => translateKeywords(true);
}

async function translateKeywords(force) {
  const f = $("#new-task");
  const topic = f.topic.value.trim();
  if (!topic) return;
  kwState.lastTopic = topic;
  const note = $("#kw-note");
  note.textContent = "翻译中…";
  // 手动重新翻译时，把用户已填写的词作为约束保留下来
  const body = { topic, keywords: kwState.edited.keywords ? split(f.keywords.value) : [],
    en_keywords: kwState.edited.en_keywords ? split(f.en_keywords.value) : [] };
  try {
    const r = await api("/api/keywords/suggest", { method: "POST", body });
    if (force || !kwState.edited.keywords) f.keywords.value = r.terms_zh.join(", ");
    if (force || !kwState.edited.en_keywords) f.en_keywords.value = r.queries_en.join(", ");
    const doms = (r.domains || []).map((d) => DOMAIN_NAME[d] || d).join("、");
    note.innerHTML = `${r.by === "llm" ? "大模型翻译" : "术语词典翻译"}${r.cached ? "（缓存）" : ""}；领域判断：${esc(doms)}`
      + (r.alt_zh?.length || r.alt_en?.length ? `<br>备用说法（检索无结果时自动换用）：${esc([...(r.alt_zh || []), ...(r.alt_en || [])].join("、"))}` : "")
      + ((r.notes || []).some((n) => n.includes("未收录")) ? `<br><span class="gap">${esc(r.notes.find((n) => n.includes("未收录")))}</span>` : "");
  } catch (err) { note.textContent = "翻译失败：" + err.message; }
}

async function loadSourceChecks() {
  sourcesCache = await api("/api/sources");
  $("#src-checks").innerHTML = "<small>信息源（全部勾选 = 按领域自动挑选；取消勾选某些 = 只用勾选的来源）</small>" + sourcesCache.filter((s) => s.id !== "web").map((s) =>
    `<label class="opt"><input type="checkbox" name="src" value="${esc(s.id)}" ${s.enabled ? "checked" : ""}> ${esc(s.name)} <i>${esc(s.method)} · ${(s.config?.domains || []).map((d) => DOMAIN_NAME[d] || d).join("/")}</i></label>`).join("");
}

async function submitTask(e) {
  e.preventDefault();
  const f = new FormData(e.target);
  let extra = {};
  try { extra = JSON.parse(f.get("params") || "{}"); } catch { return alert("额外参数不是合法 JSON"); }
  const budget = {};
  for (const k of ["max_steps", "max_tool_calls", "max_seconds", "max_cost"]) budget[k] = Number(f.get(k));
  const graph = f.get("graph");
  const thisTime = { focus: split(f.get("tt_focus")), exclude: split(f.get("tt_exclude")) };
  if (f.get("tt_save")) {      // 用户明确确认后才写入长期偏好
    for (const [type, value] of Object.entries(thisTime)) {
      if (value.length) await api("/api/preferences", { method: "POST", body: { type, value } });
    }
  }
  const params = graph === "research" ? {
    this_time: f.get("tt_save") ? {} : thisTime,
    domains: f.getAll("domain").length ? f.getAll("domain") : undefined,
    keywords: split(f.get("keywords")), en_keywords: split(f.get("en_keywords")),
    auto_translate: !!f.get("auto_translate"),
    sources: allChecked() ? undefined : f.getAll("src"), max_docs: Number(f.get("max_docs")), max_fetch: Number(f.get("max_fetch")),
    days: Number(f.get("days")), min_relevance: Number(f.get("min_relevance")),
    hitl_on_block: !!f.get("hitl_on_block"), ...extra } : extra;
  try {
    const { id } = await api("/api/tasks", { method: "POST", body: { topic: f.get("topic"), graph, params, budget } });
    location.hash = `#/task/${id}`;
    refreshList();
  } catch (err) { alert(err.message); }
}

function allChecked() {
  const boxes = $$("#src-checks input[name=src]");
  return boxes.every((b) => b.checked);
}

async function submitImport(e) {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    const { id } = await api("/api/import", { method: "POST", body: { url: f.get("url"), topic: f.get("topic") } });
    e.target.reset();
    location.hash = `#/task/${id}`;
    refreshList();
  } catch (err) { alert(err.message); }
}

async function refreshList() {
  const rows = await api("/api/tasks");
  $("#task-list").innerHTML = rows.map((t) => `
    <li><a href="#/task/${t.id}" class="${current?.id === t.id ? "active" : ""}">
      <span class="t">${esc(taskTitle(t))}</span>
      <span class="s">${esc(STATUS[t.status] || t.status)} · ${esc(GRAPH_NAME[t.graph] || t.graph)} · ${fmtTime(t.created_at)}</span>
    </a></li>`).join("") || `<li class="s">暂无任务</li>`;
}

// ---------------------------------------------------------------- 路由
function route() {
  if (current?.timer) clearTimeout(current.timer);
  current = null;
  const h = location.hash || "#/";
  const view = h.startsWith("#/library") ? "library" : h.startsWith("#/doc/") ? "library"
    : h.startsWith("#/memory") ? "memory"
    : h.startsWith("#/sources") ? "sources" : h.startsWith("#/settings") ? "settings" : "tasks";
  $$("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === view));
  $("#app").classList.toggle("wide", view !== "tasks");
  const m = h.match(/^#\/task\/(\w+)/);
  if (view === "tasks") loadSourceChecks();
  if (m) return showTask(m[1]);
  if (h.startsWith("#/library")) return showLibrary(new URLSearchParams(h.split("?")[1] || ""));
  const d = h.match(/^#\/doc\/(\w+)/);
  if (d) return showDoc(d[1]);
  const mm = h.match(/^#\/memory\/(m_\w+)/);
  if (mm) return showMemory(mm[1]);
  if (h.startsWith("#/memory")) return showMemoryHome(new URLSearchParams(h.split("?")[1] || ""));
  if (h.startsWith("#/sources")) return showSources();
  if (h.startsWith("#/settings")) return showSettings();
  $("#detail").innerHTML = `<div class="empty">从左侧提交一个主题，或打开已有任务。<br>任务进度保存在本地数据库，刷新页面不会丢失。</div>`;
}

// ---------------------------------------------------------------- 任务详情
function showTask(id) {
  current = { id, lastStepId: 0, timer: null };
  const el = $("#detail");
  el.innerHTML = "";
  el.append($("#tpl-detail").content.cloneNode(true));
  el.querySelector("[data-act=resume]").onclick = () => act("resume");
  el.querySelector("[data-act=cancel]").onclick = () => act("cancel");
  el.querySelector("[data-act=rewind]").onclick = doRewind;
  $("[data-f=docs-link]", el).href = `#/library?task=${id}&min_rel=0.4&sort=relevance&hide_dup=1`;
  poll();
}

async function act(name) {
  try { await api(`/api/tasks/${current.id}/${name}`, { method: "POST", body: {} }); poll(); }
  catch (err) { alert(err.message); }
}

async function doRewind() {
  const d = $("#detail");
  const node = $("[data-f=rewind-node]", d).value;
  const invalidate = split($("[data-f=rewind-inv]", d).value);
  try { await api(`/api/tasks/${current.id}/rewind`, { method: "POST", body: { node, invalidate } }); poll(); }
  catch (err) { alert(err.message); }
}

async function poll() {
  if (!current) return;
  const id = current.id;
  if (current.timer) clearTimeout(current.timer);
  try {
    const [task, steps] = await Promise.all([
      api(`/api/tasks/${id}`), api(`/api/tasks/${id}/trace?after=${current.lastStepId}`)]);
    if (current?.id !== id) return;
    renderTask(task);
    renderSteps(steps);
    const open = steps.find((s) => s.status === "running");
    if (open) current.lastStepId = open.id - 1;
    else if (steps.length) current.lastStepId = steps[steps.length - 1].id;
    const busy = ACTIVE.has(task.status) || task.running;
    current.timer = setTimeout(poll, busy ? 1200 : 8000);
  } catch (err) {
    const e = $("[data-f=error]");
    if (e) e.textContent = err.message;
    if (current) current.timer = setTimeout(poll, 5000);
  }
}

function renderTask(t) {
  const d = $("#detail");
  $("[data-f=graph]", d).textContent = `${GRAPH_NAME[t.graph] || t.graph} · ${t.id}`;
  $("[data-f=topic]", d).textContent = taskTitle(t);
  const b = $("[data-f=status]", d);
  b.textContent = STATUS[t.status] || t.status;
  b.className = `badge ${t.status}`;
  $("[data-f=node]", d).textContent = t.current_node && t.current_node !== "__end__"
    ? `下一步：${t.current_node} · 已保存 ${t.checkpoints} 个检查点` : `已保存 ${t.checkpoints} 个检查点`;
  $("[data-f=error]", d).textContent = t.error || "";
  $("[data-act=resume]", d).disabled = !["interrupted", "failed", "pending"].includes(t.status) || t.running;
  $("[data-act=cancel]", d).disabled = ["done", "cancelled"].includes(t.status);
  $("[data-f=docs-link]", d).style.display = ["research", "import", "recheck"].includes(t.graph) ? "" : "none";

  const u = t.usage || {}, bu = t.budget || {};
  const meters = [["步数", u.steps, bu.max_steps], ["工具调用", u.tool_calls, bu.max_tool_calls],
    ["时长 s", u.seconds, bu.max_seconds], ["成本 $", u.cost, bu.max_cost]];
  $("[data-f=budget]", d).innerHTML = meters.map(([n, v = 0, m]) => {
    const p = Math.min(100, (v / (m || 1)) * 100);
    return `<div class="meter ${p > 85 ? "hot" : ""}"><small><span>${n}</span><span>${+(+v).toFixed(2)} / ${m}</span></small>
      <div class="bar"><i style="width:${p}%"></i></div></div>`;
  }).join("");

  const g = graphs.find((x) => x.name === t.graph);
  const sel = $("[data-f=rewind-node]", d);
  if (g && !sel.options.length) sel.innerHTML = g.nodes.map((n) => `<option>${esc(n)}</option>`).join("");
  $("[data-f=state]", d).textContent = JSON.stringify(t.state, null, 2);
  renderHitl(t.hitl.filter((h) => h.status === "open"));
  renderReport(t.state.report, t.state);
}

function recheckReport(rep) {
  const OUT = { updated: "有更新（已存为新版本）", unchanged: "无变化", enriched: "补全了正文", failed: "抓取失败" };
  const items = rep.items || [];
  const changeTxt = (c) => !c ? "" : [
    c.numbers_removed?.length || c.numbers_added?.length ? `数字 ${esc((c.numbers_removed || []).join("、") || "—")} → ${esc((c.numbers_added || []).join("、") || "—")}` : "",
    c.sentences_added?.length ? `新增：${esc(c.sentences_added[0].slice(0, 60))}…` : "",
    c.sentences_removed?.length ? `删除：${esc(c.sentences_removed[0].slice(0, 60))}…` : ""].filter(Boolean).join("<br>");
  return `<div class="card report"><h3>复查结果</h3>
    <p class="small">范围：${esc(rep.scope)} · ${rep.force ? "手动复查（范围内所有可能变化的页面）" : "定时复查（只查已到期的页面）"}</p>
    <div class="stats">
      <div><b>${rep.checked}</b><small>已复查</small></div>
      <div><b>${rep.summary?.updated || 0}</b><small>有更新</small></div>
      <div><b>${rep.summary?.unchanged || 0}</b><small>无变化</small></div>
      <div><b>${rep.summary?.failed || 0}</b><small>失败</small></div>
    </div>
    ${items.length ? `<table class="tbl"><tr><th>页面</th><th>为什么复查</th><th>结果</th></tr>${items.map((r) => `
      <tr><td><a href="#/doc/${r.new_doc_id || r.doc_id}">${esc(r.title || r.url)}</a></td><td class="small">${esc(r.why || "")}</td>
      <td><span class="tag ${r.outcome === "updated" ? "nv-update" : ""}">${OUT[r.outcome] || esc(r.outcome)}</span>
      ${r.version ? ` v${r.version}` : ""}${r.error ? `<br><small>${esc(r.error)}</small>` : ""}
      ${r.change ? `<br><small>${changeTxt(r.change)}</small>` : ""}</td></tr>`).join("")}</table>`
    : `<p class="empty">这个范围内没有需要复查的页面。只有代码仓库、arXiv 论文和 3 天内发布的新闻会被安排复查。</p>`}
    <p class="small">范围内仍在复查计划中的页面 ${rep.pending} 篇${rep.next_due ? `，下次到期 ${fmtTime(rep.next_due)}` : ""}。</p></div>`;
}

function renderReport(rep, state) {
  const box = $("[data-f=report]");
  if (rep?.kind === "recheck") { box.innerHTML = recheckReport(rep); return; }
  if (!rep) {
    const cov = state?.coverage;
    box.innerHTML = cov ? `<div class="card"><h3>覆盖矩阵（进行中）</h3>${covTable(cov)}</div>` : "";
    return;
  }
  const chips = (obj, map = {}) => Object.entries(obj || {}).map(([k, v]) => `<span class="chip">${esc(map[k] || k)} <b>${v}</b></span>`).join("");
  box.innerHTML = `
    <div class="card report">
      <h3>采集报告</h3>
      <div class="stats">
        <div><b>${rep.total_docs}</b><small>资料（含重复）</small></div>
        <div><b>${rep.unique_docs}</b><small>去重后</small></div>
        <div><b>${rep.events}</b><small>事件</small></div>
        <div><b>${rep.image_stats?.kept ?? 0}</b><small>可用配图${rep.image_stats?.new ? `（新增 ${rep.image_stats.new.kept}）` : ""}</small></div>
        <div><b>${rep.actions_done}</b><small>执行行动</small></div>
      </div>
      <p class="chips">${chips(rep.by_novelty, NOVELTY)}</p>
      <p class="chips">${chips(rep.by_source, Object.fromEntries(sourcesCache.map((s) => [s.id, s.name])))}</p>
      <p class="chips">${chips(rep.by_kind, KIND)}</p>
      ${rep.memory ? memoryReport(rep.memory) : ""}
      ${rep.domains ? `<p class="small">主题领域：${rep.domains.map((d) => DOMAIN_NAME[d] || d).join("、")}；选用来源 ${rep.source_ids?.length || 0} 个：${(rep.source_ids || []).map((id) => esc(sourcesCache.find((x) => x.id === id)?.name || id)).join("、")}</p>` : ""}
      ${rep.quality ? qualityReport(rep.quality) : ""}
      <h4>覆盖矩阵</h4>${covTable(rep.coverage)}
      ${rep.covered_at ? `<p class="small">在第 ${rep.covered_at.docs} 篇资料时覆盖了全部子问题，之后只做收尾${rep.dropped_after_cover ? `，放弃了 ${rep.dropped_after_cover} 个低价值行动` : ""}。</p>` : ""}
      ${rep.gaps?.length ? `<h4>缺口补检</h4><table class="tbl"><tr><th>子问题</th><th>结果</th><th>用时</th><th>做了什么</th></tr>${rep.gaps.map((g) => `
        <tr><td>${esc(g.name)}</td><td><span class="tag">${{closed: "已补上", insufficient: "暂无足够资料", open: "进行中"}[g.status] || esc(g.status)}</span></td>
        <td>${g.seconds != null ? g.seconds + "s" : ""}</td>
        <td class="small">${esc(g.how || "")}${g.added_keywords?.length ? `<br>补充同义词：${esc(g.added_keywords.slice(0, 10).join("、"))}` : ""}${g.queries?.length ? `<br>检索：${esc(g.queries.join("；"))}` : ""}</td></tr>`).join("")}</table>` : ""}
      <h4>检索计划</h4><p class="small">中文词：${esc((rep.plan?.terms_zh || []).join("、"))}；英文检索式：${esc((rep.plan?.queries_en || []).join("；") || "无")}；由 ${rep.plan?.by === "llm" ? "大模型" : "规则"} 生成；定向补缺口 ${rep.refine_rounds} 轮</p>
      ${rep.failures?.length ? `<h4>失败与降级</h4><table class="tbl"><tr><th>对象</th><th>原因</th><th>处理</th></tr>${rep.failures.map((f) => `
        <tr><td>${esc(f.target || f.type)}${f.query ? `<br><small>${esc(f.query)}</small>` : ""}</td><td><span class="tag">${esc(f.kind)}</span> ${esc(f.reason)}</td><td>${esc(f.fallback || "")}</td></tr>`).join("")}</table>` : ""}
      <h4>可能遗漏</h4><ul class="small">${(rep.omissions || []).map((o) => `<li>${esc(o)}</li>`).join("") || "<li>无</li>"}</ul>
    </div>`;
}

const VERIFY = { supported: "已核实", unverified: "待核实", disputed: "有争议", refuted: "已被否定" };
const MSTATUS = { active: "有效", stale: "需复查/需更正", superseded: "已被更正", deleted: "已删除",
  invalid: "已失效", resolved: "已裁决" };

function memoryReport(m) {
  const chip = (n, v) => `<span class="chip">${n} <b>${v}</b></span>`;
  return `<h4>记忆</h4>
    <details class="mem-box" ${m.recalled?.length ? "open" : ""}><summary>本次召回 ${m.recalled?.length || 0} 条长期记忆</summary>
      <ul class="small">${(m.recalled || []).map((l) => `<li>${esc(l)}</li>`).join("") || "<li>无（首次处理该主题）</li>"}</ul>
      ${(m.pref_notes || []).map((n) => `<p class="small gap">${esc(n)}</p>`).join("")}
    </details>
    <p class="chips">${chip("新增事实", m.new_facts)}${chip("被更多来源支持", m.supported)}${chip("更正", m.corrected)}${chip("冲突", m.conflicts)}</p>
    ${(m.corrections || []).map((c) => `<p class="small">更正：<s>${esc(c.old)}</s> → <b>${esc(c.new)}</b>${c.stale_deliveries?.length ? `（${c.stale_deliveries.length} 条历史交付已标记需更正）` : ""}</p>`).join("")}
    ${(m.disputes || []).map((d) => `<p class="small">${d.open ? `<span class="gap">未解争议</span> ${esc(d.content)}` : `争议已裁决：采信「${esc(d.winner)}」，否定「${esc(d.loser)}」`}（证据分 ${d.score?.join(" vs ")}）</p>`).join("")}
    ${m.facts?.length ? `<details class="mem-box"><summary>本次涉及的事实 ${m.facts.length} 条</summary><ul class="small">${m.facts.map((f) =>
      `<li><a href="#/memory/${f.id}">${esc(f.content)}</a> <span class="tag v-${f.verify}">${VERIFY[f.verify] || f.verify}</span> 独立来源 ${f.independent ?? 0}</li>`).join("")}</ul></details>` : ""}`;
}

function covTable(cov) {
  return `<table class="tbl cov"><tr><th>子问题</th><th>已有 / 需要</th><th></th></tr>${Object.values(cov).map((c) => `
    <tr><td>${esc(c.name)}</td><td>${c.have} / ${c.need}</td><td>${c.have >= c.need ? '<span class="ok">✓ 已覆盖</span>' : '<span class="gap">缺口</span>'}</td></tr>`).join("")}</table>`;
}

function renderHitl(open) {
  const box = $("[data-f=hitl]");
  const key = "k:" + open.map((h) => h.id).join();
  if (box.dataset.key === key) return;        // 避免轮询时清掉用户正在填写的内容
  box.dataset.key = key;
  box.innerHTML = open.map((h) => {
    const opts = h.payload.options;
    const body = h.kind === "choose" && Array.isArray(opts)
      ? opts.map((o, i) => `<label class="opt"><input type="checkbox" checked value="${i}"> ${esc(typeof o === "string" ? o : o.label || JSON.stringify(o))}</label>`).join("")
      : `<label>答复 (JSON)<textarea rows="5">${esc(JSON.stringify(h.payload.default ?? {}, null, 2))}</textarea></label>
         ${h.payload.default?.action ? `<p class="small">action 可选：paste（粘贴正文到 text）、alt_url（替代链接填到 url）、skip（跳过）</p>` : ""}`;
    return `<div class="card hitl" data-id="${h.id}"><h3>需要你的判断 · ${esc(h.node)}</h3>
      <p>${esc(h.prompt)}</p>${body}<button>提交并继续</button></div>`;
  }).join("");
  box.querySelectorAll(".hitl").forEach((card) => {
    const h = open.find((x) => x.id === card.dataset.id);
    card.querySelector("button").onclick = async () => {
      let response;
      if (h.kind === "choose" && Array.isArray(h.payload.options)) {
        const idx = [...card.querySelectorAll("input:checked")].map((i) => +i.value);
        response = { keep: idx.map((i) => h.payload.options[i]), keep_index: idx };
      } else {
        const raw = card.querySelector("textarea").value;
        try { response = JSON.parse(raw); } catch { response = raw; }
      }
      try { await api(`/api/hitl/${h.id}`, { method: "POST", body: { response } }); box.dataset.key = ""; poll(); }
      catch (err) { alert(err.message); }
    };
  });
}

function renderSteps(steps) {
  const ol = $("[data-f=timeline]");
  if (!ol) return;
  for (const s of steps) {
    let li = ol.querySelector(`[data-sid="${s.id}"]`);
    if (!li) { li = document.createElement("li"); li.dataset.sid = s.id; ol.append(li); }
    const sys = s.node === "__harness__";
    li.className = `${s.status} ${sys ? "sys" : ""}`;
    const dur = s.ended_at ? `${((s.ended_at - s.started_at) * 1000).toFixed(0)}ms` : "…";
    li.innerHTML = `
      <div class="step-head"><b>#${s.seq} ${esc(sys ? "Harness" : s.node)}</b>
        <span>${esc(s.status)} · ${dur}${s.next_node && s.next_node !== "__end__" ? ` → ${esc(s.next_node)}` : ""}</span></div>
      ${s.thought ? `<p class="thought">${esc(s.thought)}</p>` : ""}
      ${s.summary ? `<p class="obs">${esc(s.summary)}</p>` : ""}
      ${(s.tool_calls || []).map((c) => `
        <details class="tc"><summary><span class="st-${c.status}">${esc(c.tool)} · ${esc(c.status)}</span> · ${c.latency_ms ?? 0}ms <small>${esc(toolHint(c))}</small></summary>
          <pre>输入 ${esc(JSON.stringify(c.input, null, 1))}</pre>
          ${c.error ? `<pre class="error">${esc(c.error)}</pre>` : `<pre>输出 ${esc(c.output_preview ?? JSON.stringify(c.output))}</pre>`}
        </details>`).join("")}
      ${s.error ? `<details class="tc"><summary class="st-error">错误</summary><pre>${esc(s.error)}</pre></details>` : ""}`;
  }
}

function toolHint(c) {
  const i = c.input || {};
  if (c.tool === "source_search") return `${i.source_id}「${i.query}」`;
  if (c.tool === "page_fetch" || c.tool === "image_download") return (i.url || "").slice(0, 70);
  return "";
}

// ---------------------------------------------------------------- 资料库
async function showLibrary(qs) {
  const el = $("#detail");
  const topics = await api("/api/topics");
  const p = Object.fromEntries(qs.entries());
  el.innerHTML = `
    <div class="detail-head"><div><p class="kicker">资料库</p><h1>${esc(p.topic || (p.task ? "本次任务的资料" : "全部资料"))}</h1></div>
      <div class="actions"><button class="ghost" id="btn-recheck" title="代码仓库、arXiv 论文、3 天内的新闻可能有更新或更正">${p.task ? "复查本次资料中可能变化的页面" : p.topic ? "复查该主题可能变化的页面" : "复查全部主题可能变化的页面"}</button></div></div>
    <form class="filters" id="lib-filter">
      <select name="topic"><option value="">全部主题</option>${topics.map((t) => `<option ${t.topic === p.topic ? "selected" : ""}>${esc(t.topic)}</option>`).join("")}</select>
      <select name="novelty"><option value="">全部类型</option>${Object.entries(NOVELTY).map(([k, v]) => `<option value="${k}" ${p.novelty === k ? "selected" : ""}>${v}</option>`).join("")}</select>
      <select name="source_id"><option value="">全部来源</option>${sourcesCache.map((s) => `<option value="${s.id}" ${p.source_id === s.id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select>
      <select name="content_kind"><option value="">全文/节选/摘要</option>${Object.entries(KIND).map(([k, v]) => `<option value="${k}" ${p.content_kind === k ? "selected" : ""}>${v}</option>`).join("")}</select>
      <select name="sort"><option value="time">按发布时间</option><option value="fetched" ${p.sort === "fetched" ? "selected" : ""}>按采集时间</option><option value="relevance" ${p.sort === "relevance" ? "selected" : ""}>按相关度</option></select>
      <select name="min_rel"><option value="">全部相关度</option><option value="0.4" ${p.min_rel === "0.4" ? "selected" : ""}>相关度 ≥ 0.4</option><option value="0.7" ${p.min_rel === "0.7" ? "selected" : ""}>相关度 ≥ 0.7</option></select>
      <label class="opt"><input type="checkbox" name="hide_dup" value="1" ${p.hide_dup ? "checked" : ""}> 隐藏重复</label>
      <input name="q" placeholder="搜索标题/正文" value="${esc(p.q || "")}">
      ${p.task ? `<input type="hidden" name="task" value="${esc(p.task)}">` : ""}
      <button>筛选</button>
    </form>
    <div id="doc-list" class="doc-list"><p class="small">加载中…</p></div>`;
  $("#lib-filter").onsubmit = (e) => {
    e.preventDefault();
    const f = new URLSearchParams([...new FormData(e.target).entries()].filter(([, v]) => v));
    location.hash = "#/library?" + f.toString();
  };
  $("#btn-recheck").onclick = async (e) => {
    e.target.disabled = true;
    e.target.textContent = "正在创建复查任务…";
    const { id } = await api("/api/recheck", { method: "POST", body: { task: p.task, topic: p.topic } });
    refreshList();
    location.hash = `#/task/${id}`;
  };
  const rows = await api("/api/docs?" + qs.toString());
  $("#doc-list").innerHTML = rows.length ? `<p class="small">${rows.length} 条</p>` + rows.map(docRow).join("") : `<p class="empty">没有资料</p>`;
}

function qualityReport(q) {
  const f = q.fresh || {}, r = q.relevance || {}, j = q.judge || {};
  return `<h4>资料质量</h4>
    <p class="small"><b>时效</b>（新闻/社区窗口 ${q.window_days} 天，论文/仓库 ${q.academic_days} 天）：
      7 天内 ${f["7d"] || 0} · 30 天内 ${f["30d"] || 0} · 窗口内更早 ${f.window || 0} · 窗口外背景 ${f.older || 0} · 无日期 ${f.undated || 0}；
      检索时已过滤超出窗口的 ${q.dropped_old || 0} 条</p>
    <p class="small"><b>相关度</b>（门槛 ${q.min_relevance}，平均 ${q.avg_relevance ?? "—"}）：
      高 ≥0.7 ${r.high || 0} · 中 ${r.mid || 0} · 低于门槛 ${r.low || 0}（不进交付列表）；交付 ${q.delivered} 篇。
      ${j.by === "llm" ? `大模型复核 ${j.judged} 篇：${j.demoted} 篇判为关键词巧合被降级，${j.promoted} 篇被升级。` : "未配置大模型，仅用规则相关度。"}</p>`;
}

function relTag(d) {
  if (d.relevance == null) return "";
  const v = Number(d.relevance);
  const cls = v >= 0.7 ? "rel-hi" : v >= 0.4 ? "rel-mid" : "rel-lo";
  return `<span class="tag ${cls}" title="${esc(d.judge_reason || "规则相关度")}">相关 ${v.toFixed(2)}${d.judge_reason ? " · " + esc(d.judge_reason) : ""}</span>`;
}

function docRow(d) {
  const src = sourcesCache.find((s) => s.id === d.source_id);
  return `<a class="doc-row ${d.duplicate_of ? "dup" : ""}" href="#/doc/${d.id}">
    <div class="doc-main"><b>${esc(d.title || d.url)}</b>
      <small>${esc(src?.name || d.source_id)} · ${esc(d.author || "")} · 发布 ${fmtTime(d.published_at)} · 采集 ${fmtTime(d.fetched_at)}</small></div>
    <div class="doc-tags">
      <span class="tag nv-${d.novelty}">${NOVELTY[d.novelty] || d.novelty}</span>
      ${relTag(d)}
      <span class="tag">${KIND[d.content_kind] || d.content_kind} ${d.chars ?? 0} 字</span>
      ${d.version > 1 ? `<span class="tag">v${d.version}</span>` : ""}
      ${d.primary_src ? `<span class="tag pri">一手</span>` : ""}
      ${d.via_browser ? `<span class="tag">浏览器渲染</span>` : ""}
      ${d.relation === "reprint" ? `<span class="tag">转载</span>` : d.relation === "same_event" ? `<span class="tag">同事件</span>` : ""}
      ${d.images_kept ? `<span class="tag">图 ${d.images_kept}</span>` : ""}
    </div></a>`;
}

async function showDoc(id) {
  const el = $("#detail");
  const d = await api(`/api/docs/${id}`);
  const m = d.meta || {};
  const imgs = d.images || [];
  el.innerHTML = `
    <p class="kicker"><a href="#/library?topic=${encodeURIComponent(d.topic)}">← ${esc(d.topic)}</a></p>
    <h1 class="doc-title">${esc(d.title || d.url)}</h1>
    <table class="tbl meta-tbl">
      <tr><th>来源</th><td>${esc(d.source?.name || d.source_id)}（${esc(d.source?.method || "")}）${(m.sources_seen || []).length > 1 ? ` · 也出现在：${esc(m.sources_seen.join("、"))}` : ""}</td></tr>
      <tr><th>作者</th><td>${esc(d.author || "—")}</td></tr>
      <tr><th>发布时间</th><td>${fmtTime(d.published_at)}</td></tr>
      <tr><th>采集时间</th><td>${fmtTime(d.fetched_at)}</td></tr>
      <tr><th>内容</th><td>${KIND[d.content_kind]} · ${(d.text || "").length} 字 · ${NOVELTY[d.novelty]} · 版本 v${d.version}${m.via_browser ? " · 浏览器渲染" : ""}${m.primary ? " · 一手来源" : ""}</td></tr>
      <tr><th>原文定位</th><td><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url)}</a>${d.locator?.via ? `<br>经由：<a href="${esc(d.locator.via.url)}" target="_blank" rel="noopener">${esc(d.locator.via.source)} 讨论页</a>` : ""}${m.cited_by ? `<br>被引用于：<a href="#/doc/${esc(m.cited_by)}">${esc(m.cited_by)}</a>` : ""}</td></tr>
      ${m.license ? `<tr><th>使用条件</th><td>${esc(m.license)}</td></tr>` : ""}
      ${d.recheck_at ? `<tr><th>下次复查</th><td>${fmtTime(d.recheck_at)}</td></tr>` : ""}
    </table>
    ${d.versions.length > 1 ? `<h3>版本历史</h3><ul class="small">${d.versions.map((v) => `<li><a href="#/doc/${v.id}">v${v.version}</a> · ${fmtTime(v.fetched_at)} · ${v.status === "superseded" ? "已被新版本取代" : "当前"}${v.change ? ` · 相似度 ${v.change.similarity}${v.change.numbers_removed?.length ? ` · 数字变化：${esc(v.change.numbers_removed.join(","))} → ${esc(v.change.numbers_added.join(","))}` : ""}` : ""}
      ${v.change?.sentences_added?.length ? `<br><small>新增：${esc(v.change.sentences_added.slice(0, 2).join(" / "))}</small>` : ""}</li>`).join("")}</ul>` : ""}
    ${d.reprints.length ? `<h3>转载 / 重复</h3>${d.reprints.map(docRow).join("")}` : ""}
    ${d.same_event.length ? `<h3>同一事件的其他报道</h3>${d.same_event.map(docRow).join("")}` : ""}
    ${imgs.length ? `<h3>图片（保留 ${imgs.filter((i) => i.status === "kept").length} / 共 ${imgs.length}）</h3>
      <div class="img-grid">${imgs.map((i) => `
        <figure class="${i.status}">
          ${i.local_path ? `<img src="/media/${esc(i.local_path)}" loading="lazy" alt="">` : `<div class="noimg">未下载</div>`}
          <figcaption><b>${i.status === "kept" ? "保留" : "筛除"}</b> ${i.width ? `${i.width}×${i.height}` : ""}
            ${i.reject_reason ? `<br><span class="gap">${esc(i.reject_reason)}</span>` : ""}
            ${i.caption ? `<br>图注：${esc(i.caption)}` : ""}
            ${i.context_text ? `<br><small>相邻正文：${esc(i.context_text.slice(0, 120))}</small>` : ""}
            <br><small>段落位置 ${i.position ?? "—"} · ${esc(i.license || "")}</small>
            <br><small><a href="${esc(i.url)}" target="_blank" rel="noopener">原图地址</a></small></figcaption>
        </figure>`).join("")}</div>` : ""}
    ${m.fetch_attempts ? `<h3>抓取过程</h3><pre>${esc(JSON.stringify(m.fetch_attempts, null, 1))}</pre>` : ""}
    <h3>正文</h3><div class="doc-text">${(d.text || "").split(/\n+/).map((p) => p.startsWith("## ") ? `<h4>${esc(p.slice(3))}</h4>` : `<p>${esc(p)}</p>`).join("")}</div>`;
}

// ---------------------------------------------------------------- 记忆
const DOMAIN_NAME = { tech: "科技", finance: "金融", science: "学术", general: "综合" };
const PREF_TYPES = { reading_minutes: "阅读时间（分钟）", background: "知识背景", focus: "关注重点",
  exclude: "不想看的内容", source_prefer: "来源偏好", style: "写作要求" };

function memRow(m) {
  const v = m.meta?.verify;
  return `<a class="doc-row mem-${m.status}" href="#/memory/${m.id}">
    <div class="doc-main"><b>${esc(m.content)}</b>
      <small>${esc(m.kind)} · 更新 ${fmtTime(m.updated_at)} · 使用 ${m.used_count} 次${m.last_used_at ? `（最近 ${fmtTime(m.last_used_at)}）` : ""}${m.note ? ` · ${esc(m.note)}` : ""}</small></div>
    <div class="doc-tags">
      <span class="tag st-${m.status}">${MSTATUS[m.status] || m.status}</span>
      ${m.invalid_value ? `<span class="tag v-refuted">内容无效，未生效（建议删除）</span>` : ""}
      ${v ? `<span class="tag v-${v}">${VERIFY[v] || v}</span>` : ""}
      ${m.meta?.independent != null ? `<span class="tag">独立来源 ${m.meta.independent}${m.meta.reprints ? ` · 转载 ${m.meta.reprints}` : ""}</span>` : ""}
      ${m.applies_to && m.applies_to !== "all" ? `<span class="tag">${esc(m.applies_to)}</span>` : ""}
    </div></a>`;
}

async function showMemoryHome(qs) {
  const el = $("#detail");
  const topics = await api("/api/memory/topics");
  const topic = qs.get("topic") || topics[0]?.topic || "";
  const showDead = qs.get("all") === "1";
  const [prefs, rows, preview] = await Promise.all([
    api("/api/memories?scope=user&kind=preference&status=alive"),
    topic ? api(`/api/memories?scope=topic&topic=${encodeURIComponent(topic)}${showDead ? "" : "&status=alive"}`) : [],
    topic ? api(`/api/memory/preview?topic=${encodeURIComponent(topic)}`) : null]);
  const by = (k) => rows.filter((m) => m.kind === k);
  el.innerHTML = `
    <div class="detail-head"><div><p class="kicker">记忆</p><h1>下次再来，别从头开始</h1>
      <p class="meta">每条记忆都记录来源、适用范围、更新时间和使用次数；可以查看证据、更正或删除，失效会同步到派生条目。</p></div></div>
    <div class="card"><h3>用户偏好（长期）</h3>
      ${prefs.map(memRow).join("") || '<p class="small">还没有长期偏好。任务里的“本次要求”默认不会保存，勾选“同时保存为长期偏好”或在这里添加才会生效。</p>'}
      <form id="pref-form" class="row pref-form" style="margin-top:10px">
        <select name="type">${Object.entries(PREF_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="value" required>
        <input name="applies_to" placeholder="适用范围（可空=全部，如 topic:主题名）">
        <button>保存为长期偏好</button></form>
      <p class="small">同一类型再次保存会更正旧值（旧值保留在变化记录里）。</p>
    </div>
    <div class="filters">
      <select id="mem-topic">${topics.map((t) => `<option ${t.topic === topic ? "selected" : ""}>${esc(t.topic)}</option>`).join("") || "<option>暂无主题记忆</option>"}</select>
      <label class="opt"><input type="checkbox" id="mem-dead" ${showDead ? "checked" : ""}> 显示已更正/已删除/已失效</label>
    </div>
    ${topic ? `
    <h3>未解争议</h3>${by("dispute").map(memRow).join("") || '<p class="small">无</p>'}
    <h3>事实与验证状态</h3>${by("fact").map(memRow).join("") || '<p class="small">无</p>'}
    <h3>历史交付</h3>${by("delivery").map(memRow).join("") || '<p class="small">无</p>'}
    <details class="card"><summary>下一次任务会召回什么（预览，约 ${preview.tokens} token，预算外舍弃 ${preview.dropped} 条）</summary><pre>${esc(preview.text || "（无）")}</pre></details>` : ""}`;
  $("#mem-topic").onchange = (e) => location.hash = `#/memory?topic=${encodeURIComponent(e.target.value)}${showDead ? "&all=1" : ""}`;
  $("#mem-dead").onchange = (e) => location.hash = `#/memory?topic=${encodeURIComponent(topic)}${e.target.checked ? "&all=1" : ""}`;
  const PH = { reading_minutes: "分钟数，如 5", background: "如：程序员，熟悉机器学习", focus: "多个用逗号分隔，如：复现, 开源",
    exclude: "多个用逗号分隔，如：融资, 手机", source_prefer: "来源 ID，逗号分隔，如：arxiv, wallstcn", style: "如：先结论后细节，少用形容词" };
  const pf = $("#pref-form"), sel = pf.querySelector("[name=type]"), inp = pf.querySelector("[name=value]");
  const syncInput = () => {
    inp.type = sel.value === "reading_minutes" ? "number" : "text";
    if (sel.value === "reading_minutes") { inp.min = 1; inp.max = 240; }
    inp.placeholder = PH[sel.value] || "";
    const cur = prefs.find((p) => p.meta?.type === sel.value);
    inp.value = cur ? (Array.isArray(cur.meta.value) ? cur.meta.value.join(", ") : cur.meta.value ?? "") : "";
  };
  sel.onchange = syncInput;
  syncInput();
  $("#pref-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const type = f.get("type");
    let value;
    if (type === "reading_minutes") {
      value = parseInt(String(f.get("value")).replace(/[^\d]/g, ""), 10);
      if (!value) return alert("请输入分钟数，例如 5");
    } else value = ["focus", "exclude", "source_prefer"].includes(type) ? split(f.get("value")) : f.get("value");
    try { await api("/api/preferences", { method: "POST", body: { type, value, applies_to: f.get("applies_to") || "all" } }); showMemoryHome(qs); }
    catch (err) { alert(err.message); }
  };
}

async function showMemory(id) {
  const el = $("#detail");
  const m = await api(`/api/memories/${id}`);
  const alive = ["active", "stale"].includes(m.status);
  el.innerHTML = `
    <p class="kicker"><a href="#/memory?topic=${encodeURIComponent(m.topic || "")}">← 记忆</a></p>
    <h1 class="doc-title">${esc(m.content)}</h1>
    <table class="tbl meta-tbl">
      <tr><th>类型</th><td>${esc(m.scope)} / ${esc(m.kind)}${m.meta?.type ? ` / ${esc(PREF_TYPES[m.meta.type] || m.meta.type)}` : ""}</td></tr>
      <tr><th>状态</th><td><span class="tag st-${m.status}">${MSTATUS[m.status] || m.status}</span>
        ${m.meta?.verify ? `<span class="tag v-${m.meta.verify}">${VERIFY[m.meta.verify]}</span>` : ""} ${esc(m.note || "")}</td></tr>
      ${m.meta?.independent != null ? `<tr><th>证据强度</th><td>独立来源 ${m.meta.independent} 个${m.meta.primary ? "（含一手来源）" : ""}；转载 ${m.meta.reprints || 0} 篇（不重复计数）${m.meta.generated_ignored ? `；系统生成内容 ${m.meta.generated_ignored} 条已忽略` : ""}；置信度 ${m.confidence ?? "—"}</td></tr>` : ""}
      <tr><th>适用范围</th><td>${esc(m.applies_to || "all")}${m.topic ? ` · 主题：${esc(m.topic)}` : ""}</td></tr>
      <tr><th>来源</th><td><code>${esc(JSON.stringify(m.source))}</code></td></tr>
      <tr><th>时间</th><td>创建 ${fmtTime(m.created_at)} · 更新 ${fmtTime(m.updated_at)}${m.recheck_at ? ` · 复查 ${fmtTime(m.recheck_at)}` : ""}</td></tr>
      <tr><th>使用</th><td>${m.used_count} 次${m.last_used_at ? `，最近 ${fmtTime(m.last_used_at)}` : ""}</td></tr>
      ${m.meta?.previous_content ? `<tr><th>更正前</th><td><s>${esc(m.meta.previous_content)}</s></td></tr>` : ""}
      ${m.successor ? `<tr><th>已被更正为</th><td><a href="#/memory/${m.successor.id}">${esc(m.successor.content)}</a></td></tr>` : ""}
    </table>
    ${alive ? `<form id="mem-edit" class="card"><h3>更正这条记忆</h3>
      <textarea name="content" rows="2" style="width:100%">${esc(m.content)}</textarea>
      <input name="reason" placeholder="更正原因（可空）" style="width:100%;margin:6px 0;padding:6px">
      <div class="row"><button>保存更正</button><button type="button" class="ghost danger" id="mem-del">删除这条记忆</button></div>
      <p class="small">更正会生成新版本，旧版本保留可追溯；依赖它的派生记忆（如历史交付）会被标记“需更正”。删除后不再被召回，派生记忆同步失效。</p></form>` : ""}
    ${m.evidence?.length ? `<h3>证据（${m.evidence.length}）</h3><table class="tbl"><tr><th>资料</th><th>原文引句</th><th>立场</th></tr>${m.evidence.map((e) => `
      <tr><td><a href="#/doc/${e.doc_id}">${esc(e.title || e.url)}</a><br><small>${esc(e.source_id)} · v${e.version}${e.duplicate_of ? " · 转载" : ""}${e.doc_status !== "active" ? ` · ${esc(e.doc_status)}` : ""}</small></td>
      <td>${esc(e.snippet)}</td><td>${e.stance === "support" ? "支持" : e.stance === "refute" ? "反对/旧版" : esc(e.stance)}</td></tr>`).join("")}</table>` : ""}
    ${m.upstream?.length ? `<h3>依据</h3><ul class="small">${m.upstream.map((u) => `<li>${u.kind === "document" ? `<a href="#/doc/${u.id}">` : `<a href="#/memory/${u.id}">`}${esc(u.content)}</a> · ${esc(MSTATUS[u.status] || u.status)}</li>`).join("")}</ul>` : ""}
    ${m.derived?.length ? `<h3>派生条目</h3><ul class="small">${m.derived.map((u) => `<li><a href="#/memory/${u.id}">${esc(u.content)}</a> · ${esc(MSTATUS[u.status] || u.status)}</li>`).join("")}</ul>` : ""}
    <h3>变化记录</h3><ol class="timeline">${m.events.map((e) => `<li class="ok"><div class="step-head"><b>${esc(e.event)}</b><span>${esc(e.actor)} · ${fmtTime(e.created_at)}${e.task_id ? ` · <a href="#/task/${e.task_id}">${esc(e.task_id)}</a>` : ""}</span></div>
      ${e.detail && Object.keys(e.detail).length ? `<p class="obs">${esc(JSON.stringify(e.detail))}</p>` : ""}</li>`).join("")}</ol>`;
  if (alive) {
    $("#mem-edit").onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const r = await api(`/api/memories/${id}/correct`, { method: "POST", body: { content: f.get("content"), reason: f.get("reason") } });
      location.hash = `#/memory/${r.new}`;
    };
    $("#mem-del").onclick = async () => {
      const reason = prompt("删除原因（可空）") ?? null;
      if (reason === null) return;
      const r = await api(`/api/memories/${id}`, { method: "DELETE", body: { reason } });
      alert(`已删除；同步失效 ${r.invalidated.length} 条派生记忆`);
      showMemory(id);
    };
  }
}

// ---------------------------------------------------------------- 信息源
async function showSources() {
  const el = $("#detail");
  sourcesCache = await api("/api/sources");
  el.innerHTML = `
    <div class="detail-head"><div><p class="kicker">信息源</p><h1>采集来源与健康状况</h1></div></div>
    <table class="tbl"><tr><th>来源</th><th>方式</th><th>资料</th><th>最近状态</th><th></th></tr>
    ${sourcesCache.map((s) => {
      const h = s.health || {};
      const err = h.last_error;
      const okRecent = h.last_ok && (!h.last_fail || h.last_ok > h.last_fail);
      return `<tr><td><b>${esc(s.name)}</b><br><small>${esc(s.config?.feed || s.base_url || "")}</small></td>
        <td><span class="tag">${esc(s.method)}</span><br>${(s.config?.domains || []).map((d) => `<span class="tag">${DOMAIN_NAME[d] || d}</span>`).join(" ")}</td><td>${s.docs}</td>
        <td>${h.calls ? (okRecent ? `<span class="ok">正常</span> ${fmtTime(h.last_ok)}` : `<span class="gap">失败</span> ${esc(err?.kind || "")} ${esc(err?.reason || "")}<br><small>${fmtTime(h.last_fail)}，连续 ${h.consecutive_fail} 次</small>`) : "<small>未使用</small>"}</td>
        <td class="nowrap">${s.id !== "web" ? `<button class="ghost sm" data-test="${s.id}">测试</button>` : ""}
          <button class="ghost sm" data-toggle="${s.id}" data-on="${s.enabled}">${s.enabled ? "停用" : "启用"}</button></td></tr>
        <tr class="test-row" id="test-${s.id}" hidden><td colspan="5"></td></tr>`;
    }).join("")}</table>
    <form class="card" id="add-src"><h3>添加 RSS 来源</h3>
      <div class="row"><input name="name" placeholder="名称" required><input name="feed" placeholder="RSS 地址" required></div>
      <label class="opt"><input type="checkbox" name="full_text"> RSS 内含全文</label>
      <label class="opt"><input type="checkbox" name="browser"> 文章页需要浏览器渲染</label>
      <input name="search_feed" placeholder="可选：站内搜索 RSS 模板，用 {q} 表示关键词，如 https://example.com/?s={q}&feed=rss2" style="width:100%;padding:6px;margin:4px 0">
      <div class="row"><small>领域：</small>${Object.entries(DOMAIN_NAME).map(([k, v]) => `<label class="opt"><input type="checkbox" name="domains" value="${k}" ${k === "general" ? "checked" : ""}> ${v}</label>`).join("")}</div>
      <select name="lang"><option value="zh">中文</option><option value="en">英文</option></select>
      <button>添加</button></form>`;
  $$("[data-test]").forEach((b) => b.onclick = async () => {
    const row = $(`#test-${b.dataset.test}`);
    row.hidden = false;
    row.firstElementChild.textContent = "测试中…";
    const r = await api(`/api/sources/${b.dataset.test}/test`, { method: "POST", body: {} });
    row.firstElementChild.innerHTML = r.ok
      ? `<span class="ok">可用</span> ${r.ms}ms，返回 ${r.count} 条：${r.sample.map((x) => `<br>· ${esc(x.title)} <small>${KIND[x.kind]}</small>`).join("")}`
      : `<span class="gap">失败</span> ${esc(r.error.kind)}：${esc(r.error.reason)}（${r.ms}ms）`;
  });
  $$("[data-toggle]").forEach((b) => b.onclick = async () => {
    await api(`/api/sources/${b.dataset.toggle}`, { method: "PATCH", body: { enabled: b.dataset.on !== "1" } });
    showSources();
  });
  $("#add-src").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/sources", { method: "POST", body: { name: f.get("name"), feed: f.get("feed"),
        full_text: !!f.get("full_text"), browser: !!f.get("browser"), lang: f.get("lang"),
        domains: f.getAll("domains"), search_feed: f.get("search_feed") } });
      showSources();
    } catch (err) { alert(err.message); }
  };
}

// ---------------------------------------------------------------- 设置
async function showSettings() {
  const el = $("#detail");
  const s = await api("/api/settings/llm");
  el.innerHTML = `
    <div class="detail-head"><div><p class="kicker">设置</p><h1>大模型接口</h1></div></div>
    <form class="card" id="llm-form">
      <p class="small">任何 OpenAI 兼容接口都可以，例如 DeepSeek：<code>https://api.deepseek.com/v1</code> + <code>deepseek-chat</code>；
      通义千问：<code>https://dashscope.aliyuncs.com/compatible-mode/v1</code> + <code>qwen-plus</code>。不配置时用规则后备，流程照常运行。</p>
      <label>Base URL<input name="base_url" value="${esc(s.base_url)}" placeholder="https://api.deepseek.com/v1"></label>
      <label>API Key<input name="api_key" type="password" value="${esc(s.api_key)}" placeholder="sk-..."></label>
      <label>模型<input name="model" value="${esc(s.model)}" placeholder="deepseek-chat"></label>
      <div class="row"><button>保存</button><button type="button" class="ghost" id="llm-test">测试连接</button>
      <span class="${s.available ? "ok" : "gap"}">${s.available ? "已配置" : "未配置"}</span></div>
      <p id="llm-result" class="small"></p>
    </form>
    <div class="card" id="replay-card"></div>`;
  api("/api/replay").then((r) => {
    const c = $("#replay-card");
    if (!r.enabled) { c.innerHTML = `<h3>固定样例回放</h3><p class="small">用 <code>bash start.command --replay</code> 启动可进入离线回放模式（模拟网站、独立数据库），用于演示长期记忆。</p>`; return; }
    c.innerHTML = `<h3>固定样例回放（当前第 ${r.day} 天）</h3><p class="small">第 2 天：IT之家把 128K 更正为 64K，并出现一篇 256K 新进展；Hacker News 不可用。</p>
      <div class="row"><button class="ghost" data-day="1">切到第 1 天</button><button class="ghost" data-day="2">切到第 2 天</button></div>`;
    $$("[data-day]", c).forEach((b) => b.onclick = async () => { await api("/api/replay/day", { method: "POST", body: { day: +b.dataset.day } }); showSettings(); });
  });
  $("#llm-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target).entries());
    await api("/api/settings/llm", { method: "POST", body: f });
    showSettings();
  };
  $("#llm-test").onclick = async () => {
    $("#llm-result").textContent = "测试中…";
    const r = await api("/api/settings/llm/test", { method: "POST", body: {} });
    $("#llm-result").textContent = r.ok ? `连接成功：${r.reply}` : `失败：${r.error}`;
  };
}

init();
