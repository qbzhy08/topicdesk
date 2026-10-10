// 主题编辑台前端：hash 路由 + 轮询。所有状态都来自后端，刷新后可完整重建。
const $ = (s, el = document) => el.querySelector(s);
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
const ACTIVE = new Set(["pending", "running", "cancelling"]);

let graphs = [];
let current = null;          // {id, lastStepId, timer}

async function init() {
  graphs = await api("/api/graphs");
  $("#new-task [name=graph]").innerHTML = graphs.map((g) => `<option>${esc(g.name)}</option>`).join("");
  $("#new-task").addEventListener("submit", submitTask);
  window.addEventListener("hashchange", route);
  route();
  refreshList();
  setInterval(refreshList, 4000);
}

async function submitTask(e) {
  e.preventDefault();
  const f = new FormData(e.target);
  let params = {};
  try { params = JSON.parse(f.get("params") || "{}"); } catch { return alert("参数不是合法 JSON"); }
  const budget = {};
  for (const k of ["max_steps", "max_tool_calls", "max_seconds", "max_cost"]) budget[k] = Number(f.get(k));
  try {
    const { id } = await api("/api/tasks", { method: "POST",
      body: { topic: f.get("topic"), graph: f.get("graph"), params, budget } });
    location.hash = `#/task/${id}`;
    refreshList();
  } catch (err) { alert(err.message); }
}

async function refreshList() {
  const rows = await api("/api/tasks");
  $("#task-list").innerHTML = rows.map((t) => `
    <li><a href="#/task/${t.id}" class="${current?.id === t.id ? "active" : ""}">
      <span class="t">${esc(t.topic)}</span>
      <span class="s">${esc(STATUS[t.status] || t.status)} · ${esc(t.graph)} · ${new Date(t.created_at * 1000).toLocaleString()}</span>
    </a></li>`).join("") || `<li class="s">暂无任务</li>`;
}

function route() {
  const m = location.hash.match(/^#\/task\/(\w+)/);
  if (current?.timer) clearTimeout(current.timer);
  if (!m) { current = null; return; }
  current = { id: m[1], lastStepId: 0, timer: null };
  const el = $("#detail");
  el.innerHTML = "";
  el.append($("#tpl-detail").content.cloneNode(true));
  el.querySelector("[data-act=resume]").onclick = () => act("resume");
  el.querySelector("[data-act=cancel]").onclick = () => act("cancel");
  el.querySelector("[data-act=rewind]").onclick = doRewind;
  poll();
}

async function act(name) {
  try { await api(`/api/tasks/${current.id}/${name}`, { method: "POST", body: {} }); poll(); }
  catch (err) { alert(err.message); }
}

async function doRewind() {
  const d = $("#detail");
  const node = $("[data-f=rewind-node]", d).value;
  const invalidate = $("[data-f=rewind-inv]", d).value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
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
    // 运行中的步骤可能还会更新：下次从第一个未结束步骤重新拉取
    const open = steps.find((s) => s.status === "running");
    if (open) current.lastStepId = open.id - 1;
    else if (steps.length) current.lastStepId = steps[steps.length - 1].id;
    const busy = ACTIVE.has(task.status) || task.running;
    current.timer = setTimeout(poll, busy ? 1200 : 6000);
  } catch (err) {
    $("[data-f=error]").textContent = err.message;
    current.timer = setTimeout(poll, 5000);
  }
}

function renderTask(t) {
  const d = $("#detail");
  $("[data-f=graph]", d).textContent = `流程 ${t.graph} · ${t.id}`;
  $("[data-f=topic]", d).textContent = t.topic;
  const b = $("[data-f=status]", d);
  b.textContent = STATUS[t.status] || t.status;
  b.className = `badge ${t.status}`;
  $("[data-f=node]", d).textContent = t.current_node && t.current_node !== "__end__"
    ? `下一步：${t.current_node} · 已保存 ${t.checkpoints} 个检查点` : `已保存 ${t.checkpoints} 个检查点`;
  $("[data-f=error]", d).textContent = t.error || "";
  $("[data-act=resume]", d).disabled = !["interrupted", "failed", "pending"].includes(t.status) || t.running;
  $("[data-act=cancel]", d).disabled = ["done", "cancelled"].includes(t.status);

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
}

function renderHitl(open) {
  const box = $("[data-f=hitl]");
  const key = open.map((h) => h.id).join();
  if (box.dataset.key === key) return;        // 避免轮询时清掉用户正在填写的内容
  box.dataset.key = key;
  box.innerHTML = open.map((h) => {
    const opts = h.payload.options;
    const body = h.kind === "choose" && Array.isArray(opts)
      ? opts.map((o, i) => `<label class="opt"><input type="checkbox" checked value="${i}"> ${esc(typeof o === "string" ? o : o.label || JSON.stringify(o))}</label>`).join("")
      : `<label>答复 (JSON 或文本)<textarea rows="4">${esc(JSON.stringify(h.payload.default ?? {}, null, 2))}</textarea></label>`;
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
        <details class="tc"><summary><span class="st-${c.status}">${esc(c.tool)} · ${esc(c.status)}</span> · ${c.latency_ms ?? 0}ms</summary>
          <pre>输入 ${esc(JSON.stringify(c.input, null, 1))}</pre>
          ${c.error ? `<pre class="error">${esc(c.error)}</pre>` : `<pre>输出 ${esc(c.output_preview ?? JSON.stringify(c.output))}</pre>`}
        </details>`).join("")}
      ${s.error ? `<details class="tc"><summary class="st-error">错误</summary><pre>${esc(s.error)}</pre></details>` : ""}`;
  }
}

init();
