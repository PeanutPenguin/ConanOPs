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
  setTimeout(() => t.remove(), kind === "bad" || kind === "warn" ? 8000 : 4000);
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
    if (res && res.message) toast(res.message, res.ok === false ? "bad" : res.pending ? "warn" : "good");
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
// Same pages, order, groups and headings as the app on the PC.
const NAV_SECTIONS = [
  ["Manage", [["dashboard", "Dashboard"], ["players", "Players"], ["mods", "Mods"], ["updates", "Updates"],
              ["access", "Access"], ["console", "Console"], ["settings", "Server Settings"]]],
  ["System", [["app", "App Settings"]]],
];
const PAGE_HEADERS = {
  dashboard: ["Dashboard", "{server} at a glance: status, automation and who's online"],
  players: ["Players", "Everyone who has joined {server}"],
  mods: ["Mods", "Workshop mods, load order and update status"],
  updates: ["Updates", "Conan Exiles dedicated server builds from Steam"],
  access: ["Access", "Whitelist and bans"],
  console: ["Console", "Send RCON commands to the running server"],
  settings: ["Server Settings", "Everything ConanOps writes to {server}'s settings files"],
  app: ["App Settings", "ConanOps itself: startup, remote access, integrations and appearance"],
  more: ["More", ""],
};
const TAB_NAV = [["dashboard", "Dashboard"], ["players", "Players"], ["mods", "Mods"], ["console", "Console"]];
const MORE_NAV = [["updates", "Updates"], ["access", "Access"], ["settings", "Server Settings"], ["app", "App Settings"]];
const PC_WIDE = ["app", "more"];  // pages that aren't about one server

function server() { return state.overview && state.overview.servers.find((s) => s.id === state.sid); }

// Routes: #/view or #/view/section (Server Settings and App Settings).
const LEGACY = { backups: "settings/backups", diagnostics: "settings/diagnostics" };
function route() {
  let r = location.hash.replace(/^#\/?/, "") || "dashboard";
  const [v] = r.split("/");
  if (LEGACY[v]) r = LEGACY[v];
  const [view, sub] = r.split("/");
  return { view: VIEWS[view] ? view : "dashboard", sub: sub ? decodeURIComponent(sub) : "" };
}
function go(view, sub) {
  const target = "#/" + view + (sub ? "/" + encodeURIComponent(sub) : "");
  if (location.hash !== target) location.hash = target;
  else render();
}
window.addEventListener("hashchange", () => render());

// Which server this browser is looking at. Only this browser's choice --
// it never changes what the app on the PC is showing.
async function selectServer(id) {
  state.sid = id;
  try { localStorage.setItem("conanops.sid", id); } catch (e) { /* private mode */ }
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
    NAV_SECTIONS.map(([heading, items]) => [
      h("div", { class: "side-label" }, heading),
      items.map(([k, label]) => h("button", { class: "nav-btn" + (state.view === k ? " active" : ""),
        onclick: () => go(k) }, icon(k), label))]),
    h("div", { class: "spacer" }),
    h("button", { class: "nav-btn", onclick: logout }, icon("logout"), "Sign out"));
}

function tabbar() {
  const tabs = [...TAB_NAV, ["more", "More"]];
  const inMore = !TAB_NAV.some(([k]) => k === state.view);
  return h("nav", { class: "tabbar", "aria-label": "Main" }, tabs.map(([k, label]) => h("button", {
    class: "tab" + ((state.view === k || (k === "more" && inMore)) ? " active" : ""), onclick: () => go(k) },
    icon(k), label)));
}

// Same wording as the status in the app's header.
function serverStatusPill(s) {
  if (!s) return null;
  const pill = s.running
    ? h("span", { class: "pill on" }, s.max_players ? `Online · ${s.player_count} / ${s.max_players}` : `Online · ${s.player_count}`)
    : h("span", { class: "pill" }, "Stopped");
  return s.busy ? h("span", { class: "row", style: "gap:6px;flex-wrap:nowrap" }, h("span", { class: "spinner", title: s.busy }), pill) : pill;
}

function topbar() {
  const ov = state.overview;
  const s = server();
  const [title, sub] = PAGE_HEADERS[state.view] || ["", ""];
  const perServer = !PC_WIDE.includes(state.view);
  const select = perServer && ov && ov.servers.length > 1 ? h("select", { class: "server-select", "aria-label": "Server",
      onchange: (e) => selectServer(e.target.value) },
    ov.servers.map((x) => h("option", { value: x.id, selected: x.id === state.sid }, x.name))) : null;
  return h("header", { class: "topbar" },
    h("div", { class: "title" }, h("h1", {}, title),
      sub ? h("div", { class: "dim subtitle" }, sub.replace("{server}", s ? s.name : "your server")) : null),
    perServer ? serverStatusPill(s) : null, select);
}

function layout(content) {
  return h("div", { class: "shell" }, sidebar(),
    h("main", { class: "main" + (state.view === "settings" || state.view === "app" ? " wide" : "") }, topbar(), content),
    tabbar());
}

async function refreshOverview() {
  const ov = await api("/api/overview");
  state.overview = ov;
  applyTheme(ov.theme);
  if (!ov.servers.some((s) => s.id === state.sid)) {
    let saved = null;
    try { saved = localStorage.getItem("conanops.sid"); } catch (e) { /* private mode */ }
    state.sid = ov.servers.some((s) => s.id === saved) ? saved : (ov.active_id || (ov.servers[0] || {}).id || null);
  }
  return ov;
}

function stopTimer() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }

async function render() {
  stopTimer();
  const r = route();
  state.view = r.view;
  state.sub = r.sub;
  if (!state.overview) return;
  if (!server() && !PC_WIDE.includes(state.view)) {
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

// A side list of sections (grouped like the app) next to one panel; on
// a phone the list becomes a dropdown. groups: [[heading, [[key, label]]]].
function sectioned(groups, active, onPick) {
  const panel = h("div", { class: "section-panel stack" });
  const all = groups.flatMap(([, items]) => items);
  const nav = h("nav", { class: "subnav", "aria-label": "Sections" },
    groups.map(([heading, items]) => [heading ? h("div", { class: "side-label" }, heading) : null,
      items.map(([k, label]) => h("button", { class: "nav-btn" + (k === active ? " active" : ""), onclick: () => onPick(k) }, label))]));
  const picker = h("select", { class: "section-select", "aria-label": "Section", onchange: (e) => onPick(e.target.value) },
    groups.map(([heading, items]) => h("optgroup", { label: heading || " " },
      items.map(([k, label]) => h("option", { value: k, selected: k === active }, label)))));
  return { el: h("div", { class: "split" }, nav, h("div", { class: "split-main" }, picker, panel)), panel,
           label: (all.find(([k]) => k === active) || ["", ""])[1] };
}

async function logout() {
  try { await api("/api/logout", {}); } catch (e) { /* ignore */ }
  showLogin("Signed out.");
}

const sid = () => encodeURIComponent(state.sid);
const sp = (p) => `/api/servers/${sid()}${p}`;

// Re-reads a view every few seconds -- but never while someone is typing
// in it, so a refresh doesn't wipe what they're entering.
function poll(root, paint, ms) {
  state.timer = setInterval(() => {
    const a = document.activeElement;
    if (a && root.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) return;
    if (document.querySelector(".modal-back")) return;
    if (root.querySelector("[data-dirty]")) return;  // unsaved typing would be lost
    paint().catch(() => {});
  }, ms);
}

const notice = (text, kind, ...extra) => h("div", { class: "notice" + (kind ? " " + kind : "") },
  h("div", { class: "text" }, text), ...extra);

// --------------------------------------------------------------- dashboard
const TILE_TARGETS = { restart: ["settings", "restart"], updates: ["updates"], backups: ["settings", "backups"],
                       alerts: ["app", "alerts"], ddns: ["app", "ddns"] };

function openTarget(key) {
  const t = TILE_TARGETS[key];
  if (t) go(t[0], t[1]);
}

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
      h("button", { class: "btn", onclick: async (e) => {
        const b = e.currentTarget;
        const text = s.running ? `Players online: ${s.player_count}. They get a 10-second warning first, and the ` +
          "world is saved." : "It isn't running -- this starts it.";
        if (await confirmBox("Restart the server?", text, "Restart")) act(b, () => api(sp("/restart"), {}), { after: paintSoon });
      } }, "Restart"),
      s.running ? h("button", { class: "btn danger", onclick: async (e) => {
        const b = e.currentTarget;
        if (await confirmBox("Stop the server?", "The world is saved first. It stays stopped (no automatic " +
          "restarts) until you start it again.", "Stop", true)) act(b, () => api(sp("/stop"), {}), { after: paintSoon });
      } }, "Stop") : null)
      : h("div", { class: "dim" }, "Finish this server's setup in the app on the PC.");

    const notices = [];
    if (s.hold) notices.push(notice((s.running ? "" : "Held stopped. ") + s.hold, "",
      s.running || s.busy ? null : h("button", { class: "btn small", onclick: (e) => act(e.currentTarget,
        () => api(sp("/start"), {}), { after: paintSoon }) }, "Start Anyway")));
    if (!d.rcon_enabled && s.installed) notices.push(notice(
      "RCON is off, so ConanOps can't save the world before stopping, and kick/ban/console won't work.", "",
      h("button", { class: "btn small", onclick: (e) => act(e.currentTarget, () => api(sp("/enable-rcon"), {}),
        { after: paintSoon }) }, "Turn On RCON")));
    if (d.waiting_for.length) notices.push(notice(
      `Waiting for a fix to: ${d.waiting_for.join(", ")}. The server starts again by itself once it's updated.`));
    if (d.needs_pc && d.needs_pc.length) notices.push(notice(
      "Waiting for someone at the PC (Windows needs permission there): " + d.needs_pc.join("; ") +
      ". ConanOps asks the next time the PC is used."));

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
      h("div", { class: "grid stats" },
        statCard("CPU", st.cpu !== undefined && st.cpu !== null ? `${st.cpu}%` : "—", "of the whole PC"),
        statCard("Memory", st.mem_mb ? `${(st.mem_mb / 1024).toFixed(1)} GB` : "—", st.uptime_seconds ? `up ${fmtDuration(st.uptime_seconds)}` : ""),
        statCard("Server FPS", d.fps ? Number(d.fps).toFixed(1) : "—", ""),
        statCard("Players", s.max_players ? `${s.player_count} / ${s.max_players}` : String(s.player_count),
          s.player_count ? "" : "nobody online")),
      s.players.length ? h("div", { class: "chips" }, s.players.map((n) => h("span", { class: "chip static" }, n))) : null,
      h("div", { class: "section-title" }, h("h2", {}, "Automation")),
      h("div", { class: "grid tiles" }, d.automation.map((t) => h("div", { class: "card tile" },
        h("div", { class: "mark" }, t.name.slice(0, 2).toUpperCase()),
        h("div", { class: "text" }, h("div", {}, t.name), h("div", { class: "state" + (t.on ? " on" : "") }, t.detail)),
        TILE_TARGETS[t.key] ? h("button", { class: "btn small", onclick: () => openTarget(t.key) }, "Change") : null))),
      h("div", { class: "section-title" }, h("h2", {}, "Server log")),
      log);
    log.scrollTop = stick ? log.scrollHeight : prevTop;
  };
  const paintSoon = () => setTimeout(() => paint().catch(() => {}), 1500);
  await paint();
  poll(root, paint, 3000);
}
function statCard(label, value, sub) {
  return h("div", { class: "card stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    h("div", { class: "sub" }, sub || " "));
}

// ----------------------------------------------------------------- players
async function viewPlayers(root) {
  const search = h("input", { type: "search", placeholder: "Search players", value: state.playerFilter || "",
    oninput: () => { state.playerFilter = search.value; draw(); } });
  const chart = h("div", { class: "card" });
  const list = h("div", { class: "card" });
  const head = h("div", { class: "stack" });
  let data = null;
  const draw = () => {
    const d = data;
    const q = (state.playerFilter || "").trim().toLowerCase();
    const people = d.players.filter((p) => !q || p.name.toLowerCase().includes(q));
    put(list, people.length ? h("div", { class: "list" }, people.map((p) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", { class: "name" }, p.name),
        h("div", { class: "dim" }, `${fmtDuration(p.playtime_seconds)} played · ${p.sessions} session${p.sessions === 1 ? "" : "s"}` +
          (p.online ? "" : p.last_seen ? ` · last seen ${fmtWhen(p.last_seen)}` : ""))),
      p.online ? h("span", { class: "pill on" }, "Online") : null,
      p.online && d.rcon_enabled ? h("button", { class: "btn small", onclick: async (e) => {
        const b = e.currentTarget;
        if (await confirmBox(`Kick ${p.name}?`, "They can join again right away.", "Kick", true))
          act(b, () => api(sp("/players/kick"), { name: p.name }), { after: paint });
      } }, "Kick") : null,
      h("button", { class: "btn small danger", onclick: async (e) => {
        const b = e.currentTarget;
        const id = await promptBox(`Ban ${p.name}`, "Enter their Steam ID (the 17-digit number from their Steam profile).",
          "7656119…", "Ban");
        if (id) act(b, () => api(sp("/players/ban"), { name: p.name, steam_id: id }), { after: paint });
      } }, "Ban"))))
      : h("div", { class: "empty" }, q ? "No one matches." : "Nobody has played yet."));
  };
  const paint = async () => {
    data = await api(sp("/players"));
    const d = data;
    const max = Math.max(1, ...d.activity);
    put(chart, h("h3", {}, "When people play"),
      h("div", { class: "bars", "aria-label": "Sessions started per hour of the day" },
        d.activity.map((n, i) => h("span", { style: `height:${Math.round((n / max) * 100)}%`, title: `${i}:00 — ${n}` }))),
      h("div", { class: "bars-axis" }, h("span", {}, "12 AM"), h("span", {}, "6 AM"), h("span", {}, "12 PM"), h("span", {}, "6 PM")));
    put(head, d.rcon_enabled ? null : notice("RCON is off for this server, so kicking isn't available (bans still " +
      "work and take effect at the next restart)."));
    draw();
  };
  await paint();
  put(root, head, chart, search, list);
  poll(root, paint, 10000);
}

// -------------------------------------------------------------------- mods
async function viewMods(root) {
  const top = h("div", { class: "stack" });
  const listCard = h("div", { class: "card" });
  const statusPill = (m) => m.broken ? h("span", { class: "pill bad" }, "Broken")
    : m.downloaded === false ? h("span", { class: "pill warn" }, "Not downloaded")
    : m.status === "missing" ? h("span", { class: "pill bad" }, "Not on the Workshop")
    : m.status === "updated" ? h("span", { class: "pill on" }, "Up to date")
    : m.status === "stale" ? h("span", { class: "pill warn" }, "Not updated for this patch")
    : m.status === "legacy" ? h("span", { class: "pill warn" }, "Legacy") : null;
  const paint = async () => {
    const d = await api(sp("/mods"));
    const busy = !!d.busy;
    put(top,
      d.busy ? notice(d.busy + "…", "") : null,
      d.summary ? notice(d.summary, d.summary_ok ? "good" : "") : null,
      h("div", { class: "card" }, h("div", { class: "row" },
        h("button", { class: "btn primary", disabled: busy || !d.mods.length || !d.steamcmd,
          onclick: (e) => act(e.currentTarget, () => api(sp("/mods/download"), {}), { after: paint }) },
          d.busy === "Downloading mods" ? "Downloading…" : "Download Mods"),
        h("button", { class: "btn", disabled: busy || !d.mods.some((m) => m.enabled), onclick: async (e) => {
          const b = e.currentTarget;
          if (await confirmBox("Find the broken mod?", "ConanOps stops the server and tests the mods, restarting it " +
            "many times. Your world is copied first and put back exactly as it was. Can take a while; you'll get an " +
            "alert with the result.", "Start Check"))
            act(b, () => api(sp("/mods/find-broken"), {}), { after: paint });
        } }, "Find Broken Mod"),
        h("button", { class: "btn", disabled: !d.mods.length, onclick: async (e) => {
          const b = e.currentTarget;
          await act(b, async () => { await api(sp("/mods?refresh=1")); return { ok: true, message: "Checked." }; }, { after: paint });
        } }, "Check for Outdated Mods")),
        h("div", { class: "dim", style: "margin-top:8px" }, "Changes load the next time the server starts. Order matters: top loads first.")));
    put(listCard, d.mods.length ? h("div", { class: "list" }, d.mods.map((m, i) => h("div", { class: "item" },
      h("input", { type: "checkbox", class: "switch", checked: m.enabled, disabled: busy, "aria-label": `Use ${m.name}`,
        onchange: (e) => act(null, () => api(sp("/mods/toggle"), { id: m.id, enabled: e.target.checked }), { after: paint }) }),
      h("div", { class: "grow" }, h("div", { class: "name" }, m.name),
        h("div", { class: "row" }, h("span", { class: "dim mono" }, m.id), statusPill(m),
          m.needs.length ? h("span", { class: "pill warn" }, "Needs: " + m.needs.join(", ")) : null,
          m.updated ? h("span", { class: "dim" }, "updated " + m.updated) : null)),
      h("button", { class: "btn small icon", "aria-label": "Move up", disabled: busy || i === 0,
        onclick: () => act(null, () => api(sp("/mods/move"), { id: m.id, direction: -1 }), { after: paint }) }, icon("up")),
      h("button", { class: "btn small icon", "aria-label": "Move down", disabled: busy || i === d.mods.length - 1,
        onclick: () => act(null, () => api(sp("/mods/move"), { id: m.id, direction: 1 }), { after: paint }) }, icon("down")),
      h("button", { class: "btn small icon danger", "aria-label": "Remove", disabled: busy, onclick: async (e) => {
        const b = e.currentTarget;
        if (await confirmBox(`Remove ${m.name}?`, "The next time the server starts, anything this mod added to the " +
          "world (buildings, items) is deleted.", "Remove", true))
          act(b, () => api(sp("/mods/remove"), { id: m.id }), { after: paint });
      } }, icon("trash"))))) : h("div", { class: "empty" }, "No mods on this server."));
    return d;
  };
  const first = await paint();

  // Adding mods (built once, so a refresh never wipes a search).
  const idInput = h("input", { type: "text", placeholder: "Workshop ID or link" });
  const results = h("div", { class: "list" });
  const more = h("div", {});
  const searchInput = h("input", { type: "search", placeholder: "Search the Workshop (or leave empty to browse)" });
  const sort = h("select", { "aria-label": "Sort" }, [["", "Best match / popular"], ["popular", "Most popular"],
    ["recently_updated", "Recently updated"]].map(([v, l]) => h("option", { value: v }, l)));
  const showAll = h("input", { type: "checkbox" });
  let cursor = "*";
  const runSearch = async (btn, append) => {
    if (!append) cursor = "*";
    const q = `?q=${encodeURIComponent(searchInput.value)}&sort=${sort.value}&cursor=${encodeURIComponent(cursor)}` +
      (showAll.checked ? "&show=updated,stale,legacy,unknown" : "");
    const r = await act(btn, () => api(sp("/mods/search" + q)));
    if (!r) return;
    cursor = r.next_cursor;
    const rows = r.results.map((x) => h("div", { class: "item" },
      x.preview ? h("img", { src: x.preview, alt: "", width: 48, height: 48, style: "border-radius:8px;object-fit:cover" }) : null,
      h("div", { class: "grow" }, h("div", { class: "name" }, x.title),
        h("div", { class: "dim" }, `${x.subscriptions.toLocaleString()} subscribers · ${x.status_label}`),
        x.needs.length ? h("div", { class: "dim" }, "Also needs: " + x.needs.join(", ")) : null),
      x.added ? h("span", { class: "pill on" }, "Added") :
        h("button", { class: "btn small", onclick: (ev) => act(ev.currentTarget, () => api(sp("/mods/add"), { id: x.id, name: x.title }),
          { after: async (res) => { if (res && res.ok !== false) { ev.target.replaceWith(h("span", { class: "pill on" }, "Added")); await paint(); } } }) }, "Add")));
    if (append) results.append(...rows); else put(results, ...(rows.length ? rows : [h("div", { class: "empty" }, "Nothing found.")]));
    put(more, cursor ? h("button", { class: "btn", style: "margin-top:10px;width:100%", onclick: (e) => runSearch(e.currentTarget, true) }, "Load More") : null);
  };
  const addCard = h("div", { class: "card" }, h("h3", {}, "Add a mod"),
    h("form", { class: "row", style: "margin-top:10px", onsubmit: (e) => { e.preventDefault();
        act(e.submitter, () => api(sp("/mods/add"), { id: idInput.value }), { after: (r) => { if (r && r.ok !== false) idInput.value = ""; return paint(); } }); } },
      h("div", { style: "flex:1;min-width:180px" }, idInput), h("button", { class: "btn primary", type: "submit" }, "Add")),
    first.search_available ? [
      h("form", { class: "row", style: "margin-top:14px", onsubmit: (e) => { e.preventDefault(); runSearch(e.submitter, false); } },
        h("div", { style: "flex:1;min-width:180px" }, searchInput), sort, h("button", { class: "btn", type: "submit" }, "Search")),
      h("label", { class: "check muted", style: "margin-top:8px" }, showAll, "Include mods not updated for the current patch"),
      results, more]
      : h("div", { class: "dim", style: "margin-top:10px" }, "To search the Workshop here, add a Steam API key under ConanOps → Steam Workshop."));
  put(root, top, listCard, addCard);
  poll(root, paint, 10000);
}

// ----------------------------------------------------------------- console
async function viewConsole(root) {
  const out = h("div", { class: "log tall mono", role: "log" });
  const input = h("input", { type: "text", placeholder: "RCON command, e.g. listplayers", autocomplete: "off", autocapitalize: "off" });
  const say = h("input", { type: "text", placeholder: "Message to everyone online" });
  const key = "conanops.console." + state.sid;
  const history = JSON.parse(sessionStorage.getItem(key) || "[]");
  const write = () => { out.textContent = history.join("\n\n") || "Send a command to see the server's reply."; out.scrollTop = out.scrollHeight; };
  const send = async (cmd, btn) => {
    if (!cmd) return;
    const r = await act(btn, () => api(sp("/console"), { command: cmd }));
    if (!r) return;
    history.push("> " + cmd, r.output);
    while (history.length > 80) history.shift();
    sessionStorage.setItem(key, JSON.stringify(history));
    write();
  };
  put(root,
    out,
    h("div", { class: "chips" }, ["listplayers", "saveworld"].map((c) => h("button", { class: "chip", onclick: (e) => send(c, e.currentTarget) }, c))),
    h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); const c = input.value.trim(); input.value = ""; send(c, e.submitter); } },
      h("div", { style: "flex:1;min-width:200px" }, input), h("button", { class: "btn primary", type: "submit" }, "Send")),
    h("form", { class: "row", onsubmit: (e) => { e.preventDefault();
        act(e.submitter, () => api(sp("/broadcast"), { message: say.value })).then((r) => { if (r && r.ok) say.value = ""; }); } },
      h("div", { style: "flex:1;min-width:200px" }, say), h("button", { class: "btn", type: "submit" }, "Broadcast")));
  write();
}

// ----------------------------------------------------------------- updates
async function viewUpdates(root) {
  const paint = async () => {
    const d = await api(sp("/updates"));
    const busy = d.updating || (d.busy && d.busy !== "Checking for an update");
    put(root,
      d.busy ? notice(d.busy + "…") : null,
      d.hold ? notice(d.hold) : null,
      h("div", { class: "card" }, h("div", { class: "row" },
        h("div", { class: "grow", style: "flex:1" }, h("div", { class: "dim" }, "Installed build"), h("h2", {}, d.installed)),
        d.state ? h("span", { class: "pill " + (/up to date/i.test(d.state) ? "on" : /not checked/i.test(d.state) ? "" : "warn") }, d.state) : null),
        h("div", { class: "row", style: "margin-top:12px" },
          h("button", { class: "btn", disabled: d.checking || d.updating, onclick: (e) => act(e.currentTarget, () => api(sp("/updates/check"), {}),
            { after: () => setTimeout(() => paint().catch(() => {}), 3000) }) }, d.checking ? "Checking…" : "Check Now"),
          d.pending ? h("button", { class: "btn primary", disabled: busy, onclick: async (e) => {
            const b = e.currentTarget;
            if (await confirmBox("Update the server now?", "It's stopped and backed up first, then started again " +
              "if it was running. Players are disconnected.", "Update Now")) act(b, () => api(sp("/updates/install"), {}), { after: paint });
          } }, d.updating ? "Updating…" : "Back Up & Update Now") : null),
        d.last_check ? h("div", { class: "dim", style: "margin-top:8px" }, "Last automatic check: " + fmtWhen(d.last_check)) : null),
      d.pending ? h("div", { class: "card" }, h("h3", {}, d.pending), h("div", { class: "log mono", style: "height:240px;margin-top:10px" }, d.changelog)) : null,
      h("div", { class: "card" }, h("h3", {}, "Automatic updates"),
        h("div", { class: "field" }, h("div", { class: "top" }, h("label", { for: "au" }, "Install new builds automatically (only when nobody's online)"),
          h("input", { id: "au", type: "checkbox", class: "switch", checked: d.auto_update,
            onchange: (e) => act(null, () => api(sp("/updates/settings"), { auto_update: e.target.checked }), { after: paint }) }))),
        h("div", { class: "field" }, h("div", { class: "top" }, h("label", { for: "ai" }, "Check every (hours)"),
          h("input", { id: "ai", type: "number", min: 1, max: d.interval_max, value: d.interval_hours, style: "width:100px",
            onchange: (e) => act(null, () => api(sp("/updates/settings"), { interval_hours: Number(e.target.value) }), { after: paint }) }))),
        h("div", { class: "help" }, "It also always checks right after a scheduled restart.")),
      h("div", { class: "card" }, h("h3", {}, "Update history"),
        d.history.length ? h("div", { class: "list" }, d.history.map((x) => h("div", { class: "item" },
          h("div", { class: "grow" }, h("div", { class: "name" }, x.change), h("div", { class: "dim" }, x.when)),
          h("span", { class: "pill " + (x.result === "succeeded" ? "on" : "bad") }, x.result))))
          : h("div", { class: "empty" }, "No updates since ConanOps started.")));
  };
  await paint();
  poll(root, paint, 6000);
}

// ----------------------------------------------------------------- backups
function uploadBackup(file, btn, bar) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", sp("/backups/import"));
    xhr.setRequestHeader("X-ConanOps", "1");
    xhr.setRequestHeader("Content-Type", "application/zip");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    btn.disabled = true;
    bar.hidden = false;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = e.loaded / e.total; };
    xhr.onloadend = () => {
      btn.disabled = false;
      bar.hidden = true;
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* empty */ }
      if (xhr.status === 401) { showLogin(); return resolve(null); }
      if (!xhr.status) toast("The upload didn't get through -- check the connection.", "bad");
      else toast(data.message || data.error || `Error ${xhr.status}`, xhr.status === 200 && data.ok !== false ? "good" : "bad");
      resolve(data);
    };
    xhr.send(file);
  });
}

async function viewBackups(root) {
  const top = h("div", { class: "stack" });
  const list = h("div", { class: "card" });
  const paint = async () => {
    const d = await api(sp("/backups"));
    const busy = ["Restoring a backup", "Updating", "Finding a broken mod", "Testing mods (in the app)"].includes(d.busy);
    put(top,
      d.busy ? notice(d.busy + "…") : null,
      h("div", { class: "card" }, h("div", { class: "row" },
        h("div", { style: "flex:1;min-width:0" }, h("div", { class: "dim" }, "Saved to"),
          h("div", { class: "mono", style: "word-break:break-all" }, d.destination || "Not set"),
          d.last_backup ? h("div", { class: "dim" }, "Last scheduled backup: " + fmtWhen(d.last_backup)) : null),
        h("button", { class: "btn primary", disabled: busy || !d.installed || !d.destination,
          onclick: (e) => act(e.currentTarget, () => api(sp("/backups/create"), {}), { after: paint }) }, "Back Up Now")),
));
    put(list, d.backups.length ? h("div", { class: "list" }, d.backups.map((b) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", { class: "name" }, fmtWhen(b.when)),
        h("div", { class: "dim" }, `${b.trigger} · ${b.size_mb} MB`)),
      h("button", { class: "btn small", disabled: busy, onclick: async (e) => {
        const btn = e.currentTarget;
        if (await confirmBox("Restore this backup?", `${fmtWhen(b.when)}\n\nThe server stops, the world goes back to ` +
          "this point (anything since is lost -- a safety copy of the current world is kept), then it starts again " +
          "if it was running.", "Restore", true)) act(btn, () => api(sp("/backups/restore"), { name: b.name }), { after: paint });
      } }, "Restore")))) : h("div", { class: "empty" }, "No backups yet."));
  };
  await paint();
  const file = h("input", { type: "file", accept: ".zip,application/zip" });
  const bar = h("progress", { max: 1, value: 0, hidden: true, style: "width:100%" });
  const importBtn = h("button", { class: "btn", onclick: async (e) => {
    if (!file.files.length) { toast("Choose a backup .zip first.", "bad"); return; }
    const f = file.files[0];
    if (state.remote && f.size > 95 * 1024 * 1024) {
      toast("Over the internet link, uploads are limited to about 100 MB. Use the Wi-Fi link at home for bigger files.", "bad");
      return;
    }
    const r = await uploadBackup(f, e.currentTarget, bar);
    if (r && r.ok !== false) { file.value = ""; paint().catch(() => {}); }
  } }, "Import");
  put(root, top, list, h("div", { class: "card" }, h("h3", {}, "Import a backup"),
    h("p", { class: "muted", style: "margin:6px 0 10px" }, "A world-save .zip from another server or an older " +
      "copy. It's checked, added to this list, and can then be restored like any other backup."),
    h("div", { class: "row" }, h("div", { style: "flex:1;min-width:180px" }, file), importBtn), bar));
  poll(root, paint, 8000);
}

// ------------------------------------------------------------------ access
async function viewAccess(root) {
  const top = h("div", { class: "stack" });
  const wlList = h("div", {});
  const banList = h("div", {});
  const listOf = (ids, removeLabel, onRemove) => ids.length ? h("div", { class: "list" }, ids.map((id) => h("div", { class: "item" },
    h("div", { class: "grow mono" }, id), h("button", { class: "btn small", onclick: (e) => act(e.currentTarget, () => onRemove(id), { after: paint }) }, removeLabel))))
    : h("div", { class: "empty" }, "Nobody here.");
  const paint = async () => {
    const d = await api(sp("/access"));
    put(top,
      d.installed ? null : notice("This server isn't set up yet -- finish setup in the app on the PC."),
      d.rcon_enabled ? null : notice("RCON is off, so bans take effect at the next restart."),
      h("div", { class: "card" }, h("div", { class: "field" }, h("div", { class: "top" }, h("label", { for: "wl" }, "Only let whitelisted players join"),
        h("input", { id: "wl", type: "checkbox", class: "switch", checked: d.whitelist_enabled, disabled: !d.installed,
          onchange: (e) => act(null, () => api(sp("/access/whitelist-mode"), { on: e.target.checked }), { after: paint }) })))));
    put(wlList, listOf(d.whitelist, "Remove", (id) => api(sp("/access/whitelist"), { steam_id: id, remove: true })));
    put(banList, listOf(d.banned, "Unban", (id) => api(sp("/access/unban"), { steam_id: id })));
  };
  await paint();
  const wlInput = h("input", { type: "text", placeholder: "Steam ID", inputmode: "numeric" });
  const banInput = h("input", { type: "text", placeholder: "Steam ID", inputmode: "numeric" });
  const clearOk = (input) => async (r) => { if (r && r.ok !== false) input.value = ""; await paint(); };
  put(root, top,
    h("div", { class: "card" }, h("h3", {}, "Whitelist"),
      h("form", { class: "row", style: "margin:10px 0", onsubmit: (e) => { e.preventDefault();
          act(e.submitter, () => api(sp("/access/whitelist"), { steam_id: wlInput.value }), { after: clearOk(wlInput) }); } },
        h("div", { style: "flex:1;min-width:180px" }, wlInput), h("button", { class: "btn", type: "submit" }, "Add")),
      wlList),
    h("div", { class: "card" }, h("h3", {}, "Banned"),
      h("form", { class: "row", style: "margin:10px 0", onsubmit: (e) => { e.preventDefault();
          act(e.submitter, () => api(sp("/access/ban"), { steam_id: banInput.value }), { after: clearOk(banInput) }); } },
        h("div", { style: "flex:1;min-width:180px" }, banInput), h("button", { class: "btn danger", type: "submit" }, "Ban")),
      banList));
  poll(root, paint, 10000);
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
    input = h("input", { id, type: "checkbox", class: "switch", checked: !!f.value });
  } else if (f.type === "choice") {
    input = h("select", { id }, f.choices.map((c) => h("option", { selected: JSON.stringify(c.value) === JSON.stringify(f.value) }, c.label)));
  } else if (f.type === "float" && f.max - f.min <= 1000) {
    const out = h("output", {}, Number(f.value).toFixed(f.decimals));
    input = h("input", { id, type: "range", min: f.min, max: f.max, step: f.step || 0.1, value: f.value,
      oninput: () => { out.textContent = Number(input.value).toFixed(f.decimals); } });
    input.addEventListener("input", () => onChange(f.key, val()));
    return { el: h("div", { class: "range-row" }, input, out), input, val };
  } else if (f.type === "int" || f.type === "float") {
    input = h("input", { id, type: "number", min: f.min, max: f.max, step: f.step || (f.type === "int" ? 1 : 0.1),
      value: f.value, style: "max-width:160px" });
  } else if (f.type === "time") {
    input = h("input", { id, type: "time", value: f.value || "", style: "max-width:160px" });
  } else if (f.type === "secret") {
    // Saved secrets never come to the browser; typing replaces one.
    input = h("input", { id, type: "password", value: "", autocomplete: "new-password",
      placeholder: f.has_value ? "Saved -- type to replace it" : (f.placeholder || "Not set") });
    input.addEventListener("input", () => onChange(f.key, input.value === "" ? undefined : input.value));
    const remove = f.has_value ? h("button", { type: "button", class: "btn small", onclick: () => {
      input.value = ""; input.placeholder = "Will be removed when you save"; onChange(f.key, "");
    } }, "Remove") : null;
    return { el: h("div", { class: "row" }, h("div", { style: "flex:1;min-width:160px" }, input), remove), input, val: () => input.value };
  } else {
    input = h("input", { id, type: "text", value: f.value ?? "", placeholder: f.placeholder || "", autocomplete: "off" });
  }
  input.addEventListener(f.type === "bool" || f.type === "choice" ? "change" : "input", () => onChange(f.key, val()));
  return { el: input, input, val };
}

// A "How do I...?" guide that folds out, with each step folding out too.
function foldOutGuide(g) {
  return h("details", { class: "guide" }, h("summary", {}, g.title),
    h("p", { class: "muted" }, g.intro),
    h("ol", { class: "guide-steps" }, g.steps.map((st) => h("li", {},
      h("details", {}, h("summary", {}, h("span", { class: "step-num" }, String(st.number)), st.title),
        h("p", { class: "muted" }, st.body))))));
}

// Server Settings: the same sections, in the same groups, as the app.
const SERVER_GROUP = ["diagnostics", "identity", "network"];
const AUTOMATION_GROUP = ["backups", "restart", "alerts"];

async function viewSettings(root) {
  const d = await api(sp("/settings"));
  const entries = [["diagnostics", "Diagnostics"], ...d.pages.map((p) => [p.key, p.title])];
  const keys = entries.map(([k]) => k);
  let active = state.sub || state.settingsPage || "diagnostics";
  if (!keys.includes(active)) active = "diagnostics";
  state.settingsPage = active;
  const groups = [
    ["Server", entries.filter(([k]) => SERVER_GROUP.includes(k))],
    ["Gameplay", entries.filter(([k]) => !SERVER_GROUP.includes(k) && !AUTOMATION_GROUP.includes(k))],
    ["Automation", entries.filter(([k]) => AUTOMATION_GROUP.includes(k))],
  ];
  const { el, panel } = sectioned(groups, active, (k) => go("settings", k));
  put(root, el);
  if (active === "diagnostics") return viewDiagnostics(panel);
  drawSettings(panel, d, d.pages.find((p) => p.key === active), {});
  if (active === "network") {
    // Like the app's Network & Ports page.
    panel.append(h("div", { class: "card" }, h("h3", {}, "Repair Networking"),
      h("p", { class: "muted", style: "margin:6px 0 10px" }, "Re-creates this server's Windows Firewall rules and " +
        "router port forwards (UPnP) for the saved ports."),
      h("button", { class: "btn", onclick: (e) => act(e.currentTarget, () => api(sp("/repair-network"), {})) }, "Repair Networking")));
  }
  if (active === "backups") {
    // Like the app: the backup list sits under the backup settings.
    const list = h("div", { class: "stack" });
    panel.append(h("h2", { class: "panel-title", style: "margin-top:10px" }, "Saved backups"), list);
    await viewBackups(list);
  }
}

function drawSettings(root, d, page, preset) {
  const saved = Object.fromEntries(page.fields.map((f) => [f.key, f.value]));
  const changes = {};
  const inputs = {};
  const saveBar = h("div", { class: "row end save-bar" });
  const current = (k) => (k in changes ? changes[k] : saved[k]);
  const refreshEnabled = () => {
    for (const f of page.fields) {
      if (!inputs[f.key]) continue;
      const on = f.enabled || (f.enabled_when && Object.entries(f.enabled_when).every(([k, v]) => current(k) === v));
      inputs[f.key].disabled = !on;
    }
  };
  const updateBar = () => {
    const n = Object.keys(changes).length;
    put(saveBar, ...(n ? [
      h("span", { class: "dim" }, `${n} unsaved change${n === 1 ? "" : "s"}`),
      h("button", { class: "btn", onclick: () => render() }, "Discard"),
      h("button", { class: "btn primary", onclick: (e) => act(e.currentTarget,
        () => api(sp("/settings/" + encodeURIComponent(page.key)), { values: changes }), { after: (r) => { if (r && r.ok !== false) render(); } }) },
        /restart/i.test(page.apply_label) ? "Save" : (page.apply_label || "Save"))] : []));
  };
  const setChange = (key, value, row) => {
    const f = page.fields.find((x) => x.key === key);
    if (value === undefined || (f.type !== "secret" && JSON.stringify(value) === JSON.stringify(saved[key]))) delete changes[key];
    else changes[key] = value;
    if (row) row.classList.toggle("dirty", key in changes);
    refreshEnabled();
    updateBar();
  };
  const rows = page.fields.map((f0) => {
    const f = (f0.key in preset && f0.type !== "secret") ? { ...f0, value: preset[f0.key] } : f0;
    const row = h("div", { class: "field" });
    const { el, input } = fieldInput(f, (key, value) => setChange(key, value, row));
    inputs[f.key] = input;
    if (f0.type === "secret" && f0.key in preset) {
      input.value = preset[f0.key];
      changes[f0.key] = preset[f0.key];
      row.classList.add("dirty");
    }
    row.append(...clean([f.type === "bool" ? h("div", { class: "top" }, h("label", { for: "f_" + f.key }, f.label), el)
      : h("label", { for: "f_" + f.key }, f.label), f.type === "bool" ? null : el,
      f.help ? h("div", { class: "help" }, f.help) : null]));
    const testKind = page.tests && page.tests[f.key];
    if (testKind) {
      const result = h("span", { class: "dim" });
      row.append(h("div", { class: "row", style: "margin-top:8px" },
        h("button", { class: "btn small", type: "button", onclick: async (e) => {
          result.textContent = "";
          const r = await act(e.currentTarget, () => api(sp("/alerts/test"), { kind: testKind, url: changes[f.key] || "" }));
          if (r) { result.textContent = r.message; result.className = r.ok ? "ok-text" : "bad-text"; }
        } }, "Send Test"), result));
    }
    const guide = page.guides && page.guides[f.key];
    if (guide) row.append(foldOutGuide(guide));
    if (f0.key in preset && f0.type !== "secret" && JSON.stringify(preset[f0.key]) !== JSON.stringify(saved[f0.key])) {
      changes[f0.key] = preset[f0.key];
      row.classList.add("dirty");
    }
    return row;
  });
  const actions = (page.actions || []).map((a) => h("button", { class: "btn small", onclick: async (e) => {
    const r = await act(e.currentTarget, () => api(sp("/settings/" + encodeURIComponent(page.key) + "/action"),
      { action: a.id, values: changes }));
    if (!r || !r.page) return;
    const next = {};
    for (const f of r.page.fields) {
      if (f.type !== "secret") next[f.key] = f.value;
      else if (f.key in changes) next[f.key] = changes[f.key];  // keep what was typed
    }
    drawSettings(root, d, { ...page, error: r.page.error }, next);
  } }, a.label));
  put(root,
    h("h2", { class: "panel-title" }, page.title),
    page.note ? h("div", { class: "dim" }, page.note) : null,
    page.error ? notice(page.error, "bad") : null,
    actions.length ? h("div", { class: "row" }, actions) : null,
    h("div", { class: "card" }, rows),
    saveBar);
  refreshEnabled();
  updateBar();
}

// ------------------------------------------------------------- diagnostics
async function showGuide(btn) {
  const g = await act(btn, () => api(sp("/port-guide")));
  if (!g) return;
  const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) back.remove(); } },
    h("div", { class: "modal wide", role: "dialog", "aria-modal": "true" },
      h("h2", {}, "Port forwarding guide"), h("p", { class: "muted" }, g.intro),
      h("div", { class: "stack guide" }, g.steps.map((s) => h("div", { class: "card" },
        h("div", { class: "row" }, h("span", { class: "step-num" }, String(s.number)), h("h3", {}, s.title)),
        h("p", { class: "muted", style: "margin:8px 0 0" }, s.body)))),
      h("div", { class: "row end" }, h("button", { class: "btn primary", onclick: () => back.remove() }, "Close"))));
  document.body.append(back);
}

async function viewDiagnostics(root) {
  const results = h("div", { class: "stack" });
  const summary = h("div", { class: "dim" });
  const run = async (btn) => {
    put(results, h("div", { class: "card empty" }, "Checking… this takes a few seconds."));
    const r = await act(btn, () => api(sp("/diagnostics"), {}));
    if (!r) { put(results); return; }
    summary.textContent = `Last run ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} -- ${r.summary}`;
    const order = { error: 0, warning: 1, ok: 2 };
    r.results.sort((a, b) => order[a.status] - order[b.status]);
    put(results, ...r.results.map((x) => h("div", { class: "card" },
      h("div", { class: "row" }, h("span", { class: "pill " + (x.status === "ok" ? "on" : x.status === "warning" ? "warn" : "bad") },
        x.status === "ok" ? "OK" : x.status === "warning" ? "Check" : "Problem"), h("h3", {}, x.title)),
      h("p", { class: "muted", style: "margin:8px 0 0" }, x.message),
      x.action === "port_guide" ? h("button", { class: "btn small", style: "margin-top:10px", onclick: (e) => showGuide(e.currentTarget) }, "View Port Forwarding Guide") : null,
      x.action === "install_vc" ? h("button", { class: "btn small", style: "margin-top:10px",
        onclick: (e) => act(e.currentTarget, () => api(sp("/install-vc-runtime"), {})) }, "Install Visual C++ Runtime") : null,
      x.detail ? h("details", { class: "dim", style: "margin-top:6px" }, h("summary", {}, "Details"), x.detail) : null)));
  };
  put(root,
    h("h2", { class: "panel-title" }, "Diagnostics"),
    h("div", { class: "card" }, h("p", { class: "muted", style: "margin:0 0 12px" },
      "Checks the install, ports, firewall, router forwarding and more, and explains anything that would stop friends connecting."),
      h("div", { class: "row" }, h("button", { class: "btn primary", onclick: (e) => run(e.currentTarget) }, "Run Diagnostics")),
      summary),
    results);
}

// ----------------------------------------------------------- App Settings
// Same sections, in the same groups, as App Settings in the app.
const APP_SECTIONS = [
  ["Running", [["startup", "Startup"], ["keep", "Keep Running"]]],
  ["Remote Access", [["web", "Web Version"], ["ddns", "Dynamic DNS"]]],
  ["Integrations", [["alerts", "Alerts"], ["workshop", "Steam Workshop"]]],
  ["ConanOps", [["appearance", "Appearance"], ["lock", "PIN Lock"], ["updates", "Updates"], ["delete", "Delete ConanOps"]]],
];

async function viewApp(root) {
  const keys = APP_SECTIONS.flatMap(([, items]) => items.map(([k]) => k));
  const active = keys.includes(state.sub) ? state.sub : "startup";
  const { el, panel, label } = sectioned(APP_SECTIONS, active, (k) => go("app", k));
  put(root, el);
  const paint = async () => {
    const d = await api("/api/app");
    const refresh = () => setTimeout(() => paint().catch(() => {}), 1200);
    const toggle = (key, text, help) => h("div", { class: "field" },
      h("div", { class: "top" }, h("label", { for: "a_" + key }, text),
        h("input", { id: "a_" + key, type: "checkbox", class: "switch", checked: !!d[key],
          onchange: (e) => act(null, () => api("/api/app/" + key, { value: e.target.checked }), { after: refresh }) })),
      help ? h("div", { class: "help" }, help) : null);
    const textSetting = (key, text, value, opts = {}) => {
      const input = h("input", { type: opts.secret ? "password" : (opts.type || "text"), value: opts.secret ? "" : (value || ""),
        placeholder: opts.secret ? (value ? "Saved -- type to replace it" : "Not set") : (opts.placeholder || ""),
        autocomplete: opts.secret ? "new-password" : "off",
        oninput: () => { input.dataset.dirty = "1"; } });
      return h("form", { class: "field", onsubmit: (e) => { e.preventDefault();
          act(e.submitter, () => api("/api/app/" + key, { value: input.value }),
            { after: (r) => { if (r && r.ok !== false) delete input.dataset.dirty; refresh(); } }); } },
        h("label", {}, text),
        h("div", { class: "row" }, h("div", { style: "flex:1;min-width:160px" }, input),
          h("button", { class: "btn small", type: "submit" }, "Save"),
          opts.secret && value ? h("button", { class: "btn small", type: "button", onclick: (e) =>
            act(e.currentTarget, () => api("/api/app/" + key, { value: "" }), { after: refresh }) }, "Remove") : null),
        opts.help ? h("div", { class: "help" }, opts.help) : null);
    };
    const onPc = (text) => h("div", { class: "card" }, h("p", { class: "muted", style: "margin:0" }, text));
    const copyRow = (url) => h("div", { class: "row" }, h("span", { class: "mono", style: "word-break:break-all;flex:1" }, url),
      h("button", { class: "btn small", type: "button", onclick: () => navigator.clipboard &&
        navigator.clipboard.writeText(url).then(() => toast("Copied.")) }, icon("copy"), "Copy"));
    const sections = {
      startup: () => [h("div", { class: "card" },
        toggle("start_with_windows", "Start ConanOps when I sign into Windows"),
        toggle("start_minimized", "Start minimized to the tray"))],
      keep: () => {
        const markDirty = (e) => { e.target.dataset.dirty = "1"; };
        const winStart = h("input", { type: "time", value: d.update_window[0], style: "width:auto;flex:1 1 130px;min-width:130px", "aria-label": "From", oninput: markDirty });
        const winEnd = h("input", { type: "time", value: d.update_window[1], style: "width:auto;flex:1 1 130px;min-width:130px", "aria-label": "To", oninput: markDirty });
        return [h("div", { class: "card" },
          toggle("keep_alive", "Reopen ConanOps if it closes or crashes",
            "A small Windows task checks every minute and reopens ConanOps (quietly, in the tray) if it stopped."),
          h("div", { class: "field" }, h("div", { class: "top" }, h("label", {}, "Run with admin rights"),
            h("span", { class: "pill " + (d.admin ? "on" : "") }, d.admin ? "On" : d.admin_mode ? "On after restart" : "Off")),
            h("div", { class: "help" }, d.admin
              ? "Changes that need Windows' permission work from here too."
              : "Turn this on in App Settings → Keep Running on the PC (Windows asks once). Until then, firewall " +
                "changes and Windows update hours wait for someone at the PC.")),
          toggle("keep_awake", "Keep the PC awake while a server is running"),
          toggle("update_restarts", "Only restart for Windows updates during this window",
            d.admin ? null : "Changing this needs Windows' permission, given at the PC."),
          h("form", { class: "row", style: "margin-top:6px", onsubmit: (e) => { e.preventDefault();
              act(e.submitter, () => api("/api/app/update_window", { value: [winStart.value, winEnd.value] }), { after: refresh }); } },
            h("span", { class: "dim" }, "From"), winStart, h("span", { class: "dim" }, "to"), winEnd,
            h("button", { class: "btn small", type: "submit" }, "Save")),
          d.sign_in_note ? notice(d.sign_in_note) : null,
          h("div", { class: "field" }, h("label", { for: "mrm" }, "If a mod stops a server from starting"),
            h("select", { id: "mrm", onchange: (e) => act(null, () => api("/api/app/mod_recovery_mode", { value: e.target.value })) },
              [["wait", "Find it, keep the server stopped and wait for a fix"], ["start_without", "Find it and start without it (its items are removed)"],
               ["alert", "Just tell me"]].map(([v, l]) => h("option", { value: v, selected: d.mod_recovery_mode === v }, l)))))];
      },
      web: () => [h("div", { class: "card stack" },
        h("div", { class: "field" }, h("label", {}, "On your Wi-Fi"), d.web_lan_url ? copyRow(d.web_lan_url) : h("div", { class: "dim" }, "—")),
        h("div", { class: "field" }, h("label", {}, "From anywhere"),
          d.web_remote_on ? (d.web_remote_url ? copyRow(d.web_remote_url) : h("div", { class: "dim" }, d.web_remote_status || "Connecting…"))
            : h("div", { class: "dim" }, "Off -- turn it on in App Settings → Web Version on the PC.")),
        h("div", { class: "help" }, `Signed-in devices: ${d.web_sessions}. The password, "Sign Everyone Out" and the ` +
          "from-anywhere switch are on the PC, so nobody can lock you out from here."),
        h("button", { class: "btn", type: "button", onclick: logout }, icon("logout"), "Sign out of this device"))],
      ddns: () => [h("div", { class: "card" },
        h("p", { class: "muted", style: "margin:0 0 10px" }, "Gives your server an address that follows your home's " +
          "internet address when it changes. Free at duckdns.org -- sign in, add a domain, and copy your token."),
        textSetting("duckdns_domain", "Domain (without .duckdns.org)", d.duckdns_domain, { placeholder: "yourdomain" }),
        textSetting("duckdns_token", "Token", d.duckdns_token_set, { secret: true }))],
      alerts: () => {
        const testRow = (kind, input) => {
          const result = h("span", { class: "dim" });
          return h("div", { class: "row", style: "margin-top:4px" },
            h("button", { class: "btn small", type: "button", onclick: async (e) => {
              result.textContent = "";
              const r = await act(e.currentTarget, () => api("/api/alerts/test", { kind, url: input().value }));
              if (r) { result.textContent = r.message; result.className = r.ok ? "ok-text" : "bad-text"; }
            } }, "Send Test"), result);
        };
        const linkSetting = (key, text, isSet, placeholder) => {
          const input = h("input", { type: "password", value: "", autocomplete: "new-password",
            placeholder: isSet ? "Saved -- type to replace it" : placeholder,
            oninput: () => { input.dataset.dirty = "1"; } });
          const form = h("form", { class: "field", onsubmit: (e) => { e.preventDefault();
              act(e.submitter, () => api("/api/app/" + key, { value: input.value }),
                { after: (r) => { if (r && r.ok !== false) { delete input.dataset.dirty; input.value = ""; } refresh(); } }); } },
            h("label", {}, text),
            h("div", { class: "row" }, h("div", { style: "flex:1;min-width:160px" }, input),
              h("button", { class: "btn small", type: "submit" }, "Save"),
              isSet ? h("button", { class: "btn small", type: "button", onclick: (e) =>
                act(e.currentTarget, () => api("/api/app/" + key, { value: "" }), { after: refresh }) }, "Remove") : null));
          form.input = input;
          return form;
        };
        const discord = linkSetting("alert_discord_url", "Discord webhook link", d.alert_discord_set, "https://discord.com/api/webhooks/...");
        const ntfy = linkSetting("alert_ntfy_url", "ntfy topic link (phone notifications)", d.alert_ntfy_set, "https://ntfy.sh/your-topic-name");
        return [h("div", { class: "card" },
          h("p", { class: "muted", style: "margin:0 0 10px" }, "Get told when a server crashes, updates, finds a broken " +
            "mod, a backup fails and more -- in a Discord channel, as phone notifications through ntfy, or both. " +
            "These are for all your servers; every alert says which server it's about."),
          discord, testRow("discord", () => discord.input),
          toggle("discord_status_enabled", "Also keep a live status message for each server in that channel",
            "Updates every ~5 minutes instead of posting new messages."),
          foldOutGuide(d.alert_guides.discord)),
          h("div", { class: "card" }, ntfy, testRow("ntfy", () => ntfy.input), foldOutGuide(d.alert_guides.ntfy))];
      },
      workshop: () => [h("div", { class: "card" },
        textSetting("steam_api_key", "Steam Web API key", d.steam_api_key_set, { secret: true,
          help: "Lets ConanOps search the Workshop and see which mods need other mods." }),
        textSetting("workshop_update_cutoff", "Mods count as updated for the current patch if updated on or after",
          d.workshop_update_cutoff, { type: "date" }))],
      appearance: () => [onPc("The web version uses the same colors as the app. Change the theme in App Settings → " +
        "Appearance on the PC.")],
      lock: () => [onPc("The PIN lock protects the app on the PC itself, so it's set there (App Settings → PIN Lock). " +
        "The web version has its own password.")],
      updates: () => [h("div", { class: "card" },
        h("h3", {}, `ConanOps ${d.version}`),
        d.app_update_status ? h("p", { class: "muted", style: "margin:8px 0" }, d.app_update_status) : null,
        h("div", { class: "row" },
          h("button", { class: "btn", disabled: d.app_update_busy, onclick: (e) => act(e.currentTarget, () => api("/api/app/check_app_update", {}),
            { after: () => setTimeout(() => paint().catch(() => {}), 4000) }) }, "Check for Updates"),
          d.app_update_available ? h("button", { class: "btn primary", disabled: d.app_update_busy, onclick: async (e) => {
            const b = e.currentTarget;
            if (await confirmBox("Update ConanOps?", "ConanOps restarts itself on the PC to finish; your servers keep " +
              "running. This page reconnects in a minute.", "Update")) act(b, () => api("/api/app/install_app_update", {}));
          } }, "Install Update") : null),
        toggle("auto_check_app_updates", "Check for updates automatically"),
        toggle("auto_install_app_updates", "Install updates automatically"))],
      delete: () => [onPc("Deleting ConanOps or your servers can only be done on the PC, so nobody can do it from the web.")],
    };
    put(panel,
      h("h2", { class: "panel-title" }, label),
      d.needs_pc && d.needs_pc.length ? notice("Waiting for someone at the PC (Windows needs permission there): " +
        d.needs_pc.join("; ")) : null,
      ...sections[active]());
  };
  await paint();
  poll(panel, paint, 10000);
}

async function viewMore(root) {
  put(root, h("div", { class: "more-grid" }, MORE_NAV.map(([k, label]) =>
    h("button", { class: "btn", onclick: () => go(k) }, icon(k), label))),
    h("button", { class: "btn", style: "margin-top:12px;width:100%", onclick: logout }, icon("logout"), "Sign out"));
}

const VIEWS = { dashboard: viewDashboard, players: viewPlayers, mods: viewMods, updates: viewUpdates,
  access: viewAccess, console: viewConsole, settings: viewSettings, app: viewApp, more: viewMore };

// -------------------------------------------------------------------- boot
async function start() {
  try {
    const s = await api("/api/session");
    state.remote = !!s.remote;
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
