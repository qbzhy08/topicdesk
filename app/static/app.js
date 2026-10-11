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
const GRAPH_NAME = { write: "写作", research: "主题检索", import: "链接导入", recheck: "复查", reassess: "重新评估", demo: "演示" };
const taskTitle = (t) => ({ recheck: "复查 · ", reassess: "重新评估 · " }[t.graph] || "") + (t.topic === "*" ? "全部主题" : t.topic);
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
  await loadDomains();
  setupTagPop();
  await loadSourceChecks();
  $("#new-task").addEventListener("submit", submitTask);
  const ta = $("#new-task").topic;
  ta.addEventListener("input", () => autoGrow(ta));
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#new-task").requestSubmit(); }
  });
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
    sources: allChecked() ? undefined : f.getAll("src"), max_docs: Number(f.get("max_docs")), max_fetch: Number(f.get("max_fetch")), confirm_aspects: !!f.get("confirm_aspects"),
    days: Number(f.get("days")), min_relevance: Number(f.get("min_relevance")),
    hitl_on_block: !!f.get("hitl_on_block"), ...extra } : extra;
  const form = e.target;
  form.classList.add("sending");
  const hero = $(".hero");
  if (hero) hero.classList.add("leaving");
  try {
    const [{ id }] = await Promise.all([
      api("/api/tasks", { method: "POST", body: { topic: f.get("topic").trim(), graph, params, budget } }),
      new Promise((r) => setTimeout(r, 650))]);          // 让过渡动效走完
    form.reset();
    kwState.edited = { keywords: false, en_keywords: false };
    kwState.lastTopic = "";
    $("#kw-note").textContent = "";
    sessionStorage.setItem("td:justStarted", id);
    location.hash = `#/task/${id}`;
    refreshList();
  } catch (err) {
    form.classList.remove("sending");
    if (hero) hero.classList.remove("leaving");
    alert(err.message);
  }
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
// 记住上次打开的任务和榜单分页：从“信息源”“记忆”等页面回到“任务”时恢复（浏览器本地保存，失败不影响使用）
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem("td:" + k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem("td:" + k, JSON.stringify(v)); } catch { /* 隐私模式等 */ } },
};

let routedOnce = false;

function route() {
  if (current?.timer) clearTimeout(current.timer);
  if (current?.liveTimer) clearInterval(current.liveTimer);
  current = null;
  const form = $("#new-task");
  if (form && !$("#composer-holder").contains(form)) $("#composer-holder").append(form);   // 离开前收回，保留事件和已填内容
  const h = location.hash || "#/";
  const firstLoad = !routedOnce;
  routedOnce = true;
  if (firstLoad && h.startsWith("#/task/")) {     // 打开或刷新网页：一律从新主题开始（上次任务在左栏“最近任务”里）
    location.replace("#/new");
    return;
  }
  // 新打开网页：显示新主题输入框；在应用内从其他页面点回“任务”：恢复上次的任务
  if (h === "#/" && !firstLoad && store.get("lastTask")) {
    location.replace(`#/task/${store.get("lastTask")}`);
    return;
  }
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
  const ar = h.match(/^#\/article\/(\w+)(?:\?v=(\d+))?/);
  if (ar) return showArticle(ar[1], ar[2]);
  if (h.startsWith("#/memory/demo")) return showMemoryDemo();
  if (h.startsWith("#/memory")) return showMemoryHome(new URLSearchParams(h.split("?")[1] || ""));
  if (h.startsWith("#/sources")) return showSources();
  if (h.startsWith("#/settings")) return showSettings();
  showComposer();
}

// ---------------------------------------------------------------- 运行中：告诉用户此刻在做什么
const PHASE = { plan: "正在理解主题、拆分子问题", decide: "正在决定下一步检索", act: "正在检索与抓取资料",
  judge: "大模型正在复核相关度", images: "正在挑选配图", remember: "正在核验事实、写入记忆", rank: "正在做价值评估与排序",
  verify: "正在补充核验可疑资料", report: "正在生成报告", recheck: "正在复查页面", reassess: "正在重新评估",
  import_fetch: "正在抓取链接", import_images: "正在处理图片",
  load: "正在读取 Skill 和写作记忆", select: "正在按 Skill 选材", outline: "正在列提纲", gap: "资料有缺口，正在补充检索",
  draft: "正在逐节写作", check: "正在逐句核对出处和数字", fix: "正在按核对结果改写", illustrate: "正在配图：选图、质检、找位置",
  save: "正在保存文章" };

function renderLive(t) {
  const bar = $("[data-f=live]");
  if (!bar) return;
  const active = ["pending", "running", "cancelling"].includes(t.status) || t.running;
  bar.hidden = !active;
  if (!active) { if (current?.liveTimer) { clearInterval(current.liveTimer); current.liveTimer = null; } return; }
  const node = t.current_node && t.current_node !== "__end__" ? t.current_node : "plan";
  $("[data-f=live-text]", bar).textContent = t.status === "pending" ? "排队中，马上开始…" :
    t.status === "cancelling" ? "正在取消…" : (PHASE[node] || `正在执行 ${node}`) + "…";
  const base = (t.usage?.seconds || 0), t0 = Date.now();
  if (current) {
    if (current.liveTimer) clearInterval(current.liveTimer);
    const tick = () => { const el = $("[data-f=live-time]"); if (el) el.textContent = `${Math.round(base + (Date.now() - t0) / 1000)}s`; };
    tick();
    current.liveTimer = setInterval(tick, 1000);
  }
}

// ---------------------------------------------------------------- 新主题（居中大输入框）
const GREETINGS = ["今天想追踪什么？", "有什么值得深挖的主题？", "想读懂哪个话题？"];
// 测试话题：覆盖不同领域，标注各自适合测什么（点击后填入主题并勾选对应领域）
const EXAMPLES = [
  { t: "开源大模型的长上下文评测", d: "tech", why: "论文 + 评测数据 · 专题解读" },
  { t: "固态电池量产进展", d: "auto", why: "时间线多 · 测示意图" },
  { t: "GLP-1 减重药的长期安全性", d: "health", why: "学术 + 新闻 · 测出处核对" },
  { t: "A 股量化监管新规影响", d: "finance", why: "传闻与回应 · 观点交锋" },
  { t: "人形机器人量产与订单", d: "tech", why: "产品图多 · 测配图" },
  { t: "新能源汽车价格战", d: "auto", why: "数字密集 · 测柱状图" },
  { t: "司美格鲁肽国内仿制药上市", d: "health", why: "专利到期事件 · 测时间线" },
  { t: "AI 眼镜值不值得买", d: "consumer", why: "测评 + 用户口碑 · 测社区来源" },
  { t: "现制茶饮价格战与加盟扩张", d: "consumer", why: "商业新闻 + 社交平台" },
  { t: "国产 GPU 替代进展", d: "tech", why: "数据与争议并存" },
  { t: "低空经济与 eVTOL 商业化", d: "general", why: "政策 + 企业动态" },
  { t: "国产买断制单机游戏出海", d: "game", why: "销量数据 + 玩家讨论" },
  { t: "外卖平台补贴大战", d: "consumer", why: "多方表态 · 观点交锋" },
  { t: "大模型 API 降价潮", d: "tech", why: "价格数字多 · 测柱状图" },
];
let exampleStart = 0;

function showComposer() {
  $$("#task-list a").forEach((a) => a.classList.remove("active"));
  const hour = new Date().getHours();
  const hello = hour < 6 ? "夜深了" : hour < 11 ? "早上好" : hour < 14 ? "中午好" : hour < 18 ? "下午好" : "晚上好";
  const el = $("#detail");
  el.innerHTML = `<div class="hero">
      <p class="hero-hello"><span class="spark big">✳</span>${hello}</p>
      <h1 class="hero-title">${GREETINGS[Math.floor(Math.random() * GREETINGS.length)]}</h1>
      <div class="hero-slot"></div>
      <div class="hero-examples" id="hero-examples"></div>
      <p class="hero-foot">TopicDesk 会自动拆分子问题、从 20+ 个来源检索、去重核验、按价值排序，并记住你的偏好和上次的结论。</p>
    </div>`;
  const form = $("#new-task");
  form.classList.remove("sending");
  $(".hero-slot", el).append(form);
  const ta = form.topic;
  const box = $("#hero-examples", el);
  const drawExamples = () => {
    const n = 6, pick = [];
    for (let i = 0; i < n; i++) pick.push(EXAMPLES[(exampleStart + i) % EXAMPLES.length]);
    box.innerHTML = pick.map((x) => `<button type="button" class="example" data-t="${esc(x.t)}" data-d="${esc(x.d)}" title="${esc(x.why)}">
        ${esc(x.t)}<small>${esc(x.why)}</small></button>`).join("") +
      `<button type="button" class="example more" id="ex-more">换一批 ↻</button>`;
    $$(".example[data-t]", box).forEach((b) => b.onclick = () => {
      ta.value = b.dataset.t;
      if (DOMAIN_NAME[b.dataset.d]) renderDomainChips(b.dataset.d, true);   // 只勾这个话题的领域
      autoGrow(ta); ta.focus(); translateKeywords(false);
    });
    $("#ex-more", box).onclick = () => { exampleStart = (exampleStart + n) % EXAMPLES.length; drawExamples(); };
  };
  drawExamples();
  autoGrow(ta);
  setTimeout(() => ta.focus(), 50);
}

function autoGrow(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 240) + "px";
}

// ---------------------------------------------------------------- 任务详情
function showTask(id) {
  store.set("lastTask", id);
  current = { id, lastStepId: 0, timer: null, rkTab: (store.get("rkTabs") || {})[id] || null };
  const el = $("#detail");
  el.innerHTML = "";
  let fresh = false;
  try { fresh = sessionStorage.getItem("td:justStarted") === id; sessionStorage.removeItem("td:justStarted"); } catch { /* */ }
  el.classList.toggle("arrive", fresh);
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
    if (/not found/i.test(err.message) && store.get("lastTask") === current?.id) {
      store.set("lastTask", null);          // 任务已不存在：不再自动打开它
      location.replace("#/new");
      return;
    }
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
  renderLive(t);
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
  if (g && !sel.options.length) {
    sel.innerHTML = g.nodes.map((n) => `<option value="${esc(n)}">${esc(NODE_NAME[n] || n)}（${esc(n)}）</option>`).join("");
    $("[data-f=rewind-eg]", d).innerHTML = (REWIND_EG[t.graph] || []).map((x) => `<li>${x}</li>`).join("");
  }
  $("[data-f=state]", d).textContent = JSON.stringify(t.state, null, 2);
  renderHitl(t.hitl.filter((h) => h.status === "open"));
  // 写作入口先渲染：报告或榜单渲染出错时不能连带它也不显示
  if (t.graph === "research" && t.status === "done") loadWritePanel(t).catch((err) => showPanelError("write", "写作入口", err));
  if (t.graph === "write") renderWriteTask(t);
  try { renderReport(t.state.report, t.state); } catch (err) { showPanelError("report", "采集报告", err); }
  if (t.graph === "research" && t.status === "done") loadRanking().catch((err) => showPanelError("ranking", "价值排序", err));
}

function showPanelError(f, name, err) {
  console.error(err);
  const box = $(`[data-f=${f}]`);
  if (box) box.innerHTML = `<div class="card"><p class="gap">${name}显示出错：${esc(err?.message || String(err))}</p>
    <p class="small muted">请截图发给开发者。其他部分不受影响。</p></div>`;
  if (current && f === "write") current.writeLoaded = null;
}

const NODE_NAME = { plan: "理解主题、拆子问题", decide: "决定下一步", act: "检索与抓取", judge: "复核相关度", images: "挑选配图",
  remember: "核验事实、写入记忆", rank: "价值排序", verify: "补充核验", report: "生成报告",
  load: "读取 Skill 和记忆", select: "选材", outline: "列提纲", gap: "补充检索", draft: "逐节写作", check: "逐句核对",
  fix: "按核对结果改写", illustrate: "配图", save: "保存文章", recheck: "复查页面", reassess: "重新评估" };
const REWIND_EG = {
  write: ["改了 Skill 文件或写作偏好 → 从“读取 Skill 和记忆”重跑：按新的要求重新选材、列提纲、写作",
    "检索任务后来补充或重评了资料 → 从“选材”重跑，用新的素材重写",
    "写作中途失败（比如大模型超时）→ 从失败的那一步接着跑，前面的步骤不重做"],
  research: ["改了长期偏好（关注重点、排除内容）→ 从“理解主题、拆子问题”重跑，抓过的网页直接复用",
    "只想重新生成报告 → 从“生成报告”重跑",
    "任务中途失败 → 从失败的那一步接着跑，不用从头检索"],
};

// ---------------------------------------------------------------- Lv3：写作入口
let skillsCache = null;
async function loadWritePanel(t) {
  if (!current || current.writeLoaded === t.id) return;
  current.writeLoaded = t.id;
  const box = $("[data-f=write]");
  const [sk, arts] = await Promise.all([skillsCache || api("/api/skills"), api(`/api/articles?research_task=${t.id}`)]);
  skillsCache = sk;
  if (current?.id !== t.id) return;
  const pick = store.get("skill") || sk.skills[0]?.id;
  box.innerHTML = `<div class="card write-panel"><h3>写成文章</h3>
    <p class="small">选一种写法（Skill）。同一组资料，不同 Skill 的选材、结构和表达都不一样。Skill 是 <code>skills/</code> 文件夹里的文件，新增一个文件就多一种写法。</p>
    <div class="skill-grid">${sk.skills.map((x) => `<label class="skill-card ${x.id === pick ? "on" : ""}">
      <input type="radio" name="skill" value="${esc(x.id)}" ${x.id === pick ? "checked" : ""}>
      <b>${esc(x.name)}</b> <small class="muted">v${esc(x.version)}</small>
      <span class="small">${esc(x.summary)}</span>
      <span class="small muted">读者：${esc(x.reader)}</span>
      <span class="small muted">${esc(x.length)} · ${x.material.list === "5" ? "五分钟清单" : "三十分钟清单"}选材，最多 ${x.material.max_docs} 篇</span>
      <span class="sk-struct">${x.structure.map((s) => `<i>${esc(s.heading)}</i>`).join("")}</span></label>`).join("")}</div>
    ${sk.errors?.length ? `<p class="small gap">有 Skill 文件格式不对，没有加载：${sk.errors.map(esc).join("；")}</p>` : ""}
    <div class="row"><button id="write-go">开始写作</button><small class="muted" id="write-msg"></small></div>
    ${arts.length ? `<h4>已写的文章</h4><div class="art-list">${arts.map((a) => `<a class="art-item" href="#/article/${a.id}">
      <span><b>${esc(a.title || "（写作中）")}</b><small class="muted">${esc(a.skill_name || a.skill)} · 第 ${a.current_version} 版 · ${fmtTime(a.updated_at)}${a.support_rate != null ? ` · 出处支持率 ${Math.round(a.support_rate * 100)}%` : ""}</small></span>
      <span class="btn primary">打开 →</span></a>`).join("")}</div>` : ""}
  </div>`;
  $$(".skill-card input", box).forEach((r) => r.onchange = () => {
    $$(".skill-card", box).forEach((c) => c.classList.toggle("on", c.contains(r) && r.checked));
    store.set("skill", r.value);
  });
  $("#write-go").onclick = async (e) => {
    e.target.disabled = true;
    const skill = $(".skill-card input:checked", box)?.value;
    try {
      const r = await api(`/api/tasks/${t.id}/write`, { method: "POST", body: { skill } });
      location.hash = `#/task/${r.id}`;
    } catch (err) { $("#write-msg").textContent = err.message; e.target.disabled = false; }
  };
}

function renderWriteTask(t) {
  const box = $("[data-f=write]");
  const st = t.state || {};
  const res = st.result;
  const sig = JSON.stringify([t.status, res?.version, st.title]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  const s = st.check?.summary;
  box.innerHTML = `<div class="card write-panel">
    <p class="small"><a href="#/task/${esc(t.params?.research_task || "")}">← 回到检索任务</a>${st.skill ? ` · Skill「${esc(st.skill.name)}」v${esc(st.skill.version)}` : ""}</p>
    ${res ? `<div class="cta"><div class="cta-text"><p class="kicker">文章已写好 · 第 ${res.version} 版</p><h3>${esc(st.title)}</h3>
      <p class="chips"><span class="chip">素材 <b>${st.docs?.length || 0}</b></span><span class="chip">出处支持率 <b>${s?.support_rate != null ? Math.round(s.support_rate * 100) + "%" : "—"}</b></span>
      <span class="chip">自动改正 <b>${(st.fixes || []).length}</b></span><span class="chip">补充检索 <b>${(st.gap_log || []).filter((g) => !g.skipped).length}</b></span><span class="chip">待你确认 <b>${s?.problems ?? 0}</b></span></p></div>
      <a class="btn primary big" href="#/article/${res.article_id}">打开文章 →</a></div>`
    : `<p class="small">${st.title ? `正在写《${esc(st.title)}》` : "正在准备写作"}，完成后这里会出现文章链接。</p>`}</div>`;
}

function recheckReport(rep) {
  const OUT = { updated: "有更新（已存为新版本）", unchanged: "无变化", enriched: "补全了正文", failed: "抓取失败" };
  const items = rep.items || [];
  const changeTxt = (c) => !c ? "" : [
    c.numbers_removed?.length || c.numbers_added?.length ? `数字 ${esc((c.numbers_removed || []).join("、") || "—")} → ${esc((c.numbers_added || []).join("、") || "—")}` : "",
    c.sentences_added?.length ? `新增：${esc(c.sentences_added[0].slice(0, 60))}…` : "",
    c.sentences_removed?.length ? `删除：${esc(c.sentences_removed[0].slice(0, 60))}…` : ""].filter(Boolean).join("<br>");
  return `<div class="card report"><h3>复查结果</h3>
    <p class="small">范围：${esc(rep.scope)} · ${rep.force ? "手动复查（范围内所有可能变化的页面）" : "定时复查（只查已到期的页面）"}
      ${rep.by_kind ? ` · 代码仓库 ${rep.by_kind.github}、论文 ${rep.by_kind.arxiv}、网页 ${rep.by_kind.web}；实际打开网页 ${rep.opened_pages} 个，其余通过接口确认未变` : ""}</p>
    <div class="stats">
      <div><b>${rep.checked}</b><small>已复查</small></div>
      <div><b>${rep.summary?.updated || 0}</b><small>有更新</small></div>
      <div><b>${rep.summary?.unchanged || 0}</b><small>无变化</small></div>
      <div><b>${rep.summary?.failed || 0}</b><small>失败</small></div>
    </div>
    ${items.length ? `<table class="tbl"><tr><th>页面</th><th class="col-why">为什么复查</th><th class="col-out">结果</th></tr>${items.map((r) => `
      <tr><td><a href="#/doc/${r.new_doc_id || r.doc_id}">${esc(r.title || r.url)}</a></td><td class="small col-why">${esc(r.why || "")}</td>
      <td><span class="tag ${r.outcome === "updated" ? "nv-update" : ""}">${OUT[r.outcome] || esc(r.outcome)}</span>
      ${r.version ? ` v${r.version}` : ""}${r.note ? `<br><small>${esc(r.note)}</small>` : ""}${r.error ? `<br><small>${esc(r.error)}</small>` : ""}
      ${r.change ? `<br><small>${changeTxt(r.change)}</small>` : ""}</td></tr>`).join("")}</table>`
    : `<p class="empty">这个范围内没有需要复查的页面。只有代码仓库、arXiv 论文和 3 天内发布的新闻会被安排复查。</p>`}
    <p class="small">范围内仍在复查计划中的页面 ${rep.pending} 篇${rep.next_due ? `，下次到期 ${fmtTime(rep.next_due)}` : ""}。</p></div>`;
}

function renderReport(rep, state) {
  const box = $("[data-f=report]");
  if (rep?.kind === "recheck") { box.innerHTML = recheckReport(rep); return; }
  if (rep?.kind === "reassess") {
    box.innerHTML = `<div class="card report"><h3>重新评估</h3><p>已为任务生成第 ${rep.version} 版评估（${esc(rep.trigger)}）。
      <a href="#/task/${rep.task}">回到原任务查看价值榜单</a></p>
      ${rep.verify?.length ? `<ul class="small">${rep.verify.map((r) => `<li>「${esc(r.title)}」：${r.outcome === "corroborated" ? "找到交叉印证" : "未找到印证，标注待核实"}</li>`).join("")}</ul>` : ""}</div>`;
    return;
  }
  if (!rep) {
    const cov = state?.coverage;
    box.innerHTML = cov ? `<div class="card"><h3>覆盖矩阵（进行中）</h3>${covTable(cov)}</div>` : "";
    return;
  }
  const chips = (obj, map = {}) => Object.entries(obj || {}).map(([k, v]) => `<span class="chip">${esc(map[k] || k)} <b>${v}</b></span>`).join("");
  box.innerHTML = `${rep.compare ? compareCard(rep.compare) : ""}
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
      ${rep.dynamic && !rep.dynamic.rendered && !rep.dynamic.failed ? `<p class="small"><b>动态页面</b>：本次没有遇到需要浏览器渲染的页面。<a href="#/library?how=browser">查看资料库里所有浏览器渲染过的资料</a></p>` : ""}
      ${rep.dynamic && (rep.dynamic.rendered || rep.dynamic.failed) ? `<p class="small"><b>动态页面</b>：${rep.dynamic.rendered} 篇普通抓取拿不到正文，改用浏览器渲染后成功${rep.dynamic.failed ? `；${rep.dynamic.failed} 篇浏览器也被拦截，保留摘要（可在资料页粘贴正文）` : ""}。
        ${rep.dynamic.example ? `例子：<a href="#/doc/${rep.dynamic.example.id}">${esc((rep.dynamic.example.title || "").slice(0, 40))}</a>` : ""}</p>` : ""}
      ${aspectPlanReport(state?.aspect_plan)}
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
  invalid: "已失效", resolved: "已裁决", archived: "不再参考" };
const mstatus = (m) => m.kind === "delivery" && m.status === "stale" ? "部分结论已过期" : (MSTATUS[m.status] || m.status);

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

// ---------------------------------------------------------------- 与上次相比（同一主题的两次独立任务）
const PREF_FROM = { long_term: "长期偏好", this_time: "本次要求" };
const PREF_OP = { added: "新增", changed: "修改", removed: "移除" };
function compareCard(c, opts = {}) {
  const prefs = (c.pref_effects || []).length ? `<div class="cmp-sec"><h4>偏好如何影响结果</h4><ul class="small">${c.pref_effects.map((e) => `
      <li><b>${esc(e.name)}：${esc(e.value)}</b> <span class="tag">${PREF_FROM[e.from] || esc(e.from)}</span> → ${esc(e.effect)}
      ${e.examples?.length ? `<br><small class="muted">被排除：${e.examples.map(esc).join("；")}</small>` : ""}</li>`).join("")}</ul>
      ${(c.pref_changes || []).length ? `<p class="small muted">和上次相比：${c.pref_changes.map((x) => `${PREF_OP[x.op]}「${esc(x.name)}」${x.op === "changed" ? `${esc(x.before)} → ${esc(x.after)}` : esc(x.after || x.before || "")}`).join("；")}</p>` : ""}</div>` : "";
  if (!c.prev) {
    return `<div class="card cmp first"><h3>与上次相比</h3><p class="small">这是该主题的第一次任务。下次再查同一主题时，这里会列出重复资料、新进展、被更正的旧结论，以及删除或更正过的记忆是否还被召回。</p>${prefs}</div>`;
  }
  const d = c.docs || {};
  const link = (x) => `<a href="#/doc/${x.id}">${esc(x.title)}</a>`;
  const checks = (c.checks || []).map((k) => `<div class="cmp-check ${k.ok ? "ok" : "bad"}"><b>${k.ok ? "✓" : "✗"} ${esc(k.name)}</b><small>${esc(k.detail)}</small></div>`).join("");
  return `<div class="card cmp">
    <div class="cmp-head"><h3>与上次相比</h3>
      <small>上次 ${fmtTime(c.prev.at)}（${c.prev.docs} 篇资料）${opts.noLink ? "" : ` · <a href="#/task/${c.prev.id}">打开上次任务</a>`}</small></div>
    <div class="cmp-checks">${checks}</div>
    <div class="cmp-grid">
      <div class="cmp-sec"><h4>资料</h4>
        <div class="cmp-nums"><div><b>${d.new ?? 0}</b><small>新进展</small></div><div><b>${d.updated ?? 0}</b><small>内容更新</small></div>
          <div><b>${d.repeated ?? 0}</b><small>上次已有</small></div><div><b>${d.reprints ?? 0}</b><small>转载/重复报道</small></div></div>
        ${d.new_items?.length ? `<p class="small"><span class="tag nv-new">新</span> ${d.new_items.map(link).join("；")}</p>` : ""}
        ${d.updated_items?.length ? `<p class="small"><span class="tag nv-update">更新</span> ${d.updated_items.map(link).join("；")}</p>` : ""}
        ${d.repeated_titles?.length ? `<details class="small"><summary>上次已有、只做比对的 ${d.repeated} 篇</summary>${d.repeated_titles.map(esc).join("；")}${d.repeated > d.repeated_titles.length ? " …" : ""}</details>` : ""}
      </div>
      <div class="cmp-sec"><h4>旧结论更正</h4>
        ${(c.corrections || []).map((x) => `<p class="small"><s>${esc(x.old)}</s><br>→ <b>${esc(x.new)}</b></p>`).join("") || '<p class="small muted">本次没有来源更正</p>'}
        ${c.prev.delivery_status && c.prev.delivery_status !== "active" ? `<p class="small">上次的交付：<span class="tag st-${c.prev.delivery_status}">${MSTATUS[c.prev.delivery_status] || c.prev.delivery_status}</span> ${esc((c.prev.delivery_note || "").replace(/（上游 m_\w+）/, ""))}</p>` : ""}
      </div>
      ${prefs}
      <div class="cmp-sec"><h4>删除 / 更正过的记忆</h4>
        ${(c.invalidated || []).map((x) => `<p class="small"><span class="tag">${esc(x.event)}</span> ${x.actor === "user" ? "你" : "系统"}：<s>${esc(x.content)}</s>${x.reason && x.actor === "user" ? `<br><small class="muted">原因：${esc(x.reason.replace(/（上游 m_\w+）/, ""))}</small>` : ""}
          ${x.replacement ? `<br>接替为：${esc(x.replacement)}${x.replacement_recalled ? "（本次召回的是这条）" : ""}` : ""}
          <br>${x.recalled ? '<span class="gap">✗ 本次仍被召回</span>' : '<span class="ok">✓ 本次没有召回</span>'}</p>`).join("") || '<p class="small muted">两次任务之间没有删除或更正记忆</p>'}
      </div>
    </div>
    <details class="small"><summary>本次召回的 ${c.recalled} 条记忆</summary><ul>${(c.recalled_lines || []).map((l) => `<li>${esc(l)}</li>`).join("")}</ul></details>
  </div>`;
}

const ORIGIN = { default: "通用", llm: "按类型拆分", data: "按资料新增", user: "你添加", community: "社区话题", memory: "沿用上次" };
function covTable(cov) {
  return `<table class="tbl cov"><tr><th>子问题</th><th>来源</th><th>已有 / 需要</th><th></th></tr>${Object.values(cov).map((c) => `
    <tr class="${c.optional ? "opt-row" : ""}"><td>${esc(c.name)}${c.priority === "high" ? ` <span class="tag">必需</span>` : ""}${c.why ? `<br><small>${esc(c.why)}</small>` : ""}</td>
    <td><span class="tag">${ORIGIN[c.origin] || esc(c.origin || "通用")}</span></td>
    <td>${c.have} / ${c.optional ? "可选" : c.need}</td>
    <td>${c.optional ? '<span class="small">可选</span>' : c.have >= c.need ? '<span class="ok">✓ 已覆盖</span>' : '<span class="gap">缺口</span>'}</td></tr>`).join("")}</table>`;
}

function aspectPlanReport(ap) {
  if (!ap) return "";
  const OP = { drop_default: "去掉通用子问题", merge: "合并重复", optional: "降为可选", add: "按资料新增", keep_high: "保留必需项", add_rejected: "建议新增但未采纳",
    user_remove: "你删除", user_add: "你新增" };
  const ch = ap.changes || [];
  return `<h4>子问题拆分</h4><p class="small">问题类型：${esc(ap.type_name || "未判断（未配置大模型时只用通用子问题）")}
    ${ap.reused?.length ? `；沿用上次的子问题：${ap.reused.map(esc).join("、")}` : ""}${ap.refine_notes ? `；修订说明：${esc(ap.refine_notes)}` : ""}</p>
    ${ch.length ? `<ul class="small">${ch.map((c) => `<li><b>${OP[c.op] || c.op}</b>「${esc(c.name)}」${c.into ? ` → 并入「${esc(c.into)}」` : ""}：${esc(c.why || "")}
      ${c.evidence_titles?.length ? `（依据：${c.evidence_titles.map(esc).join("；")}）` : ""}</li>`).join("")}</ul>` : ""}`;
}

function renderHitl(open) {
  const box = $("[data-f=hitl]");
  const key = "k:" + open.map((h) => h.id).join();
  if (box.dataset.key === key) return;        // 避免轮询时清掉用户正在填写的内容
  box.dataset.key = key;
  box.innerHTML = open.map((h) => {
    const opts = h.payload.options;
    if (h.kind === "aspects") {
      return `<div class="card hitl" data-id="${h.id}"><h3>需要你的判断 · 确认子问题</h3><p>${esc(h.prompt)}</p>
        ${h.payload.aspects.map((a) => `<div class="asp-row"><input type="checkbox" data-asp="${esc(a.id)}" ${a.id === "overview" ? "disabled" : ""} checked>
          <input class="asp-name" data-name="${esc(a.id)}" value="${esc(a.name)}"><span class="tag">${ORIGIN[a.origin] || esc(a.origin || "通用")}</span>
          ${a.why ? `<small>${esc(a.why)}</small>` : ""}</div>`).join("")}
        <label>新增子问题（每行一个）<textarea rows="3" data-add></textarea></label><button>确认并开始检索</button></div>`;
    }
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
      if (h.kind === "aspects") {
        const keep = [...card.querySelectorAll("[data-asp]")].filter((i) => i.checked).map((i) => i.dataset.asp);
        const rename = Object.fromEntries([...card.querySelectorAll("[data-name]")].map((i) => [i.dataset.name, i.value]));
        const add = card.querySelector("[data-add]").value.split("\n").map((x) => x.trim()).filter(Boolean);
        response = { keep, rename, add };
      } else if (h.kind === "choose" && Array.isArray(h.payload.options)) {
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
      <select name="how"><option value="">全部获取方式</option>${Object.entries(HOW_NAME).map(([k, v]) => `<option value="${k}" ${p.how === k ? "selected" : ""}>${v}</option>`).join("")}</select>
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
  api("/api/docs/methods?" + new URLSearchParams([...qs.entries()].filter(([k]) => k === "topic" || k === "task"))).then((cnt) => {
    $$("#lib-filter select[name=how] option").forEach((o) => { if (o.value) o.textContent = `${HOW_NAME[o.value]}（${cnt[o.value] || 0}）`; });
  }).catch(() => {});
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

const HOW_NAME = { browser: "浏览器渲染（动态页面）", html: "网页抓取", api: "官方 / 开放接口", rss: "RSS 订阅", websearch: "搜索 API", human: "人工粘贴" };

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

// ---------------------------------------------------------------- 采集过程卡片
// 只把会影响“信不信这份资料、要不要做点什么”的信息放在明面上：结果 → 关键判断 → 前后对比 → 代价 → 合规 → 需要你做的；
// 请求头、重试、原始记录等收进“技术细节”。
const KIND_FULL = { full: "全文", excerpt: "节选", summary: "摘要", title: "标题" };
const secs = (ms) => (ms == null ? "" : ms < 100 ? "<0.1 s" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
const ERR_KIND = { blocked: "被拦截（访问受限或验证页）", timeout: "超时", robots: "robots.txt 不允许", not_found: "页面不存在",
  network: "网络不可达", bad_content: "内容无法解析", rate_limit: "被限流", server: "服务器错误" };

// 没有打开网页的资料（接口、RSS 全文、搜索摘要、复用）：说明它是怎么来的、为什么不需要 / 没有打开网页
const API_WHY = {
  arxiv: ["论文摘要", "arXiv 官方接口直接返回完整摘要，论文页是静态页面，不需要再打开"],
  openalex: ["论文摘要", "OpenAlex 开放学术接口返回摘要和被引次数"],
  pubmed: ["医学论文摘要", "PubMed 官方接口（E-utilities）批量返回摘要，网页常有验证页，不打开"],
  github: ["仓库简介", "GitHub 官方接口返回简介、星标和最近推送时间；复查时也用接口比对，不打开网页"],
  hn: ["讨论帖", "Hacker News 接口只有标题和分数，原文另外追踪"],
  bilibili: ["视频简介", "B站公开搜索接口返回标题、简介、标签和播放量；视频内容本身需要语音转写"],
  juejin: ["文章摘要", "掘金站内搜索接口"], wallstcn: ["文章", "华尔街见闻站内搜索接口，直接返回全文"],
};

function originCard(d) {
  const m = d.meta || {};
  const src = d.source || {};
  const name = src.name || d.source_id;
  const chars = (d.text || "").length;
  const kept = (d.images || []).filter((i) => i.status === "kept").length;
  const full = d.content_kind === "full";
  let kind, line, badges = [], action = false, ok = true;
  if (m.extract_method === "human" || m.provided_by === "human") {
    kind = "人工提供"; line = "正文由你粘贴提供";
  } else if (src.method === "websearch" || src.method === "search") {
    kind = `${m.platform || name} · 搜索结果`;
    if (m.summary_only) { line = `${m.platform || name}的正文需要登录才能查看，平台条款也不允许抓取，所以只保留搜索摘要`; action = true; ok = false; }
    else if (!full) {
      const rel = m.relevance;
      line = rel != null && rel < 0.3 ? `标题和摘要与主题不太相关（相关度 ${rel.toFixed(2)}），没有打开原文，省下抓取时间`
        : "搜索结果只有摘要，本次没有打开原文（抓全文数量已达上限，或排在后面）";
      action = rel == null || rel >= 0.3; ok = false;
    } else line = "通过搜索找到，正文完整";
    badges.push("正规搜索 API");
  } else if (src.method === "api" || API_WHY[d.source_id]) {
    const [what, why] = API_WHY[d.source_id] || ["内容", `${name} 接口返回`];
    kind = `${name} · 官方/开放接口`; line = why;
    badges.push("官方 / 开放接口");
    return cardShell(d, kind, `${what} ${chars.toLocaleString()} 字${kept ? ` · 配图 ${kept} 张` : ""}`,
      `<span class="fc-ok">✓ ${what}完整</span>`, line, badges, false, m);
  } else if (src.method === "rss") {
    kind = `${name} · RSS 订阅`;
    line = full ? "RSS 订阅里已经包含全文，不需要打开网页" : "RSS 只有摘要，本次没有打开原文（相关度不够高，或抓全文数量已达上限）";
    action = !full; ok = full;
    badges.push("公开订阅源");
  } else { kind = name; line = "直接入库"; }
  if (m.reused_from) line += `；本次复用了 ${fmtTime(m.reused_from)} 抓到的全文，没有重新联网`;
  const status = ok ? `<span class="fc-ok">✓ ${full ? "已拿到全文" : "内容完整"}</span>` : `<span class="fc-warn">⚠ 只有${KIND_FULL[d.content_kind] || "摘要"}</span>`;
  return cardShell(d, kind, `${KIND_FULL[d.content_kind] || ""} ${chars.toLocaleString()} 字${kept ? ` · 配图 ${kept} 张` : ""}`, status, line, badges, action, m);
}

function cardShell(d, kind, result, status, line, badges, action, m) {
  return `<section class="card fetch-card">
    <header><h3>采集过程</h3>${status}</header>
    <p class="fc-kind">${esc(kind)}</p>
    <p class="fc-result">${result}</p>
    <ol class="fc-steps"><li class="ok one"><b>获取方式</b><span>${esc(line)}</span></li></ol>
    <p class="fc-badges">${badges.map((b) => `<span class="tag">${esc(b)}</span>`).join("")}<span class="tag">未登录</span><span class="tag">未绕过验证码</span></p>
    ${action ? pasteBox(d, false) : ""}
    <details class="fc-tech"><summary>查看技术细节</summary><pre>${esc(JSON.stringify({ source: d.source_id, method: d.source?.method, extract_method: m.extract_method,
      via: m.via, relevance: m.relevance, platform: m.platform, reused_from: m.reused_from }, null, 1))}</pre></details>
  </section>`;
}

function pasteBox(d, dynamic) {
  return `<div class="fc-action"><p><b>需要你帮忙：</b>${dynamic ? "浏览器也没能打开正文。" : ""}已保留${KIND_FULL[d.content_kind] || "摘要"}；
      如果这篇重要，打开<a href="${esc(d.url)}" target="_blank" rel="noopener">原文</a>，把正文粘贴到这里：</p>
      <textarea rows="4" data-paste placeholder="粘贴正文（至少 50 字）"></textarea>
      <button class="ghost sm" data-paste-btn="${esc(d.id)}">保存为全文</button> <small data-paste-msg></small></div>`;
}

function fetchCard(d) {
  const m = d.meta || {};
  const att = m.fetch_attempts || [];
  if (!att.length) return originCard(d);
  const html = att.find((a) => a.mode === "html");
  const br = att.find((a) => a.mode === "browser");
  const human = att.find((a) => a.mode === "human");
  const kept = (d.images || []).filter((i) => i.status === "kept").length;
  const full = d.content_kind === "full";
  const chars = (d.text || "").length;
  const before = m.before_fetch;
  const dynamic = !!br;
  const status = full ? `<span class="fc-ok">✓ 已拿到全文</span>` : `<span class="fc-warn">⚠ 只有${KIND_FULL[d.content_kind] || "摘要"}</span>`;
  const kindLabel = human ? "人工提供正文" : dynamic ? (br.ok ? "前端渲染页面" : "动态页面 · 抓取受阻") : "静态页面";

  // ① 结果
  const result = `${KIND_FULL[d.content_kind] || ""} ${chars.toLocaleString()} 字${kept ? ` · 配图 ${kept} 张` : ""}`
    + (before && before.chars < chars ? `<span class="fc-before">（抓取前只有 ${before.chars} 字${KIND_FULL[before.kind] || "摘要"}）</span>` : "");

  // ② 关键判断：每一步一行
  const steps = [];
  if (html) {
    const why = !html.ok ? `失败：${ERR_KIND[html.kind] || html.kind || ""}` :
      dynamic ? (html.probe ? `只有 ${html.chars} 字（该站已知为前端渲染，直接用浏览器）` : `只有 ${html.chars} 字，正文太短 → 改用浏览器打开`)
        : `${html.chars} 字，正文完整`;
    steps.push(`<li class="${html.ok && !dynamic ? "ok" : dynamic && html.ok ? "turn" : "bad"}"><b>普通抓取</b><span class="fc-t">${html.cache ? "缓存" : secs(html.elapsed_ms)}</span><span>${esc(why)}</span></li>`);
  }
  if (br) {
    steps.push(`<li class="${br.ok ? "ok" : "bad"}"><b>浏览器渲染</b><span class="fc-t">${secs(br.elapsed_ms)}</span><span>${br.ok ? `${br.chars} 字 ✓` : `失败：${esc(br.reason ? br.reason.replace(/^浏览器/, "").slice(0, 40) : (ERR_KIND[br.kind] || br.kind || ""))}`}</span></li>`);
  }
  if (human) steps.push(`<li class="ok"><b>人工粘贴</b><span class="fc-t"></span><span>${human.chars} 字 ✓</span></li>`);

  // ③ 前后对比：只在“换了方式且换成功”时才有意义
  const compare = dynamic && br.ok && html?.ok && html.preview != null ? `<div class="fc-compare">
      <div><small>普通抓取看到的（${html.chars} 字）</small><p>${esc(html.preview || "（几乎没有正文）")}</p></div>
      <div><small>浏览器渲染后（${(br.chars || chars).toLocaleString()} 字）</small><p>${esc((d.text || "").replace(/\s+/g, " ").slice(0, 160))}…</p></div></div>` : "";

  // ④ 代价：只有用了浏览器才值得说
  const cost = dynamic && br.ok && br.elapsed_ms ? `<p class="fc-note">${html?.elapsed_ms >= 100
      ? `浏览器渲染耗时约为普通抓取的 ${Math.max(2, Math.round(br.elapsed_ms / html.elapsed_ms))} 倍`
      : `浏览器渲染用时 ${secs(br.elapsed_ms)}，普通抓取不到 0.1 秒`}，所以只在普通抓取拿不到正文时才用。</p>` : "";

  // ⑤ 合规
  const robotsNo = att.some((a) => a.kind === "robots");
  const comply = `<p class="fc-badges">${robotsNo ? `<span class="tag">robots.txt 不允许 · 已停止</span>` : `<span class="tag">遵守 robots.txt</span>`}<span class="tag">未登录</span><span class="tag">未绕过验证码</span></p>`;

  // ⑥ 需要你做的：只在没拿到全文时出现
  const action = !full ? pasteBox(d, dynamic) : "";

  return `<section class="card fetch-card">
    <header><h3>采集过程</h3>${status}</header>
    <p class="fc-kind">${esc(d.source?.name || d.source_id)} · ${kindLabel}</p>
    <p class="fc-result">${result}</p>
    <ol class="fc-steps">${steps.join("")}</ol>
    ${compare}${cost}${comply}${action}
    <details class="fc-tech"><summary>查看技术细节</summary><pre>${esc(JSON.stringify(att, null, 1))}</pre></details>
  </section>`;
}

function bindPaste(el) {
  const b = el.querySelector("[data-paste-btn]");
  if (!b) return;
  b.onclick = async () => {
    const msg = el.querySelector("[data-paste-msg]");
    try {
      const r = await api(`/api/docs/${b.dataset.pasteBtn}/paste`, { method: "POST", body: { text: el.querySelector("[data-paste]").value } });
      location.hash = `#/doc/${r.doc_id}`;
      if (r.doc_id === b.dataset.pasteBtn) showDoc(r.doc_id);
    } catch (err) { msg.textContent = err.message; }
  };
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
    ${fetchCard(d)}
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

    <h3>正文</h3><div class="doc-text">${(d.text || "").split(/\n+/).map((p) => p.startsWith("## ") ? `<h4>${esc(p.slice(3))}</h4>` : `<p>${esc(p)}</p>`).join("")}</div>`;
  bindPaste(el);
}

// ---------------------------------------------------------------- 记忆
const DOMAIN_NAME = { tech: "科技", finance: "金融", science: "学术", general: "综合", community: "社区" };   // 启动后被 /api/domains 覆盖
let domainsCache = [];

async function loadDomains() {
  domainsCache = await api("/api/domains");
  for (const k of Object.keys(DOMAIN_NAME)) delete DOMAIN_NAME[k];
  for (const d of domainsCache) DOMAIN_NAME[d.id] = d.name;
  renderDomainChips();
}

// 新主题界面：领域胶囊（内置 + 自建）+ “＋ 标签”就地新建
function renderDomainChips(checkId, only = false) {
  const box = $("#domain-chips");
  if (!box) return;
  const checked = only ? new Set() : new Set($$("#domain-chips input:checked").map((i) => i.value));
  if (checkId) checked.add(checkId);
  box.innerHTML = domainsCache.map((d) => `<label class="chip-t ${d.custom ? "custom" : ""}" title="${esc(d.desc || d.name)}">
    <input type="checkbox" name="domain" value="${esc(d.id)}" ${checked.has(d.id) ? "checked" : ""}><span>${esc(d.name)}</span></label>`).join("");
}

function tagForm(prefix) {
  return `<div class="tag-form">
    <label>标签名称<input name="${prefix}name" maxlength="12" placeholder="例如：宠物、留学、户外运动"></label>
    <label>判断关键词（可空，逗号分隔）<input name="${prefix}hints" placeholder="例如：猫, 狗, 猫粮, 宠物医院"></label>
    <p class="small">主题里出现这些词时会自动归到这个标签；新标签默认用知乎、小红书、抖音、B站、全网搜索和公众号检索，可在“信息源”页给其他来源加上它。</p>
    <div class="row"><button type="button" data-tag-save>新建标签</button><button type="button" class="ghost" data-tag-cancel>取消</button><small class="gap" data-tag-msg></small></div></div>`;
}

function setupTagPop() {
  const pop = $("#tag-pop");
  $("#tag-add-btn").onclick = () => {
    pop.hidden = !pop.hidden;
    if (pop.hidden) return;
    pop.innerHTML = tagForm("tp_");
    pop.querySelector("[name=tp_name]").focus();
    pop.querySelector("[data-tag-cancel]").onclick = () => { pop.hidden = true; };
    const save = async () => {
      const name = pop.querySelector("[name=tp_name]").value.trim();
      try {
        const d = await api("/api/domains", { method: "POST", body: { name, hints: pop.querySelector("[name=tp_hints]").value } });
        pop.hidden = true;
        await loadDomains();
        renderDomainChips(d.id);
      } catch (err) { pop.querySelector("[data-tag-msg]").textContent = err.message; }
    };
    pop.querySelector("[data-tag-save]").onclick = save;
    pop.querySelectorAll("input").forEach((i) => i.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
  };
}
const PREF_TYPES = { reading_minutes: "阅读时间（分钟）", background: "知识背景", focus: "关注重点",
  exclude: "不想看的内容", source_prefer: "来源偏好", style: "写作要求" };

function memRow(m) {
  const v = m.meta?.verify;
  return `<a class="doc-row mem-${m.status}" href="#/memory/${m.id}">
    <div class="doc-main"><b>${esc(m.content)}</b>
      <small>${esc(m.kind)} · 更新 ${fmtTime(m.updated_at)} · 使用 ${m.used_count} 次${m.last_used_at ? `（最近 ${fmtTime(m.last_used_at)}）` : ""}${m.note ? ` · ${esc(m.note)}` : ""}</small></div>
    <div class="doc-tags">
      ${m.invalid_value ? "" : `<span class="tag st-${m.status}">${mstatus(m)}</span>`}
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
      <p class="meta">每条记忆都记录来源、适用范围、更新时间和使用次数；可以查看证据、更正或删除，失效会同步到派生条目。</p></div>
      <a class="btn ghost" href="#/memory/demo">记忆验证演示 →</a></div>
    <div class="card"><h3>用户偏好（长期）</h3>
      ${prefs.map((p) => memRow(p) + (p.suggest ? `<div class="fix-row">这条偏好的类型选错了，所以没有生效。
        <button class="ghost sm" data-fix="${p.id}" data-type="${p.suggest.type}" data-value="${p.suggest.value}">${esc(p.suggest.label)}</button></div>` : "")).join("") || '<p class="small">还没有长期偏好。任务里的“本次要求”默认不会保存，勾选“同时保存为长期偏好”或在这里添加才会生效。</p>'}
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
    <h3>历史交付</h3><p class="small">每次检索交付了哪些资料和事实。下次检索会参考它，说明“与上次相比”的变化；其中的事实被更正时，它会标为“部分结论已过期”。</p>
    ${by("delivery").map(memRow).join("") || '<p class="small">无</p>'}
    <details class="card"><summary>下一次任务会召回什么（预览，约 ${preview.tokens} token，预算外舍弃 ${preview.dropped} 条）</summary><pre>${esc(preview.text || "（无）")}</pre></details>` : ""}`;
  $$("[data-fix]").forEach((b) => b.onclick = async () => {
    try {
      await api("/api/preferences", { method: "POST", body: { type: b.dataset.type, value: +b.dataset.value } });
      await api(`/api/memories/${b.dataset.fix}`, { method: "DELETE", body: { reason: "类型选错，已改为阅读时间" } });
      showMemoryHome(qs);
    } catch (err) { b.insertAdjacentText("afterend", " " + err.message); }
  });
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

// ---------------------------------------------------------------- 文章
const CHECK_NAME = { supported: "出处支持", semantic: "改写后意思一致", weak: "出处里找不到依据", number: "数字对不上出处", bad_cite: "出处编号不存在",
  uncited: "有数字但没有出处", analysis: "分析", analysis_num: "分析里有资料中没有的数字", plain: "一般叙述" };
const CHECK_BAD = new Set(["weak", "number", "bad_cite", "uncited", "analysis_num"]);

async function showArticle(id, ver) {
  const el = $("#detail");
  const me = current = {};
  const d = await api(`/api/articles/${id}${ver ? `?v=${ver}` : ""}`);
  if (current !== me) return;
  const a = d.article, v = d.version || {}, m = v.meta || {};
  const s = m.check?.summary || {};
  const refOf = (n) => m.refmap?.[String(n)] ?? n;
  const renum = (t) => (t || "").replace(/\[(\d+)\]/g, (_, n) => `[${refOf(n)}]`);
  const docs = (m.docs || []).slice().sort((x, y) => (x.ref ?? 99) - (y.ref ?? 99));
  const all = (m.check?.items || []).map((it, k) => ({ ...it, k }));
  const items = all.filter((i) => i.status !== "plain");
  const bad = all.filter((i) => CHECK_BAD.has(i.status));
  const fixes = m.fixes || [], gaps = m.gap_log || [];
  const tab = store.get("artTab") || "check";
  const secName = (sid) => (m.sections || []).find((x) => x.id === sid)?.heading || sid;
  const panes = {
    check: `<div class="stats sm"><div><b>${s.support_rate != null ? Math.round(s.support_rate * 100) + "%" : "—"}</b><small>出处支持率</small></div>
        <div><b>${s.cited ?? 0}</b><small>带出处</small></div><div><b>${s.semantic ?? 0}</b><small>改写一致</small></div>
        <div><b class="${s.problems ? "gap" : "ok"}">${s.problems ?? 0}</b><small>待你确认</small></div></div>
      <p class="small muted">先按字面核对出处和每个数字；字面不像的句子再让大模型对照原文判断意思是否一致。下面列出的句子在正文里有下划线，点一下跳过去。</p>
      ${bad.length ? bad.map((i) => `<div class="chk bad" data-goto="${i.k}"><span class="tag rel-lo">${CHECK_NAME[i.status]}</span>
          <span class="chk-s">${esc(renum(i.sentence))}</span>
          <details><summary>原因与出处原文</summary>${i.note ? `<p>${esc(renum(i.note))}</p>` : ""}${i.quote ? `<p class="muted">出处原文：${esc(i.quote)}</p>` : ""}</details></div>`).join("")
        : `<p class="small ok">✓ 没有数字对不上、缺出处或找不到依据的句子</p>`}
      <details class="small all-chk"><summary>全部 ${items.length} 句的核对结果</summary>${items.map((i) => `<div class="chk"><span class="tag ${CHECK_BAD.has(i.status) ? "rel-lo" : ""}">${CHECK_NAME[i.status]}</span>
        ${esc(renum(i.sentence))}${i.quote && i.status !== "supported" ? `<br><small class="muted">出处原文：${esc(i.quote)}</small>` : ""}</div>`).join("")}</details>`,
    fixes: fixes.length ? fixes.map((f) => `<div class="chk small">${f.rewrite
        ? `<b>按核对结果改写「${esc(secName(f.section))}」</b>${(f.items || []).map((i) => `<br><small class="muted">${CHECK_NAME[i.status] || esc(i.status)}：${esc(renum(i.sentence))}</small>`).join("")}`
        : `<b>${esc(f.why)}</b><br><s>${esc(renum(f.before))}</s><br>→ ${esc(renum(f.after))}`}</div>`).join("")
      : `<p class="small muted">写作中没有需要改正的地方。</p>`,
    gaps: gaps.length ? gaps.map((g) => `<div class="chk small"><b>「${esc(g.query)}」</b> ${esc(g.why || "")}<br>
        ${g.skipped ? `<small class="muted">${esc(g.skipped)}</small>` : `<small class="muted">${esc((g.sources || []).join("；"))}</small><br>${g.found?.length ? "找到：" + g.found.map(esc).join("；") : "没有找到新资料"}`}</div>`).join("")
      : `<p class="small muted">素材够用，写作时没有补充检索。</p>`,
    docs: `<p class="small muted">按 Skill 的选材规则从价值排序里选出；带编号的是正文引用了的。</p>` +
      docs.map((x) => `<div class="chk small">${x.ref ? `<b>[${x.ref}]</b> ` : `<span class="muted">未引用</span> `}<a href="#/doc/${x.id}">${esc(x.title)}</a><br><small class="muted">${esc(x.label || "")} · ${esc(x.why || "")}</small></div>`).join(""),
  };
  const figs = m.figures || [], cover = m.cover, ich = m.image_checks || [], irej = m.image_rejected || [];
  const figRow = (f, where) => `<div class="fig-row"><img src="${esc(f.src)}" alt="" loading="lazy">
      <div><b>${where}</b> <span class="tag ${f.kind === "diagram" ? "nv-update" : ""}">${f.kind === "diagram" ? "示意图" : "原图"}</span>
        <p class="small">${esc(renum(f.caption || ""))}</p>
        <p class="small muted">为什么放这里：${esc(f.why || "")}</p>
        ${f.kind === "photo" && f.w ? `<p class="small muted">${f.w}×${f.h} · 清晰度 ${f.sharpness ?? "—"}${(f.warn || []).length ? ` · <span class="gap">${f.warn.map(esc).join("；")}</span>` : ""}
          ${f.url ? ` · <a href="${esc(f.url)}" target="_blank" rel="noopener">原图链接</a>` : ""}</p>` : ""}</div></div>`;
  panes.imgs = `${ich.length ? `<div class="img-checks">${ich.map((c) => `<span class="tag ${c.ok ? "" : "rel-lo"}">${c.ok ? "✓" : "✗"} ${esc(c.name)}：${esc(c.detail)}</span>`).join("")}</div>` : ""}
    ${cover ? figRow(cover, "封面") : ""}
    ${figs.map((f) => figRow(f, `「${esc(secName(f.section))}」第 ${f.para + 1} 段后`)).join("") || '<p class="small muted">这篇文章写于配图功能上线之前，重新写一次即可配图。</p>'}
    ${irej.length ? `<details class="small"><summary>没用上的图（${irej.length}）</summary>${irej.map((r) => `<div class="chk">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc((r.doc || "").slice(0, 30))}</a>` : esc(r.doc || "")}：${esc(r.why)}</div>`).join("")}</details>` : ""}
    <p class="small muted">质检：清晰度（边缘锐度）、尺寸、压缩、是否缩略图/裁切、与已选图是否重复；位置按图注和图片前后的正文与段落的相似度决定。示意图只用文中已核对过的数据。</p>`;
  const TABS = [["check", "核对", bad.length], ["imgs", "配图", figs.length + (cover ? 1 : 0)], ["fixes", "改动", fixes.length], ["gaps", "补检", gaps.length], ["docs", "素材", docs.length]];
  el.innerHTML = `
    <div class="detail-head art-head"><div><p class="kicker">文章 · ${esc(m.skill?.name || a.skill)} v${esc(m.skill?.version || "")}</p><h1>${esc(a.title || "")}</h1>
      <p class="meta">第 ${v.version} 版（${esc(v.note || "")}，${fmtTime(v.created_at)}）
        ${d.versions.length > 1 ? `<select id="art-ver">${d.versions.map((x) => `<option value="${x.version}" ${x.version === v.version ? "selected" : ""}>第 ${x.version} 版 · ${esc(x.note || "")}</option>`).join("")}</select>` : ""}
        · <a href="#/task/${esc(m.research_task || "")}">检索任务</a> · <a href="#/task/${esc(a.task_id || "")}">写作过程</a>
        · <span title="Skill 文件内容指纹：同一版本号下内容改过也能看出来">Skill 指纹 <code>${esc(m.skill?.fingerprint || "")}</code></span>
        · 提纲${m.outline_by === "llm" ? "大模型" : "规则"}，正文${m.draft_by === "llm" ? "大模型写作" : "规则摘编"}</p>
      ${(m.prefs || []).length ? `<p class="meta">用到的写作偏好/反馈：${m.prefs.map(esc).join("；")}</p>` : ""}</div></div>
    <div class="art-layout">
      <article class="art-body">${v.html || ""}</article>
      <aside class="art-side card">
        <div class="side-tabs">${TABS.map(([k, n, c]) => `<button class="${k === tab ? "on" : ""}" data-tab="${k}">${n}${c ? ` <i>${c}</i>` : ""}</button>`).join("")}</div>
        <div class="side-pane">${panes[tab] || panes.check}</div>
      </aside>
    </div>`;
  $("#art-ver") && ($("#art-ver").onchange = (e) => location.hash = `#/article/${id}?v=${e.target.value}`);
  $$(".side-tabs button", el).forEach((b) => b.onclick = () => {
    store.set("artTab", b.dataset.tab);
    $$(".side-tabs button", el).forEach((x) => x.classList.toggle("on", x === b));
    $(".side-pane", el).innerHTML = panes[b.dataset.tab];
    $(".side-pane", el).scrollTop = 0;
    bindGoto();
  });
  // 出处上标：页内跳到参考资料（不能改 location.hash，否则会触发路由）
  $$(".art-body sup.cite a", el).forEach((x) => x.onclick = (e) => {
    e.preventDefault();
    const li = $(`.art-body #ref-${x.getAttribute("href").split("-").pop()}`, el);
    if (li) { li.scrollIntoView({ behavior: "smooth", block: "center" }); flash(li); }
  });
  function bindGoto() {
    $$("[data-goto]", el).forEach((row) => row.querySelector(".chk-s").onclick = () => {
      const mk = $(`.art-body mark[data-chk="${row.dataset.goto}"]`, el);
      if (mk) { mk.scrollIntoView({ behavior: "smooth", block: "center" }); flash(mk); }
    });
  }
  bindGoto();
}

function flash(node) {
  node.classList.remove("flash");
  void node.offsetWidth;
  node.classList.add("flash");
}

// ---------------------------------------------------------------- 记忆验证演示（固定样例回放）
async function showMemoryDemo() {
  const el = $("#detail");
  const me = current = {};
  const d = await api("/api/demo/memory");
  if (current !== me) return;
  const run = (r) => !r ? "" : r.status === "done" ? `<span class="ok">✓ 完成</span> <a href="#/task/${r.id}">查看任务</a>`
    : r.status === "failed" ? `<span class="gap">失败：${esc(r.error || "")}</span>` : `<span class="spin"></span> 运行中…（约 10 秒）`;
  const running = [d.run1, d.run2].some((r) => r && !["done", "failed", "cancelled", "interrupted"].includes(r.status));
  const s1 = d.run1?.status === "done", s2 = d.run2?.status === "done";
  const step = (n, title, on, body) => `<div class="card demo-step ${on ? "" : "dim"}"><div class="demo-n">${n}</div><div class="demo-b"><h3>${title}</h3>${body}</div></div>`;
  el.innerHTML = `
    <div class="detail-head"><div><p class="kicker">记忆 · 验证演示</p><h1>同一主题，两次独立任务</h1>
      <p class="meta">主题「${esc(d.topic)}」。用固定样例回放（模拟网站、独立数据库），每次结果都一样，方便对照。
      第 2 天的样例里：IT之家把 128K 更正为 64K，出现一篇 256K 新进展，Hacker News 不可用。</p></div>
      ${d.enabled ? `<button class="ghost sm" id="demo-reset">从头再来</button>` : ""}</div>
    ${!d.enabled ? `<div class="card warn-card"><h3>需要在回放模式下运行</h3>
      <p class="small">为了不动你的真实资料和记忆，演示用独立的数据库和模拟网站。在项目文件夹里双击 <b>演示回放.command</b>（或在终端执行 <code>PORT=8001 bash start.command --replay</code>），浏览器会在 8001 端口打开回放版的这一页。不用关掉当前服务，两个可以同时开。</p></div>` : ""}
    ${step(1, "第一次任务（第 1 天）", d.enabled, `<p class="small">检索、抽取事实、写入历史交付。此时还没有任何记忆。</p>
      <div class="row">${s1 ? "" : `<button id="demo-run1" ${running || !d.enabled ? "disabled" : ""}>开始第一次任务</button>`} ${run(d.run1)}</div>`)}
    ${step(2, "修改记忆", s1, `<p class="small">删除或更正一条事实，再设置几条长期偏好。可以自己点，也可以一键套用示例改动。</p>
      ${s1 ? `<div class="demo-facts">${d.facts.map((f) => `<div class="demo-fact"><span>${esc(f.content)} <span class="tag v-${f.verify}">${VERIFY[f.verify] || esc(f.verify || "")}</span></span>
        ${s2 ? "" : `<span class="nowrap"><button class="ghost sm" data-del="${f.id}">删除</button> <button class="ghost sm" data-fix="${f.id}">更正</button></span>`}</div>`).join("") || '<p class="small">没有事实</p>'}</div>
        <p class="small">长期偏好：${d.prefs.map((p) => esc(p.content)).join("；") || "无"}（在 <a href="#/memory">记忆页</a> 可以添加）</p>
        ${s2 ? "" : `<button class="ghost" id="demo-sample">一键套用示例改动</button><small class="muted"> 删除“64K 以上准确率下降”这条事实；偏好：关注“大海捞针”、排除“踩坑”、阅读时间 10 分钟</small>`}
        ${d.edits.length ? `<ul class="small demo-edits">${d.edits.map((e) => `<li><span class="tag">${esc(e.op)}</span> ${esc(e.content)}</li>`).join("")}</ul>` : ""}` : ""}`)}
    ${step(3, "第二次任务（第 2 天）", s1, `<p class="small">同一主题再跑一次。任务开始时会召回长期记忆，结束时和第一次对比。</p>
      <div class="row">${s2 ? "" : `<button id="demo-run2" ${running || !s1 ? "disabled" : ""}>开始第二次任务</button>`} ${run(d.run2)}</div>`)}
    ${step(4, "核对结果", s2, s2 && d.compare ? compareCard(d.compare) + recallNow(d.recall_now) : `<p class="small">第二次任务完成后，这里显示“与上次相比”，并重新召回一次，逐条确认删除和更正过的记忆不再出现。</p>`)}`;
  const go = (path, body) => async (e) => {
    e.target.disabled = true;
    try { await api(`/api/demo/memory/${path}`, { method: "POST", body: body || {} }); } catch (err) { alertInline(e.target, err.message); }
    showMemoryDemo();
  };
  $("#demo-run1") && ($("#demo-run1").onclick = go("run1"));
  $("#demo-run2") && ($("#demo-run2").onclick = go("run2"));
  $("#demo-sample") && ($("#demo-sample").onclick = go("sample"));
  $("#demo-reset") && ($("#demo-reset").onclick = async (e) => {
    if (e.target.dataset.sure !== "1") { e.target.dataset.sure = "1"; e.target.textContent = "确认清空演示数据？"; e.target.classList.add("danger"); return; }
    await go("reset")(e);
  });
  $$("[data-del]", el).forEach((b) => b.onclick = async () => {
    await api(`/api/memories/${b.dataset.del}`, { method: "DELETE", body: { reason: "演示：手动删除" } });
    showMemoryDemo();
  });
  $$("[data-fix]", el).forEach((b) => b.onclick = () => {
    const row = b.closest(".demo-fact");
    if (row.querySelector("form")) return;
    const old = row.querySelector("span").firstChild.textContent.trim();
    row.insertAdjacentHTML("beforeend", `<form class="row demo-fix"><input name="c" value="${esc(old)}"><button>保存更正</button></form>`);
    row.querySelector("form").onsubmit = async (e) => {
      e.preventDefault();
      await api(`/api/memories/${b.dataset.fix}/correct`, { method: "POST", body: { content: e.target.c.value, reason: "演示：手动更正" } });
      showMemoryDemo();
    };
  });
  if (running) me.timer = setTimeout(() => { if (current === me) showMemoryDemo(); }, 2000);
}

function alertInline(btn, msg) { btn.insertAdjacentHTML("afterend", ` <small class="gap">${esc(msg)}</small>`); }

function recallNow(r) {
  if (!r) return "";
  const leaked = r.dead.filter((x) => x.recalled);
  return `<div class="card"><h3>现在再召回一次</h3>
    <p class="small">模拟“下一次任务开始”，召回该主题的记忆（不计入使用次数），逐条检查已删除、已更正、已失效的记忆：
    ${leaked.length ? `<span class="gap">✗ 有 ${leaked.length} 条仍被召回</span>` : `<span class="ok">✓ ${r.dead.length} 条全部没有出现</span>`}</p>
    <table class="tbl"><tr><th>失效的记忆</th><th>状态</th><th>召回中</th></tr>${r.dead.map((x) => `<tr><td class="small"><s>${esc(x.content)}</s></td>
      <td><span class="tag st-${x.status}">${MSTATUS[x.status] || esc(x.status)}</span></td><td>${x.recalled ? '<span class="gap">出现</span>' : '<span class="ok">未出现</span>'}</td></tr>`).join("")}</table>
    <details class="small"><summary>召回结果全文（${r.lines.length} 条）</summary><ul>${r.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></details></div>`;
}

async function showMemory(id) {
  const el = $("#detail");
  const m = await api(`/api/memories/${id}`);
  const alive = ["active", "stale"].includes(m.status);
  const isDelivery = m.kind === "delivery";
  el.innerHTML = `
    <p class="kicker"><a href="#/memory?topic=${encodeURIComponent(m.topic || "")}">← 记忆</a></p>
    <h1 class="doc-title">${esc(m.content)}</h1>
    <table class="tbl meta-tbl">
      <tr><th>类型</th><td>${esc(m.scope)} / ${esc(m.kind)}${m.meta?.type ? ` / ${esc(PREF_TYPES[m.meta.type] || m.meta.type)}` : ""}</td></tr>
      <tr><th>状态</th><td><span class="tag st-${m.status}">${mstatus(m)}</span>
        ${m.meta?.verify ? `<span class="tag v-${m.meta.verify}">${VERIFY[m.meta.verify]}</span>` : ""} ${esc(m.note || "")}</td></tr>
      ${m.meta?.independent != null ? `<tr><th>证据强度</th><td>独立来源 ${m.meta.independent} 个${m.meta.primary ? "（含一手来源）" : ""}；转载 ${m.meta.reprints || 0} 篇（不重复计数）${m.meta.generated_ignored ? `；系统生成内容 ${m.meta.generated_ignored} 条已忽略` : ""}；置信度 ${m.confidence ?? "—"}</td></tr>` : ""}
      <tr><th>适用范围</th><td>${esc(m.applies_to || "all")}${m.topic ? ` · 主题：${esc(m.topic)}` : ""}</td></tr>
      <tr><th>来源</th><td><code>${esc(JSON.stringify(m.source))}</code></td></tr>
      <tr><th>时间</th><td>创建 ${fmtTime(m.created_at)} · 更新 ${fmtTime(m.updated_at)}${m.recheck_at ? ` · 复查 ${fmtTime(m.recheck_at)}` : ""}</td></tr>
      <tr><th>使用</th><td>${m.used_count} 次${m.last_used_at ? `，最近 ${fmtTime(m.last_used_at)}` : ""}</td></tr>
      ${m.meta?.previous_content ? `<tr><th>更正前</th><td><s>${esc(m.meta.previous_content)}</s></td></tr>` : ""}
      ${m.successor ? `<tr><th>已被更正为</th><td><a href="#/memory/${m.successor.id}">${esc(m.successor.content)}</a></td></tr>` : ""}
    </table>
    ${isDelivery && (alive || m.status === "archived") ? `<div class="card" id="dlv-actions"><h3>这条交付记录</h3>
      <p class="small">交付记录是“发生过什么”的历史，没有对错，所以不提供更正。${m.status === "archived"
        ? "它现在<b>不再参考</b>：下次检索不会召回，但记录保留，可以随时恢复。" : "不想让下次检索参考它时，选“不再参考”即可，记录会保留、可以恢复。"}</p>
      <div class="row">${m.status === "archived" ? `<button id="dlv-restore">恢复参考</button>` : `<button id="dlv-archive">不再参考</button>`}
        <button type="button" class="ghost danger sm" id="dlv-del">彻底删除…</button></div>
      <div id="dlv-impact" hidden></div></div>` : ""}
    ${alive && !isDelivery ? `<form id="mem-edit" class="card"><h3>更正这条记忆</h3>
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
  if (isDelivery && $("#dlv-actions")) {
    const ar = $("#dlv-archive"), rs = $("#dlv-restore");
    if (ar) ar.onclick = async () => { await api(`/api/memories/${id}/archive`, { method: "POST", body: {} }); showMemory(id); };
    if (rs) rs.onclick = async () => { await api(`/api/memories/${id}/restore`, { method: "POST", body: {} }); showMemory(id); };
    $("#dlv-del").onclick = async () => {
      const box = $("#dlv-impact");
      const imp = await api(`/api/memories/${id}/impact`);
      box.hidden = false;
      box.innerHTML = `<div class="fc-action"><p><b>彻底删除后无法恢复。</b>这次交付包含 ${imp.docs} 篇资料、${imp.facts} 条事实。影响：</p>
        <ul class="small">${imp.effects.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        <p class="small">如果只是不想让下次检索参考它，用“不再参考”更合适。</p>
        <div class="row"><button class="danger" id="dlv-del-yes">确认彻底删除</button><button class="ghost" id="dlv-del-no">取消</button></div></div>`;
      $("#dlv-del-no").onclick = () => { box.hidden = true; };
      $("#dlv-del-yes").onclick = async () => { await api(`/api/memories/${id}`, { method: "DELETE", body: { reason: "用户彻底删除交付记录" } }); showMemory(id); };
    };
  }
  if (alive && !isDelivery) {
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
    <div class="card" id="tag-mgr"><h3>领域标签</h3>
      <p class="small">主题被判定为哪些领域，就用带这些标签的来源检索。可以新建自己的标签，再在下面的来源上点“标签”勾选。</p>
      <div class="tag-list">${domainsCache.map((d) => `<span class="dtag ${d.custom ? "custom" : ""}" title="${esc(d.desc)}${d.hints?.length ? "\n关键词：" + esc(d.hints.slice(0, 12).join("、")) : ""}">
        ${esc(d.name)} <small>${d.sources} 个来源</small>${d.custom ? `<button class="x" data-del-tag="${esc(d.id)}" title="删除这个标签">×</button>` : ""}</span>`).join("")}
        <button class="chip-add" id="mgr-add">＋ 新建标签</button></div>
      <div id="mgr-form" hidden></div></div>
    <table class="tbl src-tbl"><tr><th>来源</th><th>方式</th><th>资料</th><th>最近状态</th><th></th></tr>
    ${sourcesCache.map((s) => {
      const h = s.health || {};
      const err = h.last_error;
      const okRecent = h.last_ok && (!h.last_fail || h.last_ok > h.last_fail);
      return `<tr><td><b>${esc(s.name)}</b><br><small>${esc(s.config?.feed || s.base_url || "")}</small></td>
        <td><span class="tag">${esc(s.method)}</span><br>${s.config?.any_domain ? `<span class="tag">适用所有领域</span>` : (s.config?.domains || []).map((d) => `<span class="tag">${esc(DOMAIN_NAME[d] || d)}</span>`).join(" ")}
          ${s.config?.any_domain || s.id === "web" ? "" : `<button class="linkish" data-edit-tags="${s.id}">标签</button>`}</td><td class="c-docs">${s.docs}<span class="m-only"> 篇资料</span></td>
        <td>${h.calls ? (okRecent ? `<span class="ok">正常</span> ${fmtTime(h.last_ok)}${h.last_count === 0
          ? `<br><small class="muted">最近一次 0 条：${esc(h.last_note || "没有匹配结果")}</small>`
          : h.last_count != null ? `<br><small>最近一次 ${h.last_count} 条</small>` : ""}` : `<span class="gap">失败</span> ${esc(err?.kind || "")} ${esc(err?.reason || "")}<br><small>${fmtTime(h.last_fail)}，连续 ${h.consecutive_fail} 次</small>`) : "<small>未使用</small>"}</td>
        <td class="nowrap">${s.id !== "web" ? `<button class="ghost sm" data-test="${s.id}">测试</button>` : ""}
          <button class="ghost sm" data-toggle="${s.id}" data-on="${s.enabled}">${s.enabled ? "停用" : "启用"}</button></td></tr>
        <tr class="test-row" id="test-${s.id}" hidden><td colspan="5"></td></tr>
        <tr class="test-row" id="tags-${s.id}" hidden><td colspan="5"></td></tr>`;
    }).join("")}</table>
    <form class="card" id="add-src"><h3>添加 RSS 来源</h3>
      <div class="row"><input name="name" placeholder="名称" required><input name="feed" placeholder="RSS 地址" required></div>
      <label class="opt"><input type="checkbox" name="full_text"> RSS 内含全文</label>
      <label class="opt"><input type="checkbox" name="browser"> 文章页需要浏览器渲染</label>
      <input name="search_feed" placeholder="可选：站内搜索 RSS 模板，用 {q} 表示关键词，如 https://example.com/?s={q}&feed=rss2" style="width:100%;padding:6px;margin:4px 0">
      <p class="small">领域（至少选一个，检索这些领域的主题时才会用到它）：</p>
      <div class="chips">${domainsCache.map((d) => `<label class="chip-t"><input type="checkbox" name="domains" value="${esc(d.id)}"><span>${esc(d.name)}</span></label>`).join("")}</div>
      <p class="small gap" id="add-src-msg"></p>
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
  $$("[data-edit-tags]").forEach((b) => b.onclick = () => {
    const sid = b.dataset.editTags, row = $(`#tags-${sid}`), src = sourcesCache.find((x) => x.id === sid);
    row.hidden = !row.hidden;
    if (row.hidden) return;
    const cur = new Set(src.config?.domains || []);
    row.firstElementChild.innerHTML = `<div class="chips">${domainsCache.map((d) => `<label class="chip-t"><input type="checkbox" value="${esc(d.id)}" ${cur.has(d.id) ? "checked" : ""}><span>${esc(d.name)}</span></label>`).join("")}</div>
      <div class="row" style="margin-top:6px"><button class="sm" data-save>保存「${esc(src.name)}」的标签</button><small class="gap" data-msg></small></div>`;
    row.querySelector("[data-save]").onclick = async () => {
      const doms = [...row.querySelectorAll("input:checked")].map((i) => i.value);
      try { await api(`/api/sources/${sid}`, { method: "PATCH", body: { domains: doms } }); await loadDomains(); showSources(); }
      catch (err) { row.querySelector("[data-msg]").textContent = err.message; }
    };
  });
  $$("[data-del-tag]").forEach((b) => b.onclick = async () => {
    const d = domainsCache.find((x) => x.id === b.dataset.delTag);
    const box = $("#mgr-form");
    box.hidden = false;
    box.innerHTML = `<div class="fc-action"><p>删除标签「${esc(d.name)}」？它会从 ${d.sources} 个来源上摘掉，已有资料不受影响。</p>
      <div class="row"><button class="danger" data-yes>删除</button><button class="ghost" data-no>取消</button></div></div>`;
    box.querySelector("[data-no]").onclick = () => { box.hidden = true; };
    box.querySelector("[data-yes]").onclick = async () => { await api(`/api/domains/${d.id}`, { method: "DELETE" }); await loadDomains(); showSources(); };
  });
  $("#mgr-add").onclick = () => {
    const box = $("#mgr-form");
    box.hidden = false;
    box.innerHTML = tagForm("mg_") + `<p class="small">挂到哪些来源：</p><div class="chips">${sourcesCache.filter((s) => s.id !== "web" && !s.config?.any_domain).map((s) =>
      `<label class="chip-t"><input type="checkbox" value="${esc(s.id)}" ${["zhihu", "xiaohongshu", "douyin", "bilibili"].includes(s.id) ? "checked" : ""}><span>${esc(s.name)}</span></label>`).join("")}</div>`;
    box.querySelector("[data-tag-cancel]").onclick = () => { box.hidden = true; };
    box.querySelector("[data-tag-save]").onclick = async () => {
      const sources = [...box.querySelectorAll(".chips input:checked")].map((i) => i.value);
      try {
        await api("/api/domains", { method: "POST", body: { name: box.querySelector("[name=mg_name]").value, hints: box.querySelector("[name=mg_hints]").value, sources } });
        await loadDomains(); showSources();
      } catch (err) { box.querySelector("[data-tag-msg]").textContent = err.message; }
    };
  };
  $$("[data-toggle]").forEach((b) => b.onclick = async () => {
    await api(`/api/sources/${b.dataset.toggle}`, { method: "PATCH", body: { enabled: b.dataset.on !== "1" } });
    showSources();
  });
  $("#add-src").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    if (!f.getAll("domains").length) { $("#add-src-msg").textContent = "请至少选一个领域"; return; }
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
    <form class="card" id="ws-form"></form>
    <div class="card" id="replay-card"></div>`;
  renderWebsearchForm();
  api("/api/replay").then((r) => {
    const c = $("#replay-card");
    if (!r.enabled) { c.innerHTML = `<h3>固定样例回放</h3><p class="small">双击项目里的 <b>演示回放.command</b>（或 <code>bash start.command --replay</code>）进入离线回放模式（模拟网站、独立数据库），用于 <a href="#/memory/demo">记忆验证演示</a>。</p>`; return; }
    c.innerHTML = `<h3>固定样例回放（当前第 ${r.day} 天）</h3><p class="small">第 2 天：IT之家把 128K 更正为 64K，并出现一篇 256K 新进展；Hacker News 不可用。</p>
      <div class="row"><button class="ghost" data-day="1">切到第 1 天</button><button class="ghost" data-day="2">切到第 2 天</button>
      <a class="btn" href="#/memory/demo">打开记忆验证演示 →</a></div>`;
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

async function renderWebsearchForm() {
  const f = $("#ws-form");
  const w = await api("/api/settings/websearch");
  f.innerHTML = `<h3>搜索 API（公众号 / 知乎 / 贴吧 / 小红书 / 抖音 / 全网）</h3>
    <p class="small">这些平台没有开放搜索接口，或网页需要登录，所以通过正规搜索服务按站点检索：拿到标题、摘要和链接；公众号文章再抓全文，
      需要登录的平台只保留摘要。B站和“公众号（搜狗）”不需要 key。<b>可以同时填多家</b>：测试后，检索每个平台时自动选收录好的那家
      （例如博查不收录知乎时，知乎改用 Tavily）。</p>
    ${Object.entries(w.providers).map(([k, n]) => `<div class="ws-row"><label><input type="radio" name="primary" value="${k}" ${w.provider === k ? "checked" : ""}> ${n}</label>
      <input name="key_${k}" type="password" value="${esc(w.keys[k] || "")}" placeholder="${k === "bocha" ? "博查 key（中文推荐）" : k === "tavily" ? "tvly-…（每月有免费额度）" : "Brave key"}"></div>`).join("")}
    <p class="small">单选框表示首选服务；key 留空表示不用这家。</p>
    <div class="row"><button>保存</button><button type="button" class="ghost" id="ws-test">测试各平台收录</button>
      <span class="${w.available ? "ok" : "gap"}">${w.available ? "已配置" : "未配置"}</span></div>
    <div id="ws-result" class="small"></div>`;
  f.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(f);
    const keys = Object.fromEntries(Object.keys(w.providers).map((k) => [k, fd.get("key_" + k) || ""]));
    try { await api("/api/settings/websearch", { method: "POST", body: { keys, primary: fd.get("primary") } }); renderWebsearchForm(); }
    catch (err) { $("#ws-result").textContent = err.message; }
  };
  $("#ws-test").onclick = async () => {
    $("#ws-result").textContent = "测试中（每家服务 × 每个平台各搜一次，约十几秒）…";
    try {
      const r = await api("/api/settings/websearch/test", { method: "POST", body: {} });
      const provs = Object.keys(r.matrix);
      const plats = Object.keys(r.matrix[provs[0]] || {});
      $("#ws-result").innerHTML = `<table class="tbl small"><tr><th>平台</th>${provs.map((p) => `<th>${esc(p)}</th>`).join("")}<th>检索时使用</th></tr>
        ${plats.map((pl) => `<tr><td>${esc(pl)}</td>${provs.map((p) => {
          const v = r.matrix[p][pl];
          if (!v) return "<td>—</td>";
          if (!v.ok) return `<td class="gap">失败：${esc(v.error)}</td>`;
          return `<td class="${v.count ? "ok" : "gap"}" title="${esc(v.first || "")}">${v.count} 条${v.first ? `<br><small>${esc(v.first.slice(0, 24))}</small>` : v.raw ? "<br><small>有结果但不是该平台</small>" : ""}</td>`;
        }).join("")}<td>${(r.routes[pl] || []).length ? esc(r.routes[pl].join(" → ")) : '<span class="gap">都不收录，跳过</span>'}</td></tr>`).join("")}</table>
        <p class="small">用同一个查询「${esc(r.query)}」测试。${r.skipped.length ? `<b>${r.skipped.map(esc).join("、")}</b> 所有已配置的服务都不收录，检索时跳过；可以再填一家服务的 key 后重新测试。` : "各平台都有服务可用。"}</p>`;
    } catch (err) { $("#ws-result").textContent = err.message; }
  };
}

init();

// ---------------------------------------------------------------- Lv2 价值榜单
const FACTORS = [["relevance", "相关"], ["credibility", "可信"], ["novelty", "新颖"], ["timeliness", "时效"]];
const pct = (x) => (x == null ? "—" : (x * 100).toFixed(0) + "%");

async function loadRanking(force = false) {
  if (!current) return;
  const id = current.id;
  const v = current.rkVersion ? `?version=${current.rkVersion}` : "";
  const res = await api(`/api/tasks/${id}/ranking${v}`).catch(() => null);
  if (!res || current?.id !== id) return;
  const sig = JSON.stringify([res.versions.length, res.data?.version, res.eval?.labeled_in_pool, Object.keys(res.eval?.labels || {}).length, Object.values(res.eval?.labels || {}).join("")]);
  if (!force && sig === current.rkSig) return;
  current.rkSig = sig;
  current.rk = res;
  renderRanking();
}

function rkItemRow(it, sel, labels, topic) {
  const lab = labels[it.id];
  const bars = FACTORS.map(([k, n]) => `<span class="fbar" title="${n} ${pct(it.features[k])}"><span class="box"><i style="height:${Math.round(it.features[k] * 100)}%"></i></span><small>${n}</small></span>`).join("");
  const src = sourcesCache.find((s) => s.id === it.source_id)?.name || it.source_id;
  return `<li class="rk-item">
    <div class="rk-rank">${sel.rank}</div>
    <div class="rk-body">
      <a href="#/doc/${it.id}" class="rk-title">${esc(it.title)}</a>
      <div class="small">${esc(src)} · ${fmtTime(it.published_at)} · ${sel.cost} 分钟
        ${it.primary ? `<span class="tag pri">一手</span>` : ""}${it.independent > 1 ? `<span class="tag">${it.independent} 个来源</span>` : ""}
        ${it.unverified ? `<span class="tag rel-lo">待核实</span>` : ""}${it.disputed ? `<span class="tag rel-lo">有争议</span>` : ""}</div>
      <div class="small rk-why">${esc(sel.why)}</div>
    </div>
    <div class="rk-side">
      <div class="rk-value" title="价值分">${it.value.toFixed(2)}</div>
      <div class="fbars">${bars}</div>
      <div class="rk-label">
        <button class="mini ${lab === 1 ? "on" : ""}" data-label="1" data-doc="${it.id}" title="标注为相关">相关</button>
        <button class="mini ${lab === 0 ? "on" : ""}" data-label="0" data-doc="${it.id}" title="标注为不相关">不相关</button>
        <button class="mini" data-verify="${it.id}" title="到其他来源找交叉印证，然后重新评估">核验</button>
      </div>
    </div></li>`;
}

function rkListTab(d, key, labels, topic) {
  const L = d.lists[key];
  const items = Object.fromEntries(d.items.map((it) => [it.id, it]));
  const desc = L.mode === "skim" ? "每篇按读标题和摘要约 0.6 分钟计，适合快速掌握动态" : "每篇按全文阅读时间计（超长文按 15 分钟封顶）";
  return `<p class="small">预算 ${L.budget} 分钟，已用 ${L.used} 分钟，入选 ${L.selected.length} 篇。${desc}；同一事件只保留价值最高的一篇。</p>
    <ol class="rk-list">${L.selected.map((s) => rkItemRow(items[s.id], s, labels, topic)).join("") || "<p class='empty'>没有可入选的资料</p>"}</ol>
    <details class="rk-excl"><summary>未入选 ${L.excluded.length} 篇及原因</summary>
      <ul class="small">${L.excluded.map((e) => `<li><a href="#/doc/${e.id}">${esc(items[e.id]?.title || e.id)}</a> — ${esc(e.why)}</li>`).join("")}</ul></details>`;
}

function rkBaselineTab(d, ev, labels) {
  const m = ev.metrics;
  const names = [["ours", "价值排序（我们）"], ["time", "按发布时间"], ["popularity", "按热度"]];
  const rows = [["P@5", "P@5（前 5 篇中相关的比例）"], ["P@10", "P@10"], ["coverage@10", "子问题覆盖率（前 10）"],
    ["dup@10", "重复率（前 10，越低越好）"], ["fresh@10", "7 天内占比（前 10）"], ["primary@10", "一手来源占比（前 10）"]];
  const best = (k) => {
    const vals = names.map(([n]) => m[n]?.[k]).filter((x) => x != null);
    if (!vals.length) return null;
    return k === "dup@10" ? Math.min(...vals) : Math.max(...vals);
  };
  const items = Object.fromEntries(d.items.map((it) => [it.id, it]));
  const col = (n) => (d.orders[n] || []).slice(0, 10).map((id, i) => {
    const it = items[id]; const lab = labels[id];
    return `<li class="${lab === 1 ? "lab-yes" : lab === 0 ? "lab-no" : ""}"><span class="small">${i + 1}.</span> <a href="#/doc/${id}">${esc((it?.title || id).slice(0, 48))}</a>
      <span class="rk-label inline"><button class="mini ${lab === 1 ? "on" : ""}" data-label="1" data-doc="${id}">相关</button><button class="mini ${lab === 0 ? "on" : ""}" data-label="0" data-doc="${id}">不相关</button></span></li>`;
  }).join("");
  return `<p class="small">三种排序用同一批资料。基线不做价值评估、不去重。P@K 优先用你的人工标注，未标注的用自动标注（大模型复核分 ≥2 或规则相关度 ≥0.6 视为相关）。
    自动标注和价值排序都用了相关度信号，会偏向我们的方法，所以<b>人工标注越多，对比越可信</b>：已标注 ${ev.labeled_in_pool} / ${ev.pool.length} 篇（三种排序前 10 篇的并集）。</p>
    <table class="tbl rk-metrics"><tr><th>指标</th>${names.map(([, n]) => `<th>${n}</th>`).join("")}</tr>
    ${rows.map(([k, label]) => `<tr><td>${label}</td>${names.map(([n]) => {
      const v = m[n]?.[k]; const hb = (k.startsWith("P@") && m[n]?.["human@" + k.slice(2)]) ? `<small>（人工 ${m[n]["human@" + k.slice(2)]}）</small>` : "";
      return `<td class="${v != null && v === best(k) ? "best" : ""}">${pct(v)} ${hb}</td>`;
    }).join("")}</tr>`).join("")}</table>
    <div class="rk-cols">${names.map(([n, t]) => `<div><h5>${t} 前 10</h5><ol class="rk-mini">${col(n)}</ol></div>`).join("")}</div>`;
}

function rkProcessTab(d, res) {
  const prob = d.problems || [];
  const trig = d.triggered_by || [];
  const ver = d.verify || [];
  const diff = d.diff?.["5"] || [];
  const later = res.versions.filter((v) => v.version > d.version);
  const probList = (ps) => `<ul class="small">${ps.map((p) => `<li>第 ${p.rank ?? "?"} 名「${esc(p.title)}」：${esc(p.issue)}${p.credibility != null ? `（可信度 ${p.credibility}）` : ""}</li>`).join("")}</ul>`;
  if (d.version === 1 || !d.diff_from) {
    return `<ol class="rk-steps"><li><b>发现问题</b>：检查速览榜前 5 名的可信度
      ${prob.length ? probList(prob) : `<p class="small">没有发现可信度问题。</p>`}</li>
      <li><b>补充核验 → 重新评估</b>：${later.length ? `已完成，见 ${later.map((v) => `<a href="#" data-rkv="${v.version}">第 ${v.version} 版 · ${esc(v.trigger)}</a>`).join("、")}`
        : `还没有做。点上方“补充核验并重新评估”，或在榜单里对单条资料点“核验”。`}</li></ol>`;
  }
  return `<ol class="rk-steps">
    <li><b>发现问题</b>（第 ${d.diff_from} 版评估后）${trig.length ? probList(trig) : `<p class="small">本版由“${esc(d.trigger)}”触发，没有针对具体问题。</p>`}</li>
    <li><b>补充核验</b>
      ${ver.length ? `<ul class="small">${ver.map((r) => `<li>「${esc(r.title)}」：检索 ${r.queries.map(esc).join("、") || "无合适来源"}；
        相关结果 ${r.found.length} 条${r.found.length ? `（${r.found.slice(0, 3).map((f) => esc((f.title || "").slice(0, 30)) + (f.action === "already_known" ? "，已在库中" : "")).join("；")}）` : ""}；
        报道该事件的站点 ${r.hosts_before.length} → ${r.hosts_after.length}；
        <b>${r.outcome === "corroborated" ? "找到交叉印证，可信度提升" : "未找到印证，标注待核实，价值分打 85 折"}</b></li>`).join("")}</ul>` : `<p class="small">本版未做补充核验（只按最新数据、标注或权重重排）。</p>`}</li>
    <li><b>重新评估</b>：与第 ${d.diff_from} 版相比，速览榜变化 ${diff.length} 处
      ${diff.length ? `<table class="tbl small"><tr><th>资料</th><th>名次</th><th>价值分</th><th>可信度</th></tr>${diff.map((x) => `<tr><td>${esc(x.title.slice(0, 50))}</td>
        <td>${x.from ?? "未入选"} → ${x.to ?? "移出"}</td><td>${x.value_from?.toFixed(2) ?? "—"} → ${x.value_to?.toFixed(2) ?? "—"}</td>
        <td>${x.cred_from ?? "—"} → ${x.cred_to ?? "—"}</td></tr>`).join("")}</table>` : ""}</li>
    ${prob.length ? `<li><b>本版仍存在的问题</b>（可以继续核验）${probList(prob)}</li>` : ""}
  </ol>`;
}

function renderRanking() {
  const box = $("[data-f=ranking]");
  const res = current?.rk;
  if (!box) return;
  if (!res?.data) { box.innerHTML = ""; return; }
  const d = res.data, ev = res.eval, labels = ev.labels || {};
  const keys = Object.keys(d.lists).sort((a, b) => +a - +b);
  const tabs = [...keys.map((k) => [k, k === "5" ? "5 分钟速览" : k === "30" ? "30 分钟深读" : `你的 ${k} 分钟`]),
    ["base", "基线对比与指标"], ["proc", "发现问题 → 核验 → 重评"]];
  const tab = current.rkTab && tabs.some(([k]) => k === current.rkTab) ? current.rkTab :
    (d.pref_minutes && d.lists[String(d.pref_minutes)] ? String(d.pref_minutes) : "5");
  const w = d.weights;
  box.innerHTML = `<div class="card ranking">
    <div class="rk-head"><h3>价值榜单</h3>
      <select data-rk="version">${res.versions.map((v) => `<option value="${v.version}" ${v.version === d.version ? "selected" : ""}>第 ${v.version} 版 · ${esc(v.trigger)}</option>`).join("")}</select>
      <button class="ghost" data-rk="verify" title="对速览榜前列的可疑资料补充核验，然后重新评估">补充核验并重新评估</button>
      <button class="ghost" data-rk="weights-toggle">调整权重</button></div>
    <p class="small">价值分 = 相关度 ×${w.relevance} + 可信度 ×${w.credibility} + 新颖性 ×${w.novelty} + 时效性 ×${w.timeliness}；阅读时间作为预算；门槛 ${d.min_rel} 以下不入榜${d.pref_minutes ? `；你设置的阅读时间是 ${d.pref_minutes} 分钟` : ""}。</p>
    <form class="rk-weights" data-rk="weights" hidden>${FACTORS.map(([k, n]) => `<label>${n}<input name="${k}" type="number" step="0.05" min="0" max="1" value="${w[k]}"></label>`).join("")}<button>按新权重重排</button></form>
    <div class="tabs">${tabs.map(([k, n]) => `<button class="tab ${k === tab ? "on" : ""}" data-rktab="${k}">${n}</button>`).join("")}</div>
    <div class="rk-pane">${tab === "base" ? rkBaselineTab(d, ev, labels) : tab === "proc" ? rkProcessTab(d, res) : rkListTab(d, tab, labels, res.topic)}</div>
    <p class="small rk-msg" data-rk="msg"></p></div>`;
  box.querySelectorAll("[data-rktab]").forEach((b) => b.onclick = () => {
    current.rkTab = b.dataset.rktab;
    const tabs = store.get("rkTabs") || {};
    tabs[current.id] = current.rkTab;
    store.set("rkTabs", tabs);
    renderRanking();
  });
  box.querySelector("[data-rk=version]").onchange = (e) => { current.rkVersion = +e.target.value; loadRanking(true); };
  box.querySelectorAll("[data-rkv]").forEach((a) => a.onclick = (e) => { e.preventDefault(); current.rkVersion = +a.dataset.rkv; loadRanking(true); });
  box.querySelector("[data-rk=weights-toggle]").onclick = () => { const f = box.querySelector("[data-rk=weights]"); f.hidden = !f.hidden; };
  box.querySelector("[data-rk=weights]").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    rerank({ weights: Object.fromEntries(FACTORS.map(([k]) => [k, Number(f.get(k))])), trigger: "调整权重后重新评估" });
  };
  box.querySelector("[data-rk=verify]").onclick = () => rerank({ verify: true });
  box.querySelectorAll("[data-verify]").forEach((b) => b.onclick = () => rerank({ verify: true, doc_ids: [b.dataset.verify], trigger: "用户要求核验后重新评估" }));
  box.querySelectorAll("[data-label]").forEach((b) => b.onclick = async () => {
    const lab = +b.dataset.label, doc = b.dataset.doc;
    const cur = labels[doc];
    await api("/api/labels", { method: "POST", body: { topic: res.topic, doc_id: doc, label: cur === lab ? null : lab } });
    loadRanking(true);
  });
}

async function rerank(body) {
  const msg = $("[data-rk=msg]");
  const id = current.id;
  if (msg) msg.textContent = body.verify ? "正在补充核验（联网检索），完成后自动显示新版本…" : "正在重新评估…";
  try {
    const r = await api(`/api/tasks/${id}/rerank`, { method: "POST", body });
    refreshList();
    current.rkVersion = null;
    current.rkTab = "proc";
    if (!body.verify) return loadRanking(true);
    const before = current.rk?.versions.length || 0;
    const wait = async (n = 0) => {
      if (current?.id !== id) return;
      const res = await api(`/api/tasks/${id}/ranking`).catch(() => null);
      const t = await api(`/api/tasks/${r.task_id}`).catch(() => null);
      if (res && res.versions.length > before) return loadRanking(true);
      if (t && ["failed", "cancelled"].includes(t.status)) { if (msg) msg.textContent = `重新评估失败：${t.error || t.status}`; return; }
      if (n < 60) setTimeout(() => wait(n + 1), 2000);
    };
    wait();
  } catch (err) { if (msg) msg.textContent = err.message; }
}
