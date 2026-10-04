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
  renderPullRequest(state.pull_request);
  mirrorRuns = state.token_runs || []; drawTokens();
  const events = document.querySelector("#events"); events.replaceChildren();
  for (const event of [...state.recent_events].reverse()) {
    const row = document.createElement("tr");
    for (const value of [event.ts || "", event.event || "", event.step || ""]) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
    events.append(row);
  }
}

// 承認した回の PR。承認と同時に出す。失敗したら、この機械の前から出し直せる。
function renderPullRequest(value) {
  const section = document.querySelector("#pull-request-section");
  const box = document.querySelector("#pull-request"); box.replaceChildren(); box.className = "live";
  section.hidden = !value;
  if (!value) return;
  const result = value.result;
  if (result?.url) {
    const link = document.createElement("a");
    link.href = result.url; link.textContent = result.url; link.target = "_blank"; link.rel = "noopener";
    box.append(text("strong", `${result.head} → ${result.base}`), link,
               text("p", `${result.created ? "作成" : "更新"}: ${result.ts}`, "why"));
  } else {
    box.classList.add("error");
    box.append(text("strong", result ? "PR を出せませんでした" : "PR はまだ出していません"));
    if (result) box.append(text("p", result.error, "why"));
  }
  if (session.scope !== "local") return;
  const again = document.createElement("button");
  again.textContent = result?.url ? "PR を更新" : "PR を出す";
  again.onclick = async () => {
    again.disabled = true;
    try { await api("/api/pull-request", {method: "POST", body: "{}"}); await refresh(); }
    catch (error) { alert(error.message); again.disabled = false; }
  };
  box.append(again);
}

// 色は役に付く。回ごとの順位では付けない。
const ROLES = [
  {key: "planner", label: "プランナー", color: "#3987e5"},
  {key: "critic", label: "クリティック", color: "#d95926"},
  {key: "solver", label: "ソルバー", color: "#199e70"},
];
// 種類は積み上げの下から並べる。色は役とは別の並びで、隣り合う色どうしを見分けられる。
const KINDS = [
  {key: "cache_read", label: "キャッシュ読み取り", color: "#9085e9"},
  {key: "cache_write", label: "キャッシュ書き込み", color: "#c98500"},
  {key: "input", label: "入力", color: "#d55181"},
  {key: "output", label: "出力", color: "#008300"},
];
const OUTCOME = {green: "完了", stopped: "停止", running: "未完了", abandoned: "中断", active: "走行中"};
const SVG = "http://www.w3.org/2000/svg";
const compact = new Intl.NumberFormat("ja-JP", {notation: "compact", maximumFractionDigits: 1});
const exact = new Intl.NumberFormat("ja-JP");
const METRIC = {
  tokens: {label: "トークン", value: (run, role) => run.tokens[role],
           axis: value => compact.format(value), exact: value => exact.format(value)},
  usd: {label: "USD", value: (run, role) => run.usd?.[role] ?? 0,
        axis: value => `$${Number(value.toFixed(2))}`, exact: value => `$${value.toFixed(2)}`},
};
// 役割のグラフに出す量と、種類のグラフに出す役。all は3役の和。
let roleMetric = "tokens", kindRole = "all", shownRuns = [];

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

// 写しの履歴（/api/state）と、箱が今の回についてログから数えたもの（/api/live）。
let mirrorRuns = [], liveLoop = null, drawnTokens = "";

// 箱の回を写しの履歴に重ねる。写しに届くのはコミットのときだけなので、同じ回なら
// 箱の数を使う。同じ回かどうかは、同じプロジェクトで開始が5分以内かで決める。
// 箱はログの先頭の時刻、写しは台帳の PLAN_BOOTSTRAP の時刻を持つ。
function mergeRuns(mirror, live) {
  const runs = mirror.map(run => ({...run}));
  if (live?.loop?.calls) {
    const at = parseStamp(live.loop.started);
    const entry = {source: `箱: ${live.project}`, started: live.loop.started, tokens: live.loop.tokens,
                   outcome: live.loop.outcome === "running" ? "active" : live.loop.outcome};
    // 箱のログに read と write が無い（それを出す前のランナーの）回は、種類別を
    // 数えられない。USD を返さない古い `loop now` もある。そのときは写しの値を残す。
    if (live.loop.kinds) entry.kinds = live.loop.kinds;
    if (live.loop.usd) entry.usd = live.loop.usd;
    const same = runs.findIndex(run => [`projects/${live.project}`, "project"].includes(run.source)
                                       && Math.abs(parseStamp(run.started) - at) < 5 * 60000);
    if (same >= 0) runs[same] = {...runs[same], ...entry}; else runs.push(entry);
  }
  runs.sort((a, b) => parseStamp(a.started) - parseStamp(b.started));
  runs.forEach((run, index) => { run.run = index + 1; });
  return runs;
}

// 開いたまま5秒ごとに描き直すと、ツールチップが消える。変わったときだけ描く。
function drawTokens() {
  const runs = mergeRuns(mirrorRuns, liveLoop), key = JSON.stringify(runs);
  if (key === drawnTokens) return;
  drawnTokens = key;
  shownRuns = runs;
  renderRoles(runs);
  renderKinds(runs);
}

// ボタンの組の1つを押された状態にし、その値を渡す。
function toggle(selector, onChange) {
  const group = document.querySelector(selector);
  for (const element of group.querySelectorAll("button")) {
    element.onclick = () => {
      for (const other of group.querySelectorAll("button")) other.setAttribute("aria-pressed", other === element);
      onChange(element.dataset.value);
    };
  }
}

const CRITIQUE_MODE = {
  coverage: {label: "要件を満たすか", why: "計画をやり遂げたとき、要件が満たされるか"},
  trace: {label: "使う人の操作で届くか", why: "計画に、使う人の操作では届かない部分が無いか"},
};
// 5秒ごとの更新で、書きかけの入力を消さない。中身が変わったときだけ描き直す。
let drawnCritique = "", editingFinding = false;

// クリティックの指摘。箱の CRITIQUE.json があれば指摘ごとに出し、無ければログの段落を出す。
function renderCritique(value) {
  const findings = value.findings;
  const key = JSON.stringify([findings, value.running, findings ? null : value.loop?.critique]);
  if (editingFinding || key === drawnCritique) return;
  drawnCritique = key;
  const box = document.querySelector("#critique"); box.replaceChildren();
  const at = document.querySelector("#critique-at");
  if (!findings) {
    const critique = value.loop?.critique;
    at.textContent = critique?.at || "";
    box.append(critique ? text("pre", critique.text)
                        : text("p", "今の回にクリティックの指摘はありません。", "why"));
    return;
  }
  at.textContent = `${findings.round}回目の批評 / ${findings.at}`;
  const editable = findings.waiting && !value.running;
  if (editable) {
    box.append(text("p", "改訂の上限まで回しても残った指摘です。的外れなものは書き換えてください。"
      + "「続ける」を押すと、書き換えた指摘でプランナーが1回だけ計画を直し、適用して走らせます。"
      + "何も書き換えずに押すと、今の計画をそのまま適用します。", "why"));
  }
  for (const [mode, items] of Object.entries(findings.modes)) {
    const group = document.createElement("div"); group.className = "finding-group";
    group.append(text("h3", CRITIQUE_MODE[mode]?.label || mode),
                 text("p", CRITIQUE_MODE[mode]?.why || "", "why"));
    if (!items.length) group.append(text("p", "指摘なし", "why"));
    items.forEach((item, index) => group.append(findingCard(mode, index, item, editable)));
    box.append(group);
  }
  if (editable) box.append(continueButton());
}

function findingCard(mode, index, item, editable) {
  const card = document.createElement("article"); card.className = "finding";
  const head = document.createElement("div"); head.className = "finding-head";
  head.append(text("strong", `${index + 1}. ${item.title}`));
  if (item.machine_would_notice === false) head.append(text("span", "どの関門も気づかない", "badge warn"));
  card.append(head, text("p", item.evidence, "evidence"));
  if (!editable) return card;
  const edit = document.createElement("button"); edit.textContent = "書き換える";
  edit.onclick = () => { editingFinding = true; card.replaceWith(findingEditor(mode, index, item)); };
  card.append(edit);
  return card;
}

function findingEditor(mode, index, item) {
  const card = document.createElement("article"); card.className = "finding editing";
  const title = document.createElement("input"); title.value = item.title; title.maxLength = 4000;
  const evidence = document.createElement("textarea"); evidence.value = item.evidence; evidence.maxLength = 4000;
  const save = document.createElement("button"); save.textContent = "保存";
  const cancel = document.createElement("button"); cancel.textContent = "取り消す"; cancel.className = "quiet";
  const done = () => { editingFinding = false; drawnCritique = ""; refreshLive(); };
  save.onclick = async () => {
    save.disabled = true;
    try {
      await api("/api/findings", {method: "POST", body: JSON.stringify(
        {mode, index, title: title.value, evidence: evidence.value})});
      done();
    } catch (error) { alert(error.message); save.disabled = false; }
  };
  cancel.onclick = done;
  const actions = document.createElement("div"); actions.className = "request-actions";
  actions.append(cancel, save);
  card.append(text("span", "題"), title, text("span", "根拠"), evidence, actions);
  return card;
}

function continueButton() {
  const element = document.createElement("button"); element.textContent = "続ける（loop continue）";
  element.onclick = async () => {
    if (!confirm("書き換えた指摘で計画を直し、適用して走らせます。よいですか？")) return;
    element.disabled = true;
    try { await api("/api/continue", {method: "POST", body: "{}"}); drawnCritique = ""; await refreshLive(); }
    catch (error) { alert(error.message); element.disabled = false; }
  };
  return element;
}

const CHART = {width: 800, height: 280, top: 12, bottom: 30, left: 56};

function renderLegend(selector, items) {
  const legend = document.querySelector(selector); legend.replaceChildren();
  for (const item of items) {
    const entry = text("span", item.label); const swatch = document.createElement("i");
    swatch.style.background = item.color; entry.prepend(swatch); legend.append(entry);
  }
}

function tipLine(color, label, value) {
  const line = document.createElement("div"), swatch = document.createElement("i");
  if (color) swatch.style.background = color; else swatch.style.visibility = "hidden";
  line.append(swatch, text("span", label), text("b", value));
  return line;
}

function tipHeading(tip, run) {
  tip.replaceChildren(text("strong", `${run.run}回目（${OUTCOME[run.outcome] || run.outcome}）`),
                      text("p", `${run.source} / ${run.started}`, "why"));
}

// 横軸の at（viewBox の座標）の脇にツールチップを出す。右半分なら左に出す。
function placeTip(chart, root, tip, at) {
  const box = root.getBoundingClientRect(), scale = box.width / CHART.width;
  const offset = chart.getBoundingClientRect();
  const px = at * scale + box.left - offset.left;
  tip.hidden = false;
  tip.style.top = `${CHART.top * scale}px`;
  tip.style.left = px > offset.width / 2 ? "" : `${px + 20}px`;
  tip.style.right = px > offset.width / 2 ? `${offset.width - px + 20}px` : "";
}

// 目盛りと横軸の回の名前。y は値を縦の座標にする関数、x は回の番号を横の座標にする関数。
function drawAxes(root, runs, max, format, x, y, right) {
  for (let tick = 0; tick <= 4; tick++) {
    const value = max * tick / 4;
    root.append(svg("line", {x1: CHART.left, x2: CHART.width - right, y1: y(value), y2: y(value), class: "grid"}));
    const label = svg("text", {x: CHART.left - 8, y: y(value) + 4, "text-anchor": "end", class: "axis"});
    label.textContent = format(value); root.append(label);
  }
  const every = Math.ceil(runs.length / 12);
  runs.forEach((run, index) => {
    if (index % every && index !== runs.length - 1) return;
    const label = svg("text", {x: x(index), y: CHART.height - 8, "text-anchor": "middle", class: "axis"});
    label.textContent = `${run.run}回目`; root.append(label);
  });
}

function renderRoles(runs) {
  const metric = METRIC[roleMetric];
  renderLegend("#token-legend", ROLES);

  const table = document.querySelector("#token-table"); table.replaceChildren();
  for (const run of [...runs].reverse()) {
    const row = document.createElement("tr");
    row.append(text("td", run.run), text("td", run.source), text("td", run.started),
               text("td", OUTCOME[run.outcome] || run.outcome),
               ...ROLES.map(role => text("td", metric.exact(metric.value(run, role.key)))));
    table.append(row);
  }

  const chart = document.querySelector("#token-chart"); chart.replaceChildren();
  if (!runs.length) { chart.append(text("p", "写しの台帳に消費の記録がまだありません。", "why")); return; }

  const {width, height, left, top, bottom} = CHART, right = 96;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const max = niceMax(Math.max(...runs.flatMap(run => ROLES.map(role => metric.value(run, role.key)))));
  const x = index => left + (runs.length === 1 ? plotWidth / 2 : index * plotWidth / (runs.length - 1));
  const y = value => top + plotHeight - value / max * plotHeight;
  const root = svg("svg", {viewBox: `0 0 ${width} ${height}`, role: "img",
                           "aria-label": `run --all の回ごとの、役割別の消費（${metric.label}）`});
  drawAxes(root, runs, max, metric.axis, x, y, right);

  const cross = svg("line", {y1: top, y2: top + plotHeight, class: "cross", visibility: "hidden"});
  root.append(cross);
  for (const role of ROLES) {
    const points = runs.map((run, index) => `${x(index)},${y(metric.value(run, role.key))}`).join(" ");
    root.append(svg("polyline", {points, class: "series", stroke: role.color}));
    runs.forEach((run, index) => root.append(
      svg("circle", {cx: x(index), cy: y(metric.value(run, role.key)), r: 4, fill: role.color, class: "dot"})));
  }

  // 右端の値に役の名前を添える。重なる分は上下に押し広げる。
  const last = runs.length - 1;
  const labels = ROLES.map(role => ({role, at: y(metric.value(runs[last], role.key))})).sort((a, b) => a.at - b.at);
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
    tipHeading(tip, run);
    for (const role of ROLES) tip.append(tipLine(role.color, role.label, metric.exact(metric.value(run, role.key))));
    placeTip(chart, root, tip, x(index));
  };
  hit.onmouseleave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
  root.append(hit);
  chart.style.position = "relative";
  chart.append(root, tip);
}

// 選んだ役の、種類別のトークン数。内訳の無い回は null。
function kindValues(run) {
  if (!run.kinds) return null;
  const roles = kindRole === "all" ? ROLES.map(role => role.key) : [kindRole];
  return Object.fromEntries(KINDS.map(kind => [
    kind.key, roles.reduce((sum, role) => sum + (run.kinds[role]?.[kind.key] || 0), 0)]));
}

function kindTotal(values) {
  return values ? KINDS.reduce((sum, kind) => sum + values[kind.key], 0) : 0;
}

function renderKinds(runs) {
  renderLegend("#kind-legend", KINDS);
  const values = runs.map(kindValues);

  const table = document.querySelector("#kind-table"); table.replaceChildren();
  runs.map((run, index) => [run, values[index]]).reverse().forEach(([run, value]) => {
    const row = document.createElement("tr");
    row.append(text("td", run.run), text("td", run.source), text("td", run.started),
               ...KINDS.map(kind => text("td", value ? exact.format(value[kind.key]) : "—")));
    table.append(row);
  });

  const chart = document.querySelector("#kind-chart"); chart.replaceChildren();
  if (!values.some(Boolean)) { chart.append(text("p", "種類別の記録がまだありません。", "why")); return; }

  const {width, height, left, top, bottom} = CHART, right = 16, gap = 2;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const max = niceMax(Math.max(...values.map(kindTotal)));
  const band = plotWidth / runs.length, barWidth = Math.min(36, band * 0.6);
  const x = index => left + band * (index + 0.5);
  const y = value => top + plotHeight - value / max * plotHeight;
  const root = svg("svg", {viewBox: `0 0 ${width} ${height}`, role: "img",
                           "aria-label": "run --all の回ごとの、トークンの種類別の消費"});
  drawAxes(root, runs, max, value => compact.format(value), x, y, right);

  const tip = document.createElement("div"); tip.className = "tip"; tip.hidden = true;
  const bars = runs.map((run, index) => {
    const group = svg("g", {class: "bar"});
    let base = 0;
    for (const kind of KINDS) {
      const value = values[index]?.[kind.key] || 0;
      if (!value) continue;
      // 下の段との間に、面の色の隙間を2px空ける。
      const segment = y(base) - y(base + value) - (base ? gap : 0);
      if (segment > 0) {
        group.append(svg("rect", {x: x(index) - barWidth / 2, y: y(base + value), width: barWidth,
                                  height: segment, fill: kind.color}));
      }
      base += value;
    }
    root.append(group);
    return group;
  });

  runs.forEach((run, index) => {
    const hit = svg("rect", {x: left + band * index, y: top, width: band, height: plotHeight, fill: "transparent"});
    hit.onmouseenter = () => {
      bars.forEach((bar, other) => bar.classList.toggle("dim", other !== index));
      tipHeading(tip, run);
      const value = values[index];
      if (!value) {
        tip.append(text("p", "種類別の内訳がありません", "why"));
      } else {
        const total = kindTotal(value);
        for (const kind of [...KINDS].reverse()) tip.append(tipLine(kind.color, kind.label, exact.format(value[kind.key])));
        tip.append(tipLine("", "合計", exact.format(total)),
                   tipLine("", "キャッシュ読み取りの割合",
                           `${total ? Math.round(value.cache_read / total * 100) : 0}%`));
      }
      placeTip(chart, root, tip, x(index));
    };
    hit.onmouseleave = () => { tip.hidden = true; bars.forEach(bar => bar.classList.remove("dim")); };
    root.append(hit);
  });
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
function parseStamp(stamp) {
  return Date.parse(String(stamp).replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));
}

function minutesSince(stamp) {
  const at = parseStamp(stamp);
  return Number.isNaN(at) ? null : Math.max(0, Math.floor((Date.now() - at) / 60000));
}

function renderLive(value) {
  const live = document.querySelector("#live"); live.replaceChildren();
  live.className = "live";
  document.querySelector("#live-updated").textContent =
    `${new Date().toLocaleTimeString()} 更新`;
  liveLoop = value.error ? null : value; drawTokens();
  if (value.error) {
    live.classList.add("error");
    live.append(text("strong", "箱に届きません"), text("p", value.error, "why"));
    return;
  }
  renderCritique(value);
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
  let value;
  try { value = await api("/api/live"); }
  catch (error) { value = {error: error.message}; }
  renderLive(value);
  renderControl(value);
}

// ---- 操作 -------------------------------------------------------------------

const ROLE_LABEL = Object.fromEntries(ROLES.map(role => [role.key, role.label]));
const PROJECT_STATE = {current: "使用中", parked: "退避中", new: "未構築"};
// モデル欄の候補。ここに無い名前も打てる。
const MODEL_OPTIONS = ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5",
                       "claude-haiku-4-5-20251001", "opus", "sonnet", "haiku"];
let liveState = {running: false, switching: false, reachable: false}, drawnProjects = "";

// 箱への操作を1つ流す。押したボタンは終わるまで押せなくする。
async function act(element, path, body, question) {
  if (question && !confirm(question)) return;
  element.disabled = true;
  try {
    const result = await api(path, {method: "POST", body: JSON.stringify(body || {})});
    if (result.message) alert(result.message);
    drawnCritique = ""; drawnProjects = "";
    await refreshLive();
  } catch (error) { alert(error.message); }
  finally { updateRunButtons(); }
}

function updateRunButtons() {
  const {running, switching, reachable} = liveState;
  document.querySelector("#start-run").disabled = !reachable || running || switching;
  document.querySelector("#continue-run").disabled = !reachable || running || switching;
  document.querySelector("#stop-run").disabled = !reachable || !running;
}

function renderControl(value) {
  const switching = Boolean(value.switch?.running);
  liveState = {running: Boolean(value.running), switching, reachable: !value.error};
  updateRunButtons();

  const state = document.querySelector("#run-state");
  state.classList.toggle("running", liveState.running);
  state.textContent = value.error ? "箱に届きません"
    : switching ? "プロジェクトを切り替えています"
    : value.running ? `走っています（${value.project}）`
    : `止まっています（${value.project}）${value.last ? ` / 最後: ${value.last}` : ""}`;

  const box = document.querySelector("#switch");
  box.hidden = !value.switch;
  if (value.switch) {
    document.querySelector("#switch-state").textContent =
      switching ? "切り替え中。初めてのプロジェクトはプロビジョニングで数分かかります" : "最後の切り替えの出力";
    document.querySelector("#switch-log").textContent = value.switch.log.join("\n");
  }
  renderProjects(value.projects || [], value.error);
}

function renderProjects(projects, error) {
  const key = JSON.stringify([projects, error, liveState]);
  if (key === drawnProjects) return;
  drawnProjects = key;
  const body = document.querySelector("#projects"); body.replaceChildren();
  if (!projects.length) {
    const row = document.createElement("tr"), cell = text("td", error ? "—" : "プロジェクトがありません", "goal");
    cell.colSpan = 4; row.append(cell); body.append(row);
    return;
  }
  for (const project of projects) {
    const row = document.createElement("tr");
    if (project.state === "current") row.className = "current";
    const cell = document.createElement("td");
    if (project.state !== "current" && session.scope === "local") {
      const use = document.createElement("button"); use.textContent = "切り替える";
      use.disabled = liveState.running || liveState.switching;
      use.onclick = () => act(use, "/api/project", {project: project.name},
        `'${project.name}' に切り替えます。今のプロジェクトは退避されます。よいですか？`);
      cell.append(use);
    }
    row.append(text("td", project.name), text("td", PROJECT_STATE[project.state] || project.state),
               text("td", project.branch || "?"), cell);
    body.append(row);
  }
}

async function loadSettings() {
  const box = document.querySelector("#models");
  let value;
  try { value = await api("/api/settings"); } catch (error) { value = {error: error.message}; }
  box.replaceChildren();
  if (value.error) { box.append(text("p", `読めません: ${value.error}`, "why")); return; }
  const language = document.querySelector("#language"), chosen = language.value;
  language.replaceChildren(...value.languages.map(name => new Option(name, name)));
  if (value.languages.includes(chosen)) language.value = chosen;
  for (const [role, current] of Object.entries(value.roles)) box.append(modelRow(role, current, value.efforts));
}

function modelRow(role, current, efforts) {
  const row = document.createElement("div"); row.className = "model-row";
  row.append(text("span", ROLE_LABEL[role] || role));
  if (!current) { row.append(text("p", `/etc/loop/${role}.env がありません`, "why")); return row; }
  const model = document.createElement("input");
  model.value = current.model; model.placeholder = "既定"; model.setAttribute("list", "model-options");
  const effort = document.createElement("select");
  effort.append(new Option("既定", ""), ...efforts.map(name => new Option(name, name)));
  effort.value = current.effort;
  row.append(model, effort);
  if (session.scope !== "local") { model.disabled = effort.disabled = true; return row; }
  const save = document.createElement("button"); save.textContent = "保存";
  save.onclick = async () => {
    save.disabled = true;
    try {
      await api("/api/model", {method: "POST", body: JSON.stringify(
        {role, model: model.value.trim(), effort: effort.value})});
      await loadSettings();
    } catch (error) { alert(error.message); save.disabled = false; }
  };
  row.append(save);
  return row;
}

async function startRun(element) {
  const file = document.querySelector("#requirements").files[0];
  if (!file) { alert("要件のファイルを選んでください。"); return; }
  const language = document.querySelector("#language").value;
  await act(element, "/api/start", {requirements: await file.text(), language},
    `${file.name} を要件にして、${language} で計画づくりから走らせます。よいですか？`);
}

let pullTimer = null;

function renderPull(value) {
  const state = document.querySelector("#pull-state"), log = document.querySelector("#pull-log");
  const button = document.querySelector("#pull");
  button.disabled = Boolean(value.running);
  log.hidden = !value.output?.length;
  log.textContent = (value.output || []).join("\n");
  state.textContent = value.error || (value.running ? "更新中"
    : value.finished ? `${value.returncode === 0 ? "済み" : `失敗（${value.returncode}）`} / ${value.finished}` : "");
}

async function watchPull() {
  clearTimeout(pullTimer);
  let value;
  try { value = await api("/api/pull"); } catch (error) { value = {error: error.message}; }
  renderPull(value);
  if (value.running) pullTimer = setTimeout(watchPull, 2000);
  else if (value.finished) await refresh();
}

function setupControl() {
  document.querySelector("#model-options").append(...MODEL_OPTIONS.map(name => new Option(name)));
  if (session.scope !== "local") {
    for (const element of document.querySelectorAll(".local-only")) element.hidden = true;
    const note = document.querySelector("#control-note");
    note.hidden = false;
    note.textContent = "リモートからは停止と続行だけができます。開始、プロジェクトの切り替え、モデルの変更、写しの更新はこの機械の前で。";
  }
  const start = document.querySelector("#start-run");
  start.onclick = () => startRun(start);
  const resume = document.querySelector("#continue-run");
  resume.onclick = () => act(resume, "/api/continue", {}, "止まったところから続けます。よいですか？");
  const stop = document.querySelector("#stop-run");
  stop.onclick = () => act(stop, "/api/stop", {}, "走行を止めます。途中のステップはそのまま残ります。よいですか？");
  const pull = document.querySelector("#pull");
  pull.onclick = async () => {
    pull.disabled = true;
    try { renderPull(await api("/api/pull", {method: "POST", body: "{}"})); watchPull(); }
    catch (error) { alert(error.message); pull.disabled = false; }
  };
  updateRunButtons();
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
  setupControl();
  await refresh();
  await refreshLive();
  setInterval(refreshLive, 5000);
  loadSettings();
  if (session.scope === "local") watchPull();
}

document.querySelector("#refresh").onclick = refresh;
toggle("#role-metric", value => { roleMetric = value; renderRoles(shownRuns); });
toggle("#kind-role", value => { kindRole = value; renderKinds(shownRuns); });
start().catch(error => { document.querySelector("#phase").textContent = error.message; });
