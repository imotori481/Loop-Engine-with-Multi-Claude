let session;

async function api(path, options = {}) {
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (options.method === "POST") headers["X-Loop-Token"] = session.token;
  const response = await fetch(path, {...options, headers, cache: "no-store"});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}

function button(label, decision, item, note) {
  const element = document.createElement("button");
  element.textContent = label;
  element.onclick = async () => {
    element.disabled = true;
    try {
      await api("/api/decision", {method: "POST", body: JSON.stringify({kind: item.kind, request_id: item.id, decision, note: note.value})});
      await refresh();
    } catch (error) { alert(error.message); element.disabled = false; }
  };
  return element;
}

function note_only(text) {
  const element = document.createElement("p");
  element.className = "why"; element.textContent = text;
  return element;
}

async function refresh() {
  const state = await api("/api/state");
  document.querySelector("#project").textContent = state.project;
  document.querySelector("#phase").textContent = state.phase;
  document.querySelector("#progress").textContent = `${state.steps.green} / ${state.steps.total}`;
  document.querySelector("#last-event").textContent = state.last_event?.event || "—";
  const pending = document.querySelector("#pending"); pending.replaceChildren();
  if (!state.pending.length) pending.textContent = "現在、人間の対応を待っている項目はありません。";
  for (const item of state.pending) {
    const card = document.querySelector("#request-template").content.cloneNode(true);
    card.querySelector(".badge").textContent =
      {review: "予定レビュー", planner: "プランナーからの差し戻し"}[item.kind] || "エスカレーション";
    card.querySelector("h3").textContent = item.title;
    card.querySelector("pre").textContent = item.detail;
    const note = card.querySelector("textarea"), actions = card.querySelector(".request-actions");
    if (item.kind === "review") {
      if (session.scope === "local") actions.append(button("承認", "approve", item, note));
      else actions.append(note_only("承認はこの機械の前でだけ ── 画面を見られない端末から「遊べる」とは記録できません"));
      actions.append(button("修正を依頼", "revise", item, note));
    } else {
      actions.append(button("回答を記録", "respond", item, note), button("停止", "stop", item, note));
    }
    pending.append(card);
  }
  renderTokens(state.token_runs || []);
  const events = document.querySelector("#events"); events.replaceChildren();
  for (const event of [...state.recent_events].reverse()) {
    const row = document.createElement("tr");
    for (const value of [event.ts || "", event.event || "", event.step || ""]) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
    events.append(row);
  }
}

// 色は役に付く。回ごとの順位では付けない。
const ROLES = [
  {key: "planner", label: "プランナー", color: "#3987e5"},
  {key: "critic", label: "クリティック", color: "#d95926"},
  {key: "solver", label: "ソルバー", color: "#199e70"},
];
const OUTCOME = {green: "完了", stopped: "停止", running: "未完了", abandoned: "中断"};
const SVG = "http://www.w3.org/2000/svg";
const compact = new Intl.NumberFormat("ja-JP", {notation: "compact", maximumFractionDigits: 1});
const exact = new Intl.NumberFormat("ja-JP");

function svg(tag, attributes) {
  const element = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  return element;
}

// 4等分した目盛りが切りのよい数になる上限。
function niceMax(value) {
  if (value <= 0) return 1;
  const unit = 10 ** Math.floor(Math.log10(value));
  return [1, 1.2, 1.6, 2, 2.4, 3, 4, 6, 8, 10].map(step => step * unit).find(step => step >= value);
}

function renderTokens(runs) {
  const legend = document.querySelector("#token-legend"); legend.replaceChildren();
  for (const role of ROLES) {
    const item = text("span", role.label); const swatch = document.createElement("i");
    swatch.style.background = role.color; item.prepend(swatch); legend.append(item);
  }

  const table = document.querySelector("#token-table"); table.replaceChildren();
  for (const run of [...runs].reverse()) {
    const row = document.createElement("tr");
    row.append(text("td", run.run), text("td", run.source), text("td", run.started),
               text("td", OUTCOME[run.outcome] || run.outcome),
               ...ROLES.map(role => text("td", exact.format(run.tokens[role.key]))));
    table.append(row);
  }

  const chart = document.querySelector("#token-chart"); chart.replaceChildren();
  if (!runs.length) { chart.append(text("p", "写しの台帳に消費の記録がまだありません。", "why")); return; }

  const width = 800, height = 280, left = 56, right = 96, top = 12, bottom = 30;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const max = niceMax(Math.max(...runs.flatMap(run => ROLES.map(role => run.tokens[role.key]))));
  const x = index => left + (runs.length === 1 ? plotWidth / 2 : index * plotWidth / (runs.length - 1));
  const y = value => top + plotHeight - value / max * plotHeight;
  const root = svg("svg", {viewBox: `0 0 ${width} ${height}`, role: "img",
                           "aria-label": "run --all の回ごとの、役割別トークン消費"});

  for (let tick = 0; tick <= 4; tick++) {
    const value = max * tick / 4;
    root.append(svg("line", {x1: left, x2: left + plotWidth, y1: y(value), y2: y(value), class: "grid"}));
    const label = svg("text", {x: left - 8, y: y(value) + 4, "text-anchor": "end", class: "axis"});
    label.textContent = compact.format(value); root.append(label);
  }
  const every = Math.ceil(runs.length / 12);
  runs.forEach((run, index) => {
    if (index % every && index !== runs.length - 1) return;
    const label = svg("text", {x: x(index), y: height - 8, "text-anchor": "middle", class: "axis"});
    label.textContent = `${run.run}回目`; root.append(label);
  });

  const cross = svg("line", {y1: top, y2: top + plotHeight, class: "cross", visibility: "hidden"});
  root.append(cross);
  for (const role of ROLES) {
    const points = runs.map((run, index) => `${x(index)},${y(run.tokens[role.key])}`).join(" ");
    root.append(svg("polyline", {points, class: "series", stroke: role.color}));
    runs.forEach((run, index) => root.append(
      svg("circle", {cx: x(index), cy: y(run.tokens[role.key]), r: 4, fill: role.color, class: "dot"})));
  }

  // 右端の値に役の名前を添える。重なる分は上下に押し広げる。
  const last = runs.length - 1;
  const labels = ROLES.map(role => ({role, at: y(runs[last].tokens[role.key])})).sort((a, b) => a.at - b.at);
  labels.forEach((item, index) => { if (index) item.at = Math.max(item.at, labels[index - 1].at + 15); });
  for (const item of labels) {
    const label = svg("text", {x: x(last) + 10, y: item.at + 4, class: "label"});
    label.textContent = item.role.label; root.append(label);
  }

  const tip = document.createElement("div"); tip.className = "tip"; tip.hidden = true;
  const hit = svg("rect", {x: left - 20, y: top, width: plotWidth + 40, height: plotHeight, fill: "transparent"});
  hit.onmousemove = event => {
    const box = root.getBoundingClientRect(), scale = box.width / width;
    const at = (event.clientX - box.left) / scale;
    const index = runs.length === 1 ? 0 : Math.max(0, Math.min(last, Math.round((at - left) / plotWidth * last)));
    const run = runs[index];
    cross.setAttribute("x1", x(index)); cross.setAttribute("x2", x(index)); cross.setAttribute("visibility", "visible");
    tip.replaceChildren(text("strong", `${run.run}回目（${OUTCOME[run.outcome] || run.outcome}）`),
                        text("p", `${run.source} / ${run.started}`, "why"));
    for (const role of ROLES) {
      const line = document.createElement("div"), swatch = document.createElement("i");
      swatch.style.background = role.color;
      line.append(swatch, text("span", role.label), text("b", exact.format(run.tokens[role.key])));
      tip.append(line);
    }
    tip.hidden = false;
    const offset = chart.getBoundingClientRect();
    const px = x(index) * scale + box.left - offset.left;
    tip.style.top = `${top * scale}px`;
    tip.style.left = px > offset.width / 2 ? "" : `${px + 20}px`;
    tip.style.right = px > offset.width / 2 ? `${offset.width - px + 20}px` : "";
  };
  hit.onmouseleave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
  root.append(hit);
  chart.style.position = "relative";
  chart.append(root, tip);
}

const STEP_STATE = {green: "完了", active: "作業中", pending: "未着手"};

function text(tag, value, className) {
  const element = document.createElement(tag);
  element.textContent = value;
  if (className) element.className = className;
  return element;
}

// ランナーの時刻は "+0900" の形で、Date はコロンの無いオフセットを読めない。
function minutesSince(stamp) {
  const at = Date.parse(String(stamp).replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));
  return Number.isNaN(at) ? null : Math.max(0, Math.floor((Date.now() - at) / 60000));
}

function renderLive(value) {
  const live = document.querySelector("#live"); live.replaceChildren();
  live.className = "live";
  document.querySelector("#live-updated").textContent =
    `${new Date().toLocaleTimeString()} 更新`;
  if (value.error) {
    live.classList.add("error");
    live.append(text("strong", "箱に届きません"), text("p", value.error, "why"));
    return;
  }
  const activity = value.now?.activity;
  if (value.running && activity) {
    const minutes = minutesSince(activity.since);
    live.append(text("span", activity.who_ja, "badge"), text("strong", activity.text_ja),
                text("p", [activity.step, minutes === null ? "" : `${minutes} 分経過`]
                  .filter(Boolean).join(" / "), "why"));
  } else {
    live.classList.add("idle");
    live.append(text("strong", value.running ? "作業の切り替え中" : "走っていません"));
    if (value.last) live.append(text("p", `最後の結果: ${value.last}`, "why"));
  }
  live.append(text("p", `プロジェクト: ${value.project}`, "why"));

  const steps = document.querySelector("#steps"); steps.replaceChildren();
  for (const step of value.now?.steps || []) {
    const state = step.state === "active" && !value.running ? "中断" : STEP_STATE[step.state] || step.state;
    const row = document.createElement("tr");
    if (step.state === "active" && value.running) row.className = "active";
    row.append(text("td", step.id), text("td", state), text("td", step.goal, "goal"));
    steps.append(row);
  }
}

async function refreshLive() {
  try { renderLive(await api("/api/live")); }
  catch (error) { renderLive({error: error.message}); }
}

async function start() {
  session = await api("/api/session");
  document.querySelector("#scope").textContent =
    session.scope === "local" ? "この機械" : `リモート（${session.user}）`;
  const launchers = document.querySelector("#launchers");
  if (session.scope !== "local") {
    launchers.append(note_only("成果物の起動はこの機械の前でだけできます。"));
  } else if (!session.launchers.length) {
    launchers.textContent = "config.json に起動可能な成果物を設定してください。";
  }
  for (const item of session.launchers) {
    const element = document.createElement("button"); element.textContent = `${item.label}を起動`;
    element.onclick = async () => { try { await api("/api/launch", {method: "POST", body: JSON.stringify({launcher_id: item.id})}); } catch (error) { alert(error.message); } };
    launchers.append(element);
  }
  await refresh();
  await refreshLive();
  setInterval(refreshLive, 5000);
}

document.querySelector("#refresh").onclick = refresh;
start().catch(error => { document.querySelector("#phase").textContent = error.message; });
