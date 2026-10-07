/* ConanOps web app. Plain JavaScript, no external code. Everything shown
   that comes from the server (names, log lines, messages) is inserted as
   text, never as HTML. */
"use strict";

// ---------------------------------------------------------------- helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v; // CSSOM, allowed by the page's CSP (style attributes aren't)
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v; // only ever used with constant icon markup
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const ICONS = {
  dashboard: '<path d="M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z"/>',
  players: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c.8-3.2 3-5 5.5-5s4.7 1.8 5.5 5"/><circle cx="17" cy="9" r="2.4"/><path d="M15.8 14.2c2.1.2 3.8 1.8 4.4 4.3"/>',
  mods: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
  console: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M7.5 9.5l3 2.5-3 2.5M12.5 15h4"/>',
  more: '<circle cx="5.5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="18.5" cy="12" r="1.6"/>',
  updates: '<path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/>',
  backups: '<path d="M4 7h16v12H4zM4 7l2-3h12l2 3M10 12h4"/>',
  access: '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/>',
  settings: '<path d="M4 7h10M18 7h2M4 12h4M12 12h8M4 17h12M20 17h0"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="17" r="2"/>',
  diagnostics: '<path d="M3 12h4l2.5-6 4 12 2.5-6H21"/>',
  app: '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M5.5 18.5l1.7-1.7M16.8 7.2l1.7-1.7"/>',
  power: '<path d="M12 3v8M7 6.5a7 7 0 1 0 10 0"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  up: '<path d="M6 15l6-6 6 6"/>', down: '<path d="M6 9l6 6 6-6"/>',
  trash: '<path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/>',
  logout: '<path d="M14 5h5v14h-5M10 8l-4 4 4 4M6 12h10"/>',
};
// replaceChildren()/append() would turn a null into the text "null".
const clean = (kids) => kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false);
function put(el, ...kids) { el.replaceChildren(...clean(kids)); return el; }

function icon(name) {
  return h("span", { "aria-hidden": "true", html:
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>` });
}

const $app = document.getElementById("app");
const state = { overview: null, sid: null, view: "dashboard", timer: null, settingsPage: null, busy: new Set() };

class ApiError extends Error {}
async function api(path, body) {
  const opts = { method: body === undefined ? "GET" : "POST", credentials: "same-origin",
                 headers: { "X-ConanOps": "1" } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { throw new ApiError("Can't reach ConanOps. Is the PC on?"); }
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (r.status === 401 && data.needs_login) { showLogin(); throw new ApiError("Please sign in."); }
  if (r.status === 403 && data.needs_password) { showNoPassword(); throw new ApiError(data.error); }
  if (!r.ok) throw new ApiError(data.message || data.error || `Error ${r.status}`);
  return data;
}

function toast(text, kind) {
  if (!text) return;
  const t = h("div", { class: "toast " + (kind || "") }, text);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 7000 : 4000);
}

function confirmBox(title, text, okLabel, danger) {
  return new Promise((resolve) => {
    const close = (v) => { back.remove(); resolve(v); };
    const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(false); } },
      h("div", { class: "modal", role: "dialog", "aria-modal": "true" },
        h("h2", {}, title), h("p", {}, text),
        h("div", { class: "row end" },
          h("button", { class: "btn", onclick: () => close(false) }, "Cancel"),
          h("button", { class: "btn " + (danger ? "danger" : "primary"), onclick: () => close(true) }, okLabel))));
    document.body.append(back);
    back.querySelector(".btn:last-child").focus();
  });
}

function promptBox(title, text, placeholder, okLabel) {
  return new Promise((resolve) => {
    const input = h("input", { type: "text", placeholder, inputmode: "numeric" });
    const close = (v) => { back.remove(); resolve(v); };
    const back = h("div", { class: "modal-back" },
      h("form", { class: "modal", onsubmit: (e) => { e.preventDefault(); close(input.value.trim()); } },
        h("h2", {}, title), h("p", {}, text), input,
        h("div", { class: "row end" },
          h("button", { type: "button", class: "btn", onclick: () => close(null) }, "Cancel"),
          h("button", { type: "submit", class: "btn primary" }, okLabel))));
    document.body.append(back);
    input.focus();
  });
}

// Runs an action, disabling its button meanwhile; shows the result.
async function act(btn, fn, opts = {}) {
  if (btn) { btn.disabled = true; btn.dataset.label = btn.textContent; put(btn, h("span", { class: "spinner" })); }
  try {
    const res = await fn();
    if (res && res.message) toast(res.message, res.ok === false ? "bad" : "good");
    if (opts.after) await opts.after(res);
    return res;
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = btn.dataset.label; }
  }
}

function fmtDuration(s) {
  if (s === null || s === undefined) return "—";
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${hr}h`;
  if (hr) return `${hr}h ${m}m`;
  return `${m}m`;
}
function fmtWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function applyTheme(t) {
  if (!t) return;
  const r = document.documentElement.style;
  const map = { bg: "--bg", panel: "--panel", panel_alt: "--panel-alt", border: "--border", ivory: "--ivory",
                muted: "--muted", dim: "--dim", accent: "--accent", green: "--green", red: "--red", yellow: "--yellow" };
  for (const [k, v] of Object.entries(map)) if (t[k]) r.setProperty(v, t[k]);
}

// ------------------------------------------------------------------ login
function showLogin(message) {
  stopTimer();
  const err = h("div", { class: "error" }, message || "");
  const pw = h("input", { type: "password", autocomplete: "current-password", placeholder: "Password", required: true });
  const remember = h("input", { type: "checkbox", checked: true });
  const btn = h("button", { class: "btn primary", type: "submit" }, "Sign In");
  const form = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      btn.disabled = true;
      try {
        await api("/api/login", { password: pw.value, remember: remember.checked });
        start();
      } catch (ex) { err.textContent = ex.message; pw.select(); }
      finally { btn.disabled = false; }
    } },
    pw, h("label", { class: "check muted" }, remember, "Keep me signed in on this device"), btn, err);
  put($app, h("div", { class: "login" }, h("div", { class: "card" },
    h("img", { class: "logo", src: "/icon.svg", alt: "" }), h("h1", {}, "ConanOps"),
    h("p", { class: "muted" }, "Sign in to manage your servers."), form)));
  pw.focus();
}

function showNoPassword() {
  stopTimer();
  put($app, h("div", { class: "login" }, h("div", { class: "card" },
    h("img", { class: "logo", src: "/icon.svg", alt: "" }), h("h1", {}, "Almost there"),
    h("p", { class: "muted" }, "On the PC running ConanOps, open App Settings → Web Version and set a password. " +
      "Then reload this page."),
    h("button", { class: "btn primary", onclick: () => location.reload() }, "Reload"))));
}

// ------------------------------------------------------------------- shell
const MAIN_NAV = [["dashboard", "Dashboard"], ["players", "Players"], ["mods", "Mods"], ["console", "Console"]];
const OTHER_NAV = [["updates", "Updates"], ["backups", "Backups"], ["access", "Access"], ["settings", "Server Settings"],
                   ["diagnostics", "Diagnostics"], ["app", "ConanOps"]];
const TITLES = Object.fromEntries([...MAIN_NAV, ...OTHER_NAV, ["more", "More"]]);

function server() { return state.overview && state.overview.servers.find((s) => s.id === state.sid); }

function go(view) {
  if (location.hash !== "#/" + view) location.hash = "#/" + view;
  else render();
}
window.addEventListener("hashchange", () => render());

async function selectServer(id) {
  state.sid = id;
  localStorage.setItem("conanops.sid", id);
  try { await api(`/api/servers/${encodeURIComponent(id)}/select`, {}); } catch (e) { /* the view will report */ }
  await refreshOverview();
  render();
}

function sidebar() {
  const ov = state.overview;
  return h("nav", { class: "sidebar", "aria-label": "Main" },
    h("div", { class: "brand" }, h("img", { src: "/icon.svg", alt: "" }),
      h("div", {}, h("b", {}, "ConanOps"), h("span", {}, "v" + (ov ? ov.version : "")))),
    h("div", { class: "side-label" }, "Servers"),
    (ov ? ov.servers : []).map((s) => h("button", {
      class: "nav-btn server-pill" + (s.running ? " on" : "") + (s.id === state.sid ? " active" : ""),
      onclick: () => selectServer(s.id) },
      h("span", { class: "dot" }), h("span", { class: "name" }, s.name),
      s.running ? h("span", { class: "count" }, `${s.player_count}`) : null)),
    h("div", { class: "side-label" }, "Manage"),
    [...MAIN_NAV, ...OTHER_NAV.slice(0, 5)].map(([k, label]) => h("button", {
      class: "nav-btn" + (state.view === k ? " active" : ""), onclick: () => go(k) }, icon(k), label)),
    h("div", { class: "side-label" }, "System"),
    h("button", { class: "nav-btn" + (state.view === "app" ? " active" : ""), onclick: () => go("app") }, icon("app"), "ConanOps"),
    h("div", { class: "spacer" }),
    h("button", { class: "nav-btn", onclick: logout }, icon("logout"), "Sign out"));
}

function tabbar() {
  const tabs = [...MAIN_NAV, ["more", "More"]];
  const inMore = !MAIN_NAV.some(([k]) => k === state.view);
  return h("nav", { class: "tabbar", "aria-label": "Main" }, tabs.map(([k, label]) => h("button", {
    class: "tab" + ((state.view === k || (k === "more" && inMore)) ? " active" : ""), onclick: () => go(k) },
    icon(k), label)));
}

function topbar(extra) {
  const ov = state.overview;
  const s = server();
  const select = ov && ov.servers.length > 1 ? h("select", { class: "server-select", "aria-label": "Server",
      onchange: (e) => selectServer(e.target.value) },
    ov.servers.map((x) => h("option", { value: x.id, selected: x.id === state.sid }, x.name))) : null;
  const perServer = !["app", "more"].includes(state.view);
  return h("header", { class: "topbar" },
    h("div", { class: "title" }, h("h1", {}, TITLES[state.view] || ""),
      perServer && s ? h("div", { class: "dim" }, s.name) : null),
    select, extra || null);
}

function layout(content, extra) {
  return h("div", { class: "shell" }, sidebar(),
    h("main", { class: "main" }, topbar(extra), content), tabbar());
}

async function refreshOverview() {
  const ov = await api("/api/overview");
  state.overview = ov;
  applyTheme(ov.theme);
  if (!ov.servers.some((s) => s.id === state.sid)) {
    const saved = localStorage.getItem("conanops.sid");
    state.sid = ov.servers.some((s) => s.id === saved) ? saved : (ov.active_id || (ov.servers[0] || {}).id || null);
  }
  return ov;
}

function stopTimer() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }

async function render() {
  stopTimer();
  const view = (location.hash.replace(/^#\/?/, "") || "dashboard").split("/")[0];
  state.view = VIEWS[view] ? view : "dashboard";
  if (!state.overview) return;
  if (!server() && !["app", "more"].includes(state.view)) {
    put($app, layout(h("div", { class: "card empty" },
      "No servers yet. Add one in the ConanOps app on your PC (the setup needs the PC).")));
    return;
  }
  const holder = h("div", { class: "stack" }, h("div", { class: "card empty" }, "Loading…"));
  put($app, layout(holder));
  try {
    await VIEWS[state.view](holder);
  } catch (e) {
    if (e instanceof ApiError && /sign in|password/i.test(e.message)) return;
    put(holder, h("div", { class: "notice bad" }, h("div", { class: "text" }, e.message)));
  }
}

async function logout() {
  try { await api("/api/logout", {}); } catch (e) { /* ignore */ }
  showLogin("Signed out.");
}

const sid = () => encodeURIComponent(state.sid);
const sp = (p) => `/api/servers/${sid()}${p}`;

// --------------------------------------------------------------- dashboard
async function viewDashboard(root) {
  const paint = async () => {
    const d = await api(sp(""));
    const s = d.server;
    const st = d.stats || {};
    const status = s.busy ? h("span", { class: "pill warn" }, s.busy)
      : s.running ? h("span", { class: "pill on" }, "Online") : h("span", { class: "pill" }, "Offline");
    const controls = s.installed ? h("div", { class: "row" },
      s.running ? null : h("button", { class: "btn primary", onclick: (e) => act(e.currentTarget,
        () => api(sp("/start"), {}), { after: paintSoon }) }, icon("power"), "Start"),
      s.running ? h("button", { class: "btn", onclick: async (e) => {
        const b = e.currentTarget;
        if (await confirmBox("Restart the server?", `Players online: ${s.player_count}. They get a warning first, ` +
          "and the world is saved.", "Restart")) act(b, () => api(sp("/restart"), {}), { after: paintSoon });
      } }, "Restart") : null,
      s.running ? h("button", { class: "btn danger", onclick: async (e) => {
        const b = e.currentTarget;
        if (await confirmBox("Stop the server?", "The world is saved first. It stays stopped (no automatic " +
          "restarts) until you start it again.", "Stop", true)) act(b, () => api(sp("/stop"), {}), { after: paintSoon });
      } }, "Stop") : null)
      : h("div", { class: "dim" }, "Finish this server's setup in the app on the PC.");

    const notices = [];
    if (s.hold) notices.push(h("div", { class: "notice" }, h("div", { class: "text" }, "Held stopped. " + s.hold),
      h("button", { class: "btn small", onclick: (e) => act(e.currentTarget, () => api(sp("/start"), {}),
        { after: paintSoon }) }, "Start Anyway")));
    if (!d.rcon_enabled && s.installed) notices.push(h("div", { class: "notice" }, h("div", { class: "text" },
      "RCON is off, so ConanOps can't save the world before stopping, and kick/ban/console won't work."),
      h("button", { class: "btn small", onclick: (e) => act(e.currentTarget, () => api(sp("/enable-rcon"), {}),
        { after: paintSoon }) }, "Turn On RCON")));

    const log = h("div", { class: "log mono", role: "log" }, d.log.length ? d.log.join("\n") : "No log lines yet.");
    const old = root.querySelector(".log");
    const stick = !old || old.scrollTop + old.clientHeight >= old.scrollHeight - 30;
    const prevTop = old ? old.scrollTop : 0;

    put(root, 
      ...notices,
      h("div", { class: "card" }, h("div", { class: "row" }, status,
        h("span", { class: "mono muted" }, s.address),
        h("button", { class: "btn small icon", title: "Copy address", "aria-label": "Copy address",
          onclick: () => navigator.clipboard && navigator.clipboard.writeText(s.address).then(() => toast("Copied.")) },
          icon("copy")),
        h("div", { class: "spacer" }), controls)),
      d.waiting_for.length ? h("div", { class: "notice" }, h("div", { class: "text" },
        `Waiting for a fix to: ${d.waiting_for.join(", ")}. The server starts again by itself once it's updated.`)) : null,
      h("div", { class: "grid stats" },
        statCard("CPU", st.cpu !== undefined && st.cpu !== null ? `${st.cpu}%` : "—", "of the whole PC"),
        statCard("Memory", st.mem_mb ? `${(st.mem_mb / 1024).toFixed(1)} GB` : "—", st.uptime_seconds ? `up ${fmtDuration(st.uptime_seconds)}` : ""),
        statCard("Server FPS", d.fps ? Number(d.fps).toFixed(1) : "—", ""),
        statCard("Players", String(s.player_count), s.players.slice(0, 3).join(", ") + (s.players.length > 3 ? "…" : ""))),
      h("div", { class: "section-title" }, h("h2", {}, "Automation")),
      h("div", { class: "grid tiles" }, d.automation.map((t) => h("div", { class: "card tile" },
        h("div", { class: "mark" }, t.name.slice(0, 2).toUpperCase()),
        h("div", { class: "text" }, h("div", {}, t.name), h("div", { class: "state" + (t.on ? " on" : "") }, t.detail)),
        h("button", { class: "btn small", onclick: () => go(t.key === "updates" ? "updates" : t.key === "ddns" ? "app" : "settings") },
          "Change")))),
      h("div", { class: "section-title" }, h("h2", {}, "Server log")),
      log);
    log.scrollTop = stick ? log.scrollHeight : prevTop;
  };
  const paintSoon = () => setTimeout(() => paint().catch(() => {}), 1500);
  await paint();
  state.timer = setInterval(() => paint().catch(() => {}), 3000);
}
function statCard(label, value, sub) {
  return h("div", { class: "card stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    h("div", { class: "sub" }, sub || " "));
}

// ----------------------------------------------------------------- players
async function viewPlayers(root) {
  const paint = async () => {
    const d = await api(sp("/players"));
    const max = Math.max(1, ...d.activity);
    put(root, 
      h("div", { class: "card" }, h("h3", {}, "When people play"),
        h("div", { class: "bars", "aria-label": "Sessions started per hour of the day" },
          d.activity.map((n, i) => h("span", { style: `height:${Math.round((n / max) * 100)}%`, title: `${i}:00 — ${n}` }))),
        h("div", { class: "bars-axis" }, h("span", {}, "12 AM"), h("span", {}, "6 AM"), h("span", {}, "12 PM"), h("span", {}, "6 PM"))),
      h("div", { class: "card" }, d.players.length ? h("div", { class: "list" }, d.players.map((p) => h("div", { class: "item" },
        h("div", { class: "grow" }, h("div", { class: "name" }, p.name),
          h("div", { class: "dim" }, `${fmtDuration(p.playtime_seconds)} played · ${p.sessions} session${p.sessions === 1 ? "" : "s"}` +
            (p.online ? "" : p.last_seen ? ` · last seen ${fmtWhen(p.last_seen)}` : ""))),
        p.online ? h("span", { class: "pill on" }, "Online") : null,
        p.online ? h("button", { class: "btn small", onclick: async (e) => {
          const b = e.currentTarget;
          if (await confirmBox(`Kick ${p.name}?`, "They can join again right away.", "Kick", true))
            act(b, () => api(sp("/players/kick"), { name: p.name }), { after: paint });
        } }, "Kick") : null,
        h("button", { class: "btn small danger", onclick: async (e) => {
          const b = e.currentTarget;
          const id = await promptBox(`Ban ${p.name}`, "Enter their Steam ID (the 17-digit number from their Steam profile).",
            "7656119…", "Ban");
          if (id) act(b, () => api(sp("/players/ban"), { name: p.name, steam_id: id }), { after: paint });
        } }, "Ban")))) : h("div", { class: "empty" }, "Nobody has played yet.")));
  };
  await paint();
  state.timer = setInterval(() => paint().catch(() => {}), 10000);
}

// -------------------------------------------------------------------- mods
async function viewMods(root) {
  const paint = async () => {
    const d = await api(sp("/mods"));
    const statusPill = (m) => m.broken ? h("span", { class: "pill bad" }, "Broken")
      : !m.downloaded ? h("span", { class: "pill warn" }, "Not downloaded")
      : m.status === "updated" ? h("span", { class: "pill on" }, "Up to date")
      : m.status === "stale" ? h("span", { class: "pill warn" }, "Not updated yet")
      : m.status === "legacy" ? h("span", { class: "pill warn" }, "Old game version") : null;
    const idInput = h("input", { type: "text", placeholder: "Workshop ID or link" });
    const results = h("div", { class: "list" });
    const searchInput = h("input", { type: "search", placeholder: "Search the Workshop" });
    put(root, 
      d.busy ? h("div", { class: "notice" }, h("div", { class: "text" }, d.busy + "…")) : null,
      h("div", { class: "card" }, h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: (e) => act(e.currentTarget, () => api(sp("/mods/download"), {}), { after: paint }) }, "Download Mods"),
        h("button", { class: "btn", onclick: async (e) => {
          const b = e.currentTarget;
          if (await confirmBox("Find the broken mod?", "ConanOps stops the server and tests the mods, restarting it " +
            "many times. Your world is copied first and put back exactly as it was. Can take a while.", "Start Check"))
            act(b, () => api(sp("/mods/find-broken"), {}), { after: paint });
        } }, "Find Broken Mod")),
        h("div", { class: "dim", style: "margin-top:8px" }, "Changes load the next time the server starts. Order matters: top loads first.")),
      h("div", { class: "card" }, d.mods.length ? h("div", { class: "list" }, d.mods.map((m, i) => h("div", { class: "item" },
        h("input", { type: "checkbox", class: "switch", checked: m.enabled, "aria-label": `Use ${m.name}`,
          onchange: (e) => act(null, () => api(sp("/mods/toggle"), { id: m.id, enabled: e.target.checked }), { after: paint }) }),
        h("div", { class: "grow" }, h("div", { class: "name" }, m.name),
          h("div", { class: "row" }, h("span", { class: "dim mono" }, m.id), statusPill(m),
            m.updated ? h("span", { class: "dim" }, "updated " + m.updated) : null)),
        h("button", { class: "btn small icon", "aria-label": "Move up", disabled: i === 0,
          onclick: () => act(null, () => api(sp("/mods/move"), { id: m.id, direction: -1 }), { after: paint }) }, icon("up")),
        h("button", { class: "btn small icon", "aria-label": "Move down", disabled: i === d.mods.length - 1,
          onclick: () => act(null, () => api(sp("/mods/move"), { id: m.id, direction: 1 }), { after: paint }) }, icon("down")),
        h("button", { class: "btn small icon danger", "aria-label": "Remove", onclick: async (e) => {
          const b = e.currentTarget;
          if (await confirmBox(`Remove ${m.name}?`, "The next time the server starts, anything this mod added to the " +
            "world (buildings, items) is deleted.", "Remove", true))
            act(b, () => api(sp("/mods/remove"), { id: m.id }), { after: paint });
        } }, icon("trash"))))) : h("div", { class: "empty" }, "No mods on this server.")),
      h("div", { class: "card" }, h("h3", {}, "Add a mod"),
        h("form", { class: "row", style: "margin-top:10px", onsubmit: (e) => { e.preventDefault();
            act(e.submitter, () => api(sp("/mods/add"), { id: idInput.value }), { after: paint }); } },
          h("div", { class: "grow", style: "flex:1;min-width:180px" }, idInput), h("button", { class: "btn primary", type: "submit" }, "Add")),
        d.search_available ? h("form", { class: "row", style: "margin-top:10px", onsubmit: async (e) => {
            e.preventDefault();
            const r = await act(e.submitter, () => api(sp("/mods/search?q=" + encodeURIComponent(searchInput.value))));
            if (!r) return;
            put(results, ...(r.results.length ? r.results.map((x) => h("div", { class: "item" },
              x.preview ? h("img", { src: x.preview, alt: "", width: 48, height: 48, style: "border-radius:8px;object-fit:cover" }) : null,
              h("div", { class: "grow" }, h("div", { class: "name" }, x.title), h("div", { class: "dim" }, `${x.subscriptions.toLocaleString()} subscribers`)),
              h("button", { class: "btn small", onclick: (ev) => act(ev.currentTarget, () => api(sp("/mods/add"), { id: x.id, name: x.title }), { after: paint }) }, "Add")))
              : [h("div", { class: "empty" }, "Nothing found.")]));
          } }, h("div", { style: "flex:1;min-width:180px" }, searchInput), h("button", { class: "btn", type: "submit" }, "Search")) : null,
        results));
  };
  await paint();
}

// ----------------------------------------------------------------- console
async function viewConsole(root) {
  const out = h("div", { class: "log tall mono", role: "log" });
  const input = h("input", { type: "text", placeholder: "RCON command, e.g. listplayers", autocomplete: "off", autocapitalize: "off" });
  const say = h("input", { type: "text", placeholder: "Message to everyone online" });
  const history = JSON.parse(sessionStorage.getItem("conanops.console") || "[]");
  const write = () => { out.textContent = history.join("\n\n") || "Send a command to see the server's reply."; out.scrollTop = out.scrollHeight; };
  const send = async (cmd, btn) => {
    if (!cmd) return;
    const r = await act(btn, () => api(sp("/console"), { command: cmd }));
    if (!r) return;
    history.push("> " + cmd, r.output);
    while (history.length > 80) history.shift();
    sessionStorage.setItem("conanops.console", JSON.stringify(history));
    write();
  };
  put(root, 
    out,
    h("div", { class: "chips" }, ["listplayers", "saveworld"].map((c) => h("button", { class: "chip", onclick: () => send(c) }, c))),
    h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); const c = input.value.trim(); input.value = ""; send(c, e.submitter); } },
      h("div", { style: "flex:1;min-width:200px" }, input), h("button", { class: "btn primary", type: "submit" }, "Send")),
    h("form", { class: "row", onsubmit: (e) => { e.preventDefault();
        act(e.submitter, () => api(sp("/broadcast"), { message: say.value })).then(() => { say.value = ""; }); } },
      h("div", { style: "flex:1;min-width:200px" }, say), h("button", { class: "btn", type: "submit" }, "Broadcast")));
  write();
}

// ----------------------------------------------------------------- updates
async function viewUpdates(root) {
  const paint = async () => {
    const d = await api(sp("/updates"));
    put(root, 
      d.busy ? h("div", { class: "notice" }, h("div", { class: "text" }, d.busy + "…")) : null,
      h("div", { class: "card" }, h("div", { class: "row" },
        h("div", { class: "grow", style: "flex:1" }, h("div", { class: "dim" }, "Installed build"), h("h2", {}, d.installed)),
        d.state ? h("span", { class: "pill " + (/up to date/i.test(d.state) ? "on" : "warn") }, d.state) : null),
        h("div", { class: "row", style: "margin-top:12px" },
          h("button", { class: "btn", disabled: d.checking, onclick: (e) => act(e.currentTarget, () => api(sp("/updates/check"), {}),
            { after: () => setTimeout(() => paint().catch(() => {}), 4000) }) }, d.checking ? "Checking…" : "Check Now"),
          d.pending ? h("button", { class: "btn primary", onclick: async (e) => {
            const b = e.currentTarget;
            if (await confirmBox("Update the server now?", "It's backed up and stopped first, then started again " +
              "if it was running. Players are disconnected.", "Update Now")) act(b, () => api(sp("/updates/install"), {}), { after: paint });
          } }, "Update Now") : null),
        d.last_check ? h("div", { class: "dim", style: "margin-top:8px" }, "Last automatic check: " + fmtWhen(d.last_check)) : null),
      d.pending ? h("div", { class: "card" }, h("h3", {}, d.pending), h("div", { class: "log mono", style: "height:240px;margin-top:10px" }, d.changelog)) : null,
      h("div", { class: "card" }, h("h3", {}, "Automatic updates"),
        h("div", { class: "field" }, h("div", { class: "top" }, h("label", { for: "au" }, "Update automatically (only when nobody's online)"),
          h("input", { id: "au", type: "checkbox", class: "switch", checked: d.auto_update,
            onchange: (e) => act(null, () => api(sp("/updates/settings"), { auto_update: e.target.checked })) }))),
        h("div", { class: "field" }, h("div", { class: "top" }, h("label", { for: "ai" }, "Check every (hours)"),
          h("input", { id: "ai", type: "number", min: 1, max: 168, value: d.interval_hours, style: "width:100px",
            onchange: (e) => act(null, () => api(sp("/updates/settings"), { interval_hours: Number(e.target.value) })) })))));
  };
  await paint();
  state.timer = setInterval(() => paint().catch(() => {}), 8000);
}

// ----------------------------------------------------------------- backups
async function viewBackups(root) {
  const paint = async () => {
    const d = await api(sp("/backups"));
    put(root, 
      d.busy ? h("div", { class: "notice" }, h("div", { class: "text" }, d.busy + "…")) : null,
      h("div", { class: "card" }, h("div", { class: "row" },
        h("div", { style: "flex:1;min-width:0" }, h("div", { class: "dim" }, "Saved to"), h("div", { class: "mono", style: "word-break:break-all" }, d.destination || "Not set")),
        h("button", { class: "btn primary", onclick: (e) => act(e.currentTarget, () => api(sp("/backups/create"), {}), { after: paint }) }, "Back Up Now"))),
      h("div", { class: "card" }, d.backups.length ? h("div", { class: "list" }, d.backups.map((b) => h("div", { class: "item" },
        h("div", { class: "grow" }, h("div", { class: "name" }, fmtWhen(b.when)),
          h("div", { class: "dim" }, `${b.trigger} · ${b.size_mb} MB`)),
        h("button", { class: "btn small", onclick: async (e) => {
          const btn = e.currentTarget;
          if (await confirmBox("Restore this backup?", `${fmtWhen(b.when)}\n\nThe server stops, the world goes back to ` +
            "this point (anything since is lost -- a safety copy of the current world is kept), then it starts again " +
            "if it was running.", "Restore", true)) act(btn, () => api(sp("/backups/restore"), { name: b.name }), { after: paint });
        } }, "Restore")))) : h("div", { class: "empty" }, "No backups yet.")));
  };
  await paint();
}

// ------------------------------------------------------------------ access
async function viewAccess(root) {
  const paint = async () => {
    const d = await api(sp("/access"));
    const wlInput = h("input", { type: "text", placeholder: "Steam ID", inputmode: "numeric" });
    const banInput = h("input", { type: "text", placeholder: "Steam ID", inputmode: "numeric" });
    const listOf = (ids, removeLabel, onRemove) => ids.length ? h("div", { class: "list" }, ids.map((id) => h("div", { class: "item" },
      h("div", { class: "grow mono" }, id), h("button", { class: "btn small", onclick: (e) => act(e.currentTarget, () => onRemove(id), { after: paint }) }, removeLabel))))
      : h("div", { class: "empty" }, "Nobody here.");
    put(root, 
      d.rcon_enabled ? null : h("div", { class: "notice" }, h("div", { class: "text" }, "RCON is off, so bans take effect at the next restart.")),
      h("div", { class: "card" }, h("div", { class: "field" }, h("div", { class: "top" }, h("label", { for: "wl" }, "Only let whitelisted players join"),
        h("input", { id: "wl", type: "checkbox", class: "switch", checked: d.whitelist_enabled,
          onchange: (e) => act(null, () => api(sp("/access/whitelist-mode"), { on: e.target.checked }), { after: paint }) })))),
      h("div", { class: "card" }, h("h3", {}, "Whitelist"),
        h("form", { class: "row", style: "margin:10px 0", onsubmit: (e) => { e.preventDefault();
            act(e.submitter, () => api(sp("/access/whitelist"), { steam_id: wlInput.value }), { after: paint }); } },
          h("div", { style: "flex:1;min-width:180px" }, wlInput), h("button", { class: "btn", type: "submit" }, "Add")),
        listOf(d.whitelist, "Remove", (id) => api(sp("/access/whitelist"), { steam_id: id, remove: true }))),
      h("div", { class: "card" }, h("h3", {}, "Banned"),
        h("form", { class: "row", style: "margin:10px 0", onsubmit: (e) => { e.preventDefault();
            act(e.submitter, () => api(sp("/access/ban"), { steam_id: banInput.value }), { after: paint }); } },
          h("div", { style: "flex:1;min-width:180px" }, banInput), h("button", { class: "btn danger", type: "submit" }, "Ban")),
        listOf(d.banned, "Unban", (id) => api(sp("/access/unban"), { steam_id: id }))));
  };
  await paint();
}

// ---------------------------------------------------------------- settings
function fieldInput(f, onChange) {
  const id = "f_" + f.key;
  let input;
  const val = () => {
    if (f.type === "bool") return input.checked;
    if (f.type === "int") return Math.round(Number(input.value));
    if (f.type === "float") return Number(input.value);
    if (f.type === "choice") { const o = f.choices[input.selectedIndex]; return o ? o.value : null; }
    return input.value;
  };
  if (f.type === "bool") {
    input = h("input", { id, type: "checkbox", class: "switch", checked: !!f.value, disabled: !f.enabled });
  } else if (f.type === "choice") {
    input = h("select", { id, disabled: !f.enabled }, f.choices.map((c) => h("option", { selected: JSON.stringify(c.value) === JSON.stringify(f.value) }, c.label)));
  } else if (f.type === "float" && f.max - f.min <= 1000) {
    const out = h("output", {}, Number(f.value).toFixed(f.decimals));
    input = h("input", { id, type: "range", min: f.min, max: f.max, step: f.step || 0.1, value: f.value, disabled: !f.enabled,
      oninput: () => { out.textContent = Number(input.value).toFixed(f.decimals); } });
    input.addEventListener("input", () => onChange(f.key, val()));
    return { el: h("div", { class: "range-row" }, input, out), val };
  } else if (f.type === "int" || f.type === "float") {
    input = h("input", { id, type: "number", min: f.min, max: f.max, step: f.step || (f.type === "int" ? 1 : 0.1),
      value: f.value, disabled: !f.enabled, style: "max-width:160px" });
  } else if (f.type === "time") {
    input = h("input", { id, type: "time", value: f.value || "", disabled: !f.enabled, style: "max-width:160px" });
  } else {
    input = h("input", { id, type: f.secret ? "password" : "text", value: f.value ?? "", placeholder: f.placeholder || "",
      disabled: !f.enabled, autocomplete: "off" });
  }
  input.addEventListener(f.type === "bool" || f.type === "choice" ? "change" : "input", () => onChange(f.key, val()));
  return { el: input, val };
}

async function viewSettings(root) {
  const d = await api(sp("/settings"));
  if (!d.pages.length) { put(root, h("div", { class: "card empty" }, "No settings.")); return; }
  if (!d.pages.some((p) => p.key === state.settingsPage)) state.settingsPage = d.pages[0].key;
  const page = d.pages.find((p) => p.key === state.settingsPage);
  const changes = {};
  const saveBar = h("div", { class: "row end", style: "position:sticky;bottom:calc(var(--nav-h) + 8px);margin-top:12px" });
  const updateBar = () => {
    const n = Object.keys(changes).length;
    put(saveBar, ...(n ? [
      h("span", { class: "dim" }, `${n} change${n === 1 ? "" : "s"}`),
      h("button", { class: "btn", onclick: () => render() }, "Discard"),
      h("button", { class: "btn primary", onclick: (e) => act(e.currentTarget,
        () => api(sp("/settings/" + encodeURIComponent(page.key)), { values: changes }), { after: (r) => { if (r && r.ok !== false) render(); } }) },
        page.apply_label || "Save")] : []));
  };
  const original = Object.fromEntries(page.fields.map((f) => [f.key, f.value]));
  const rows = page.fields.map((f) => {
    const row = h("div", { class: "field" });
    const { el } = fieldInput(f, (key, value) => {
      if (JSON.stringify(value) === JSON.stringify(original[key])) delete changes[key]; else changes[key] = value;
      row.classList.toggle("dirty", key in changes);
      updateBar();
    });
    row.append(...clean([f.type === "bool" ? h("div", { class: "top" }, h("label", { for: "f_" + f.key }, f.label), el)
      : h("label", { for: "f_" + f.key }, f.label), f.type === "bool" ? null : el,
      f.help ? h("div", { class: "help" }, f.help) : null]));
    return row;
  });
  put(root, 
    h("div", { class: "chips", role: "tablist" }, d.pages.map((p) => h("button", { class: "chip" + (p.key === page.key ? " active" : ""),
      role: "tab", "aria-selected": p.key === page.key ? "true" : "false",
      onclick: () => { state.settingsPage = p.key; render(); } }, p.title))),
    page.note ? h("div", { class: "dim" }, page.note) : null,
    h("div", { class: "card" }, rows),
    saveBar);
}

// ------------------------------------------------------------- diagnostics
async function viewDiagnostics(root) {
  const results = h("div", { class: "stack" });
  const run = async (btn) => {
    put(results, h("div", { class: "card empty" }, "Checking… this takes a few seconds."));
    const r = await act(btn, () => api(sp("/diagnostics"), {}));
    if (!r) { put(results); return; }
    const order = { error: 0, warning: 1, ok: 2 };
    r.results.sort((a, b) => order[a.status] - order[b.status]);
    put(results, ...r.results.map((x) => h("div", { class: "card" },
      h("div", { class: "row" }, h("span", { class: "pill " + (x.status === "ok" ? "on" : x.status === "warning" ? "warn" : "bad") },
        x.status === "ok" ? "OK" : x.status === "warning" ? "Check" : "Problem"), h("h3", {}, x.title)),
      h("p", { class: "muted", style: "margin:8px 0 0" }, x.message),
      x.detail ? h("details", { class: "dim", style: "margin-top:6px" }, h("summary", {}, "Details"), x.detail) : null)));
  };
  put(root, 
    h("div", { class: "card" }, h("p", { class: "muted", style: "margin:0 0 12px" },
      "Checks the install, ports, firewall, router forwarding and more, and explains anything that would stop friends connecting."),
      h("div", { class: "row" }, h("button", { class: "btn primary", onclick: (e) => run(e.currentTarget) }, "Run Diagnostics"),
        h("button", { class: "btn", onclick: (e) => act(e.currentTarget, () => api(sp("/repair-network"), {})) }, "Repair Networking"))),
    results);
}

// ---------------------------------------------------------------- ConanOps
async function viewApp(root) {
  const paint = async () => {
    const d = await api("/api/app");
    const toggle = (key, label, help) => h("div", { class: "field" },
      h("div", { class: "top" }, h("label", { for: "a_" + key }, label),
        h("input", { id: "a_" + key, type: "checkbox", class: "switch", checked: !!d[key],
          onchange: (e) => act(null, () => api("/api/app/" + key, { value: e.target.checked }), { after: () => setTimeout(() => paint().catch(() => {}), 1500) }) })),
      help ? h("div", { class: "help" }, help) : null);
    put(root, 
      h("div", { class: "card" }, h("h3", {}, "Keep servers running"),
        toggle("start_with_windows", "Start ConanOps when I sign into Windows"),
        toggle("keep_alive", "Reopen ConanOps if it closes or crashes"),
        toggle("keep_awake", "Keep the PC awake while a server runs"),
        toggle("update_restarts", `Only restart for Windows updates ${d.update_window[0]}–${d.update_window[1]}`,
          "Windows asks for permission on the PC to change this."),
        d.sign_in_note ? h("div", { class: "notice", style: "margin-top:10px" }, h("div", { class: "text" }, d.sign_in_note)) : null,
        h("div", { class: "field" }, h("label", { for: "mrm" }, "If a mod stops a server from starting"),
          h("select", { id: "mrm", onchange: (e) => act(null, () => api("/api/app/mod_recovery_mode", { value: e.target.value })) },
            [["wait", "Find it, keep the server stopped and wait for a fix"], ["start_without", "Find it and start without it (its items are removed)"],
             ["alert", "Just tell me"]].map(([v, l]) => h("option", { value: v, selected: d.mod_recovery_mode === v }, l))))),
      h("div", { class: "card" }, h("h3", {}, `ConanOps ${d.version}`),
        d.app_update_status ? h("p", { class: "muted", style: "margin:8px 0" }, d.app_update_status) : null,
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: (e) => act(e.currentTarget, () => api("/api/app/check_app_update", {}),
            { after: () => setTimeout(() => paint().catch(() => {}), 4000) }) }, "Check for Updates"),
          d.app_update_available ? h("button", { class: "btn primary", onclick: async (e) => {
            const b = e.currentTarget;
            if (await confirmBox("Update ConanOps?", "ConanOps restarts itself on the PC to finish; your servers keep " +
              "running. This page reconnects in a minute.", "Update")) act(b, () => api("/api/app/install_app_update", {}));
          } }, "Install Update") : null),
        toggle("auto_check_app_updates", "Check for updates automatically"),
        toggle("auto_install_app_updates", "Install updates automatically")),
      h("div", { class: "card" }, h("button", { class: "btn", onclick: logout }, icon("logout"), "Sign out of this device")));
  };
  await paint();
}

async function viewMore(root) {
  put(root, h("div", { class: "more-grid" }, OTHER_NAV.map(([k, label]) =>
    h("button", { class: "btn", onclick: () => go(k) }, icon(k), label))),
    h("button", { class: "btn", style: "margin-top:12px;width:100%", onclick: logout }, icon("logout"), "Sign out"));
}

const VIEWS = { dashboard: viewDashboard, players: viewPlayers, mods: viewMods, console: viewConsole,
  updates: viewUpdates, backups: viewBackups, access: viewAccess, settings: viewSettings,
  diagnostics: viewDiagnostics, app: viewApp, more: viewMore };

// -------------------------------------------------------------------- boot
async function start() {
  try {
    const s = await api("/api/session");
    if (!s.password_set) return showNoPassword();
    if (!s.authed) return showLogin();
    await refreshOverview();
    render();
    setInterval(() => { if (state.overview) refreshOverview().then(() => {
      const side = document.querySelector(".sidebar");
      if (side) side.replaceWith(sidebar());
    }).catch(() => {}); }, 8000);
  } catch (e) {
    if (!(e instanceof ApiError && /sign in|password/i.test(e.message))) {
      put($app, h("div", { class: "login" }, h("div", { class: "card" }, h("h1", {}, "Can't reach ConanOps"),
        h("p", { class: "muted" }, e.message), h("button", { class: "btn primary", onclick: () => location.reload() }, "Try Again"))));
    }
  }
}
start();
