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
  const events = document.querySelector("#events"); events.replaceChildren();
  for (const event of [...state.recent_events].reverse()) {
    const row = document.createElement("tr");
    for (const value of [event.ts || "", event.event || "", event.step || ""]) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
    events.append(row);
  }
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
