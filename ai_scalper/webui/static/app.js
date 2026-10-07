"use strict";

const POLL = { state: 2000, performance: 10000, config: 30000, logs: 5000, doctor: 120000 };
const SPARK_MAX = 120;

let TOKEN = new URLSearchParams(location.search).get("token") || localStorage.getItem("webui.token") || "";
if (TOKEN) localStorage.setItem("webui.token", TOKEN);

let health = { mode: "?", token_required: false, hbot_found: false };
let signalHistory = [];
let lastPair = "";
let busy = false;

const el = (id) => document.getElementById(id);

function fmt(value, digits = 2) {
  if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function signed(value, digits = 2) {
  const n = Number(value);
  if (!isFinite(n)) return "—";
  return (n > 0 ? "+" : "") + n.toFixed(digits);
}

function cls(value) {
  const n = Number(value);
  if (!isFinite(n) || n === 0) return "";
  return n > 0 ? "pos" : "neg";
}

function uptime(seconds) {
  const s = Number(seconds);
  if (!isFinite(s) || s <= 0) return "—";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m ${Math.floor(s % 60)}s`;
}

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (TOKEN) headers["X-WebUI-Token"] = TOKEN;
  const res = await fetch(path, Object.assign({}, options, { headers }));
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (res.status === 401) {
    const entered = prompt("This dashboard is token-protected. Paste the token:");
    if (entered) { TOKEN = entered.trim(); localStorage.setItem("webui.token", TOKEN); }
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}

function toast(message, kind = "") {
  const node = el("toast");
  node.textContent = message;
  node.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => node.classList.add("hidden"), 5000);
}

function actionLog(message, kind = "") {
  const node = document.createElement("div");
  node.className = kind;
  const stamp = new Date().toTimeString().slice(0, 8);
  node.textContent = `${stamp}  ${message}`;
  const box = el("action-log");
  box.prepend(node);
  while (box.children.length > 8) box.removeChild(box.lastChild);
}

function showAlert(message) {
  const node = el("alert");
  if (!message) { node.classList.add("hidden"); return; }
  node.textContent = message;
  node.classList.remove("hidden");
}

function badge(text, kind = "") {
  const span = document.createElement("span");
  span.className = `badge ${kind}`;
  span.innerHTML = "";
  span.textContent = text;
  return span;
}

function richBadge(label, value, kind = "") {
  const span = document.createElement("span");
  span.className = `badge ${kind}`;
  span.textContent = `${label} `;
  const b = document.createElement("b");
  b.textContent = value;
  span.appendChild(b);
  return span;
}

/* ---------------------------------------------------------------- render: header */

function renderHeader(state) {
  const bot = state.bot || {};
  const badges = el("badges");
  badges.innerHTML = "";

  const running = state.mode === "demo" ? bot.running : (state.bot_ok && bot.running);
  badges.appendChild(running ? badge("● RUNNING", "ok") : badge("● STOPPED", "bad"));
  badges.appendChild(richBadge("mode", state.mode.toUpperCase(), state.mode === "demo" ? "demo" : ""));

  const feed = (state.ai || {}).feed || {};
  if (state.mode === "demo") {
    badges.appendChild(badge("MQTT demo", "demo"));
  } else {
    badges.appendChild(feed.connected ? badge("MQTT ●", "ok") : badge("MQTT ○", "warn"));
  }
  const errs = (bot.errors || {}).count || 0;
  if (errs) badges.appendChild(richBadge("errors", String(errs), "warn"));
  badges.appendChild(richBadge("uptime", uptime(bot.uptime_s)));
  if (bot.config) badges.appendChild(richBadge("config", bot.config));
  if (health.token_required) badges.appendChild(badge("token ✓"));

  el("subtitle").textContent = [bot.name, bot.strategy, bot.type]
    .filter(Boolean).join(" · ") || (state.bot_ok ? "no bot loaded" : "bot unreachable");
}

/* ----------------------------------------------------------------- render: signal */

function renderSignal(state) {
  const pairs = (state.ai || {}).pairs || {};
  const names = Object.keys(pairs);
  if (lastPair && pairs[lastPair]) { /* keep the selected pair stable */ }
  else if (names.length) lastPair = names[0];

  el("signal-pair").textContent = lastPair ? `${lastPair} · ${names.length} pair(s)` : "";
  const sig = lastPair ? pairs[lastPair] : null;

  if (!sig) {
    el("signal-direction").textContent = "NO SIGNAL";
    el("signal-direction").className = "signal-dir flat";
    el("signal-kv").innerHTML = "";
    ["conf", "short", "neutral", "long"].forEach((k) => {
      el(`${k}-fill`).style.width = "0%";
      el(`${k}-value`).textContent = "—";
    });
    return;
  }

  const probs = sig.probabilities || [0, 0, 0];
  const label = sig.signal === 1 ? "LONG" : sig.signal === -1 ? "SHORT" : "FLAT";
  const dir = el("signal-direction");
  dir.textContent = sig.stale ? `${label} · STALE` : label;
  dir.className = `signal-dir ${sig.stale ? "stale" : sig.signal === 1 ? "long" : sig.signal === -1 ? "short" : "flat"}`;

  const set = (key, value) => {
    el(`${key}-fill`).style.width = `${Math.max(0, Math.min(1, value)) * 100}%`;
    el(`${key}-value`).textContent = fmt(value, 3);
  };
  set("conf", sig.confidence);
  set("short", probs[0]);
  set("neutral", probs[1]);
  set("long", probs[2]);

  const th = sig.thresholds || {};
  const kv = el("signal-kv");
  kv.innerHTML = "";
  const items = [
    ["target_pct", fmt(sig.target_pct, 5), ""],
    ["price", sig.price ? fmt(sig.price, 2) : "—", ""],
    ["age", `${fmt(sig.age_s, 1)}s`, sig.stale ? "warn" : "ok"],
    ["timeout", `${fmt(th.timeout, 0)}s`, ""],
    ["thresholds", `${fmt(th.short, 2)} / ${fmt(th.long, 2)}`, ""],
    ["dropped", String(sig.malformed ?? sig.dropped ?? 0), (sig.malformed || sig.dropped) ? "warn" : ""],
    ["model", sig.model || "—", ""],
    ["source", sig.source || (state.mode === "demo" ? "demo" : "mqtt"), state.mode === "demo" ? "warn" : ""],
  ];
  for (const [name, value, kind] of items) {
    const box = document.createElement("div");
    const span = document.createElement("span");
    span.textContent = name;
    const b = document.createElement("b");
    b.textContent = value;
    if (kind) b.className = kind;
    box.appendChild(span);
    box.appendChild(b);
    kv.appendChild(box);
  }

  signalHistory.push({ long: probs[2], short: probs[0], neutral: probs[1], signal: sig.signal });
  if (signalHistory.length > SPARK_MAX) signalHistory.shift();
  renderSpark();
}

function renderSpark() {
  const svg = el("spark");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  if (signalHistory.length < 2) return;

  const w = 320, h = 60;
  const mid = document.createElementNS("http://www.w3.org/2000/svg", "line");
  mid.setAttribute("x1", 0); mid.setAttribute("x2", w);
  mid.setAttribute("y1", h / 2); mid.setAttribute("y2", h / 2);
  mid.setAttribute("stroke", "#232c3f"); mid.setAttribute("stroke-width", "1");
  svg.appendChild(mid);

  const line = (key, color) => {
    const pts = signalHistory.map((p, i) => {
      const x = (i / (SPARK_MAX - 1)) * w;
      const y = h - Math.max(0, Math.min(1, p[key])) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    path.setAttribute("points", pts);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", "1.4");
    svg.appendChild(path);
  };
  line("long", "#2ec27e");
  line("short", "#e5484d");
  line("neutral", "#4a5570");
}

/* --------------------------------------------------------------- render: status */

function renderStatus(state) {
  const bot = state.bot || {};
  el("format-status").textContent = bot.format_status || (state.bot_ok ? "(no strategy output)" : "(bot unreachable)");

  const tbody = el("balances").querySelector("tbody");
  tbody.innerHTML = "";
  const balances = bot.balances || {};
  const names = Object.keys(balances);
  if (!names.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "num";
    td.textContent = "no balance snapshot yet — run hbot status";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  for (const connector of names) {
    for (const [asset, amount] of Object.entries(balances[connector] || {})) {
      const tr = document.createElement("tr");
      for (const [value, numeric] of [[connector, false], [asset, false], [fmt(amount, 6), true]]) {
        const td = document.createElement("td");
        td.textContent = value;
        if (numeric) td.className = "num";
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  const running = state.mode === "demo" ? bot.running : (state.bot_ok && bot.running);
  el("btn-start").disabled = busy || running;
  el("btn-stop").disabled = busy || !running;
  el("btn-kill").disabled = busy;
  const demoBlocked = state.mode === "demo";
  if (demoBlocked) {
    el("btn-start").disabled = true;
    el("btn-stop").disabled = true;
    el("btn-kill").disabled = true;
  }
  el("control-note").textContent = demoBlocked
    ? "Demo mode: control disabled. Run without --demo against a real install to drive the bot."
    : "One bot per install. Start uses the loaded config; Stop cancels orders first, Kill switch also sets manual_kill_switch.";
}

/* ---------------------------------------------------------- render: performance */

function renderPerf(payload) {
  const data = payload.data || { rows: [], summary: {} };
  const s = data.summary || {};
  const tiles = el("perf-tiles");
  tiles.innerHTML = "";
  const items = [
    ["net pnl", signed(s.net_pnl, 2), cls(s.net_pnl)],
    ["gross", signed(s.gross_pnl, 2), cls(s.gross_pnl)],
    ["fees", fmt(s.fees, 2), "neg"],
    ["trades", String(s.trades ?? 0), ""],
    ["return %", `${signed(s.avg_return_pct, 3)}%`, cls(s.avg_return_pct)],
    ["fee ratio", s.fee_ratio === null || s.fee_ratio === undefined ? "—" : `${fmt(s.fee_ratio * 100, 1)}%`,
      s.fee_ratio >= 1 ? "neg" : ""],
  ];
  for (const [name, value, kind] of items) {
    const box = document.createElement("div");
    box.className = "tile";
    const span = document.createElement("span");
    span.textContent = name;
    const b = document.createElement("b");
    b.textContent = value;
    if (kind) b.className = kind;
    box.appendChild(span);
    box.appendChild(b);
    tiles.appendChild(box);
  }

  const tbody = el("history").querySelector("tbody");
  tbody.innerHTML = "";
  for (const row of data.rows || []) {
    const tr = document.createElement("tr");
    const cells = [
      [row.market, false, ""], [row.pair, false, ""], [String(row.trades ?? 0), true, ""],
      [signed(row.trade_pnl, 2), true, cls(row.trade_pnl)],
      [fmt(row.fees, 2), true, "neg"],
      [signed(row.total_pnl, 2), true, cls(row.total_pnl)],
      [`${signed(row["return%"], 3)}%`, true, cls(row["return%"])],
    ];
    for (const [value, numeric, kind] of cells) {
      const td = document.createElement("td");
      td.textContent = value;
      td.className = [numeric ? "num" : "", kind].filter(Boolean).join(" ");
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  if (!(data.rows || []).length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "num";
    td.textContent = payload.error || "no trades recorded yet";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  const note = [];
  if (s.fee_ratio !== null && s.fee_ratio !== undefined && s.fee_ratio >= 1) {
    note.push("fees ≥ gross PnL — the strategy is paying more than it earns; widen barriers or raise thresholds");
  }
  if (payload.error) note.push(`history: ${payload.error}`);
  el("perf-note").textContent = note.join(" · ");

  renderPerfChart(s);
}

function renderPerfChart(s) {
  const svg = el("perf-chart");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const bars = [["gross", Number(s.gross_pnl) || 0, "#4c8dff"],
                ["fees", -(Number(s.fees) || 0), "#e5a83b"],
                ["net", Number(s.net_pnl) || 0, (Number(s.net_pnl) || 0) >= 0 ? "#2ec27e" : "#e5484d"]];
  const max = Math.max(0.0001, ...bars.map((b) => Math.abs(b[1])));
  const w = 640, h = 140, zero = h / 2, bw = 90, gap = (w - bars.length * bw) / (bars.length + 1);

  const axis = document.createElementNS("http://www.w3.org/2000/svg", "line");
  axis.setAttribute("x1", 0); axis.setAttribute("x2", w);
  axis.setAttribute("y1", zero); axis.setAttribute("y2", zero);
  axis.setAttribute("stroke", "#232c3f");
  svg.appendChild(axis);

  bars.forEach(([name, value, color], i) => {
    const x = gap + i * (bw + gap);
    const height = Math.abs(value) / max * (h / 2 - 16);
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", x);
    rect.setAttribute("width", bw);
    rect.setAttribute("y", value >= 0 ? zero - height : zero);
    rect.setAttribute("height", Math.max(1, height));
    rect.setAttribute("fill", color);
    rect.setAttribute("opacity", "0.85");
    svg.appendChild(rect);

    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", x + bw / 2);
    text.setAttribute("y", value >= 0 ? zero - height - 5 : zero + height + 13);
    text.setAttribute("fill", "#d7dce6");
    text.setAttribute("font-size", "11");
    text.setAttribute("text-anchor", "middle");
    text.textContent = `${name} ${signed(Math.abs(name === "fees" ? -value : value), 2)}`;
    svg.appendChild(text);
  });
}

/* --------------------------------------------------------------- render: config */

function renderConfig(payload) {
  const tbody = el("config-table").querySelector("tbody");
  const strategy = payload.strategy || {};
  el("config-hint").textContent = strategy.file
    ? `${strategy.file} · ${strategy.type} · ${strategy.state}`
    : (payload.error || "no strategy loaded");

  const tunables = payload.tunables || [];
  if (!tunables.length) {
    tbody.innerHTML = "";
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "num";
    td.textContent = payload.error || "load a controller config to edit live fields";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  tbody.innerHTML = "";
  for (const item of tunables) {
    const tr = document.createElement("tr");

    const tdKey = document.createElement("td");
    tdKey.textContent = item.key;
    tr.appendChild(tdKey);

    const tdCur = document.createElement("td");
    tdCur.className = "num";
    tdCur.textContent = item.value === null || item.value === undefined ? "—" : String(item.value);
    tr.appendChild(tdCur);

    const tdInput = document.createElement("td");
    let input;
    const options = item.kind === "bool" ? ["true", "false"]
      : item.kind === "enum" ? (item.choices || []) : null;
    if (options) {
      input = document.createElement("select");
      for (const option of options) {
        const node = document.createElement("option");
        node.value = option;
        node.textContent = option;
        if (String(item.value) === option) node.selected = true;
        input.appendChild(node);
      }
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.value = item.value === null || item.value === undefined ? "" : String(item.value);
    }
    tdInput.appendChild(input);
    tr.appendChild(tdInput);

    const tdBounds = document.createElement("td");
    tdBounds.className = "bounds";
    tdBounds.textContent = item.kind === "bool" ? "bool"
      : item.kind === "enum" ? (item.choices || []).join(" | ")
        : (item.min === null || item.min === undefined ? "—" : `${item.min} … ${item.max}`);
    tr.appendChild(tdBounds);

    const tdBtn = document.createElement("td");
    const btn = document.createElement("button");
    btn.className = "btn apply";
    btn.textContent = "apply";
    btn.addEventListener("click", () => applyConfig(item.key, input.value, btn));
    tdBtn.appendChild(btn);
    tr.appendChild(tdBtn);

    tbody.appendChild(tr);
  }

  const extra = payload.readonly_live_fields || [];
  if (extra.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "bounds";
    td.textContent = `live-updatable but not editable from the UI (no server-side bounds): ${extra.join(", ")}`;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

async function applyConfig(key, value, btn) {
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "…";
  try {
    const res = await api("/api/config", { method: "POST", body: JSON.stringify({ key, value }) });
    if (res.ok) {
      toast(`${key} = ${value} applied`, "ok");
      actionLog(`config ${key}=${value} → ok`, "ok");
      await refresh("config");
    } else {
      toast(`${key} rejected: ${res.detail}`, "bad");
      actionLog(`config ${key}=${value} → ${res.detail}`, "bad");
    }
  } catch (e) {
    toast(`failed: ${e.message}`, "bad");
    actionLog(`config ${key}=${value} → ${e.message}`, "bad");
  }
  btn.textContent = original;
  btn.disabled = false;
}

/* ------------------------------------------------------------------ render: logs */

function renderLogs(payload) {
  const box = el("log-body");
  const data = payload.data || {};
  const lines = data.lines || [];
  box.innerHTML = "";
  if (!lines.length) {
    box.textContent = payload.error || "no log lines yet";
    return;
  }
  const frag = document.createDocumentFragment();
  for (const line of lines) {
    const div = document.createElement("div");
    div.textContent = line;
    if (/ - (ERROR|CRITICAL) - /.test(line)) div.className = "err";
    else if (/ - WARNING - /.test(line)) div.className = "warn";
    frag.appendChild(div);
  }
  box.appendChild(frag);
  box.scrollTop = box.scrollHeight;
}

function renderDoctor(payload) {
  const tbody = el("doctor").querySelector("tbody");
  tbody.innerHTML = "";
  const checks = (payload.data || {}).checks || [];
  for (const check of checks) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    tdName.textContent = check.check;
    const tdStatus = document.createElement("td");
    tdStatus.textContent = check.status;
    tdStatus.className = check.status === "ok" ? "pos" : check.status === "warn" ? "" : "neg";
    const tdDetail = document.createElement("td");
    tdDetail.textContent = check.detail || "";
    tr.append(tdName, tdStatus, tdDetail);
    tbody.appendChild(tr);
  }
  if (!checks.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "num";
    td.textContent = payload.error || "doctor unavailable";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

/* --------------------------------------------------------------------- controls */

async function act(path, body, label, confirmText) {
  if (busy) return;
  if (confirmText && !confirm(confirmText)) return;
  busy = true;
  try {
    const res = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
    const kind = res.ok ? "ok" : "bad";
    toast(`${label}: ${res.detail || (res.ok ? "ok" : "failed")}`, kind);
    actionLog(`${label} → ${res.detail || res.ok}`, kind);
    await Promise.all([refresh("state"), refresh("performance"), refresh("config")]);
  } catch (e) {
    toast(`${label} failed: ${e.message}`, "bad");
    actionLog(`${label} → ${e.message}`, "bad");
  } finally {
    busy = false;
  }
}

/* ------------------------------------------------------------------------ loops */

const renderers = {
  state: (payload) => { renderHeader(payload); renderSignal(payload); renderStatus(payload); },
  performance: renderPerf,
  config: renderConfig,
  logs: renderLogs,
  doctor: renderDoctor,
};

async function refresh(kind) {
  try {
    const payload = await api(`/api/${kind}`);
    renderers[kind](payload);
    if (kind === "state") {
      const broken = !payload.bot_ok && payload.mode !== "demo";
      showAlert(broken
        ? `Cannot read the bot: ${payload.bot_error || "hbot status failed"}. Is the conda env active and \`hbot\` on PATH?`
        : "");
    }
  } catch (e) {
    if (kind === "state") showAlert(`Dashboard request failed: ${e.message}`);
  }
}

function loop(kind) {
  const run = async () => {
    if (kind !== "logs" || el("logs-follow").checked) await refresh(kind);
    setTimeout(run, POLL[kind]);
  };
  run();
}

async function boot() {
  try {
    health = await api("/api/health");
    if (health.token_required && !TOKEN) {
      const entered = prompt("This dashboard is token-protected. Paste the token from the server console:");
      if (entered) { TOKEN = entered.trim(); localStorage.setItem("webui.token", TOKEN); }
    }
    if (!health.hbot_found && health.mode !== "demo") {
      showAlert("`hbot` was not found on PATH — start the dashboard inside the hummingbot conda env, "
        + "or run with --demo to preview the UI.");
    }
  } catch (e) {
    showAlert(`Health check failed: ${e.message}`);
  }
  for (const kind of ["state", "performance", "config", "logs", "doctor"]) await refresh(kind);
  for (const kind of Object.keys(POLL)) loop(kind);

  el("btn-refresh").addEventListener("click", () => {
    for (const kind of Object.keys(POLL)) refresh(kind);
    toast("refreshed");
  });
  el("btn-start").addEventListener("click", () => act("/api/start", {}, "start"));
  el("btn-stop").addEventListener("click", () =>
    act("/api/stop", {}, "stop", "Stop the bot? Open orders will be cancelled."));
  el("btn-kill").addEventListener("click", () =>
    act("/api/kill", {}, "kill switch", "KILL SWITCH: sets manual_kill_switch and stops the bot. Continue?"));
}

boot();
