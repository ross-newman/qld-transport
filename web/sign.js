"use strict";

const TZ = "Australia/Brisbane";
const clockEl = document.getElementById("clock");
const statusEl = document.getElementById("status");
const stage = document.getElementById("stage");
const stationInput = document.getElementById("station-input");
const stationMenu = document.getElementById("station-menu");
const platformPicker = document.getElementById("platform-picker");
const platformButton = document.getElementById("platform-button");
const platformMenu = document.getElementById("platform-menu");
const messageEl = document.getElementById("message");
const messageText = messageEl.firstElementChild;
const noServicesEl = document.getElementById("no-services");
const ngrTemplate = document.getElementById("ngr-icon");
const threeCarTemplate = document.getElementById("three-car-icon");
const airportTemplate = document.getElementById("airport-icon");
const routeEl = document.getElementById("route");
const ROUTE_SWITCH_MS = 20000; // platform boards alternate between the list and the next train's stops
const WIPE_MS = 2000;
let routeView = false;
let viewSince = Date.now();
const MESSAGE_SPEED = 160; // px per second, in the 1920px design canvas
let messageShown = "";

let board = null;
let fetchOk = false;
let stationId = new URLSearchParams(location.search).get("station"); // null = server default
let platform = new URLSearchParams(location.search).get("platform"); // null = all platforms
const stationIds = new Map(); // lower-case station name -> id

const clockFmt = new Intl.DateTimeFormat("en-AU", {
  timeZone: TZ, hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true,
});

function fitStage() {
  const s = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
  stage.style.transform = `scale(${s})`;
  stage.style.left = `${(window.innerWidth - 1920 * s) / 2}px`;
  stage.style.top = `${(window.innerHeight - 1080 * s) / 2}px`;
}

function tickClock() {
  // "5:30:39 pm" -> "5:30:39"
  const text = clockFmt.format(new Date()).replace(/\s?[ap]m$/i, "");
  clockEl.textContent = text;
  // The outgoing screen shown during a wipe keeps ticking too.
  for (const el of document.querySelectorAll(".wipe [data-clock]")) el.textContent = text;
}

function minutesText(departsEpoch) {
  const mins = Math.floor((departsEpoch - Date.now() / 1000) / 60);
  return mins <= 0 ? "Now" : `${mins} min`;
}

function renderSection(id, rows) {
  const container = document.querySelector(`#${id} .rows`);
  container.replaceChildren();
  for (const r of rows) {
    const el = document.createElement("div");
    el.className = "row";
    el.style.background = r.color;
    el.style.setProperty("--row", r.color);
    el.dataset.departs = r.departs_epoch;
    for (const [cls, text] of [
      ["time", r.scheduled],
      ["dest", r.destination],
      ["exp", r.express ? "Exp" : ""],
      ["platform", r.platform],
      ["minutes", minutesText(r.departs_epoch)],
    ]) {
      const span = document.createElement("span");
      span.className = cls;
      span.textContent = text;
      if (cls === "dest" && r.destination.endsWith("Airport")) {
        span.appendChild(airportTemplate.content.firstElementChild.cloneNode(true));
      }
      el.appendChild(span);
      // NGR pictogram sits between the Platform and Departs columns.
      if (cls === "platform") {
        const stockTemplate = r.three_car ? threeCarTemplate : r.ngr ? ngrTemplate : null;
        if (stockTemplate) {
          el.appendChild(stockTemplate.content.firstElementChild.cloneNode(true));
        }
      }
    }
    container.appendChild(el);
  }
}

function stockIcon(r) {
  const template = r.three_car ? threeCarTemplate : r.ngr ? ngrTemplate : null;
  return template ? template.content.firstElementChild.cloneNode(true) : null;
}

function renderRoute(r) {
  const body = routeEl.querySelector(".route-body");
  body.style.background = r.color;
  body.style.setProperty("--row", r.color);
  const row = routeEl.querySelector(".route-row");
  row.dataset.departs = r.departs_epoch;
  const svc = document.createElement("span");
  svc.className = "svc";
  svc.textContent = `${r.scheduled} to ${r.destination}`;
  const minutes = document.createElement("span");
  minutes.className = "minutes";
  minutes.textContent = minutesText(r.departs_epoch);
  row.replaceChildren(svc, ...[stockIcon(r)].filter(Boolean), minutes);

  const stops = routeEl.querySelector(".stops");
  stops.replaceChildren(...r.stops.map((s) => {
    const div = document.createElement("div");
    div.className = "stop";
    div.append(s.name);
    if (s.express) {
      const note = document.createElement("span");
      note.className = "exp-note";
      note.textContent = "express";
      div.appendChild(note);
    }
    return div;
  }));
  layoutStops(stops, r.stops);
  layoutLogo(body, stops, r);
}

const ROUTE_BODY_HEIGHT = 790;
const ROUTE_ROW_HEIGHT = 115;   // service row including its border
const LOGO_MAX_HEIGHT = 300;    // a bit under one third of the 1080px screen
const LOGO_MIN_HEIGHT = 120;
const LOGO_MARGIN = 12;         // gap above the logo, below the stops
const LOGO_BOTTOM = 60;         // gap under the logo (keep in step with the CSS)

// Airtrain logo for services to the Airport, as large as the space below the stops allows.
function layoutLogo(body, stops, r) {
  const logo = body.querySelector(".airtrain-logo");
  const space = ROUTE_BODY_HEIGHT - ROUTE_ROW_HEIGHT - stops.offsetHeight - LOGO_MARGIN - LOGO_BOTTOM;
  const height = Math.min(LOGO_MAX_HEIGHT, space);
  logo.hidden = !r.destination.endsWith("Airport") || height < LOGO_MIN_HEIGHT;
  if (!logo.hidden) logo.style.height = `${height}px`;
}

const STOPS_WIDTH = 1832;   // usable width of the stops block in the 1920px canvas
const STOPS_HEIGHT = 620;   // usable height below the service row
const STOPS_GAP = 40;
const STOPS_MAX_LINE = 80;  // tallest line, used when there are few stops
const STOPS_PREFERRED_LINE = 62;  // two columns are used only if they are at least this large
const STOPS_FONT_RATIO = 0.76;
const measureCtx = document.createElement("canvas").getContext("2d");

// Two columns with bigger text when the stops fit that way, otherwise three columns.
function layoutStops(stops, list) {
  measureCtx.font = `700 100px ${getComputedStyle(stops).fontFamily}`;
  const widest = Math.max(1, ...list.map((s) => measureCtx.measureText(s.name).width)) / 100;
  const lines = list.length + list.filter((s) => s.express).length || 1;
  const fit = (cols) => {
    const colWidth = (STOPS_WIDTH - STOPS_GAP * (cols - 1)) / cols;
    return Math.min(
      STOPS_MAX_LINE,
      STOPS_HEIGHT / Math.ceil(lines / cols),
      colWidth / widest / STOPS_FONT_RATIO,
    );
  };
  const twoCols = fit(2) >= STOPS_PREFERRED_LINE;
  const cols = twoCols ? 2 : 3;
  let lineHeight = Math.floor(fit(cols));
  stops.style.columnCount = cols;
  for (;;) {
    stops.style.fontSize = `${lineHeight * STOPS_FONT_RATIO}px`;
    stops.style.lineHeight = `${lineHeight}px`;
    // Columns that do not balance evenly (a two-line stop kept together) can still overflow.
    if (stops.offsetHeight - 36 <= STOPS_HEIGHT || lineHeight <= 24) break;
    lineHeight -= 2;
  }
}

function renderStatus() {
  statusEl.replaceChildren();
  const left = document.createElement("span");
  if (board && board.show_attribution) {
    left.textContent = "Data: TransLink (State of Queensland) GTFS / GTFS Realtime, CC BY 4.0";
  }
  const right = document.createElement("span");
  if (!fetchOk) {
    right.className = "warn";
    right.textContent = "Server unreachable - showing last known data";
  } else if (board && board.realtime_stale) {
    right.className = "warn";
    right.textContent = "Realtime feed unavailable - showing timetable";
  }
  statusEl.append(left, right);
}

function setMessage(text) {
  if (text === messageShown) return;  // keep the marquee running between refreshes
  messageShown = text;
  messageText.classList.remove("scroll");
  messageText.replaceChildren();
  if (!text) return;
  // Repeat the text back to back until it covers the bar plus one spare copy, so the loop is
  // seamless and the bar never runs empty, however short the message is.
  const addItem = () => {
    const item = document.createElement("span");
    item.className = "item";
    item.textContent = text;
    messageText.appendChild(item);
    return item;
  };
  const first = addItem();
  const width = first.offsetWidth;  // one copy, including its gap (layout px, unaffected by scaling)
  if (!width) return;
  const needed = Math.ceil(messageEl.clientWidth / width) + 1;
  for (let i = 1; i < needed; i++) addItem();
  messageText.style.setProperty("--to", `-${width}px`);
  messageText.style.setProperty("--dur", `${width / MESSAGE_SPEED}s`);  // constant speed, no pause
  messageText.classList.add("scroll");
}

function render() {
  messageEl.hidden = false;  // must be laid out for setMessage to measure it
  setMessage(board ? board.message : "");
  renderSection("inbound", board ? board.inbound : []);
  renderSection("outbound", board ? board.outbound : []);
  // The stops page follows the next train calling at the chosen platform, or else the next
  // train in the list (a rarely used platform may have none in the coming hours).
  const routeTrain = board && board.platform
    ? board.inbound.find((r) => r.platform === board.platform) ?? board.inbound[0] : null;
  const showRoute = Boolean(routeTrain && routeView);
  routeEl.hidden = !showRoute;
  document.getElementById("route-departs").hidden = !showRoute;
  stage.classList.toggle("route-view", showRoute);
  if (showRoute && !wiping) renderRoute(routeTrain);  // stops stay as they were when the wipe began
  if (board) {
    document.getElementById("title").textContent = showRoute ? board.route_title : board.title;
    document.getElementById("inbound").hidden = showRoute;
    document.getElementById("outbound").hidden = showRoute || board.single_section;
    document.querySelector("#inbound .name").textContent = board.inbound_heading;
    document.querySelector("#outbound .name").textContent = board.outbound_heading;
    document.title = `${board.station} Station`;
    if (document.activeElement !== stationInput) stationInput.value = board.station;
    renderPlatformPicker();
    // No upcoming services: replace the lists with a message in the middle of the screen.
    const none = !showRoute && board.inbound.length === 0 && board.outbound.length === 0;
    noServicesEl.hidden = !none;
    if (none) {
      document.getElementById("inbound").hidden = true;
      document.getElementById("outbound").hidden = true;
    }
    messageEl.hidden = none;
  }
  renderStatus();
}

// Countdowns follow the local clock between fetches.
function updateCountdowns() {
  // Includes the copies of the outgoing screen shown during a wipe.
  for (const el of document.querySelectorAll(".row, .route-row")) {
    const minutes = el.querySelector(".minutes");
    if (minutes && el.dataset.departs) minutes.textContent = minutesText(Number(el.dataset.departs));
  }
}

async function load() {
  const requested = stationId;
  const requestedPlatform = platform;
  try {
    const params = new URLSearchParams();
    if (requested) params.set("station", requested);
    if (requestedPlatform) params.set("platform", requestedPlatform);
    const query = params.size ? `?${params}` : "";
    const res = await fetch(`/api/departures${query}`, { cache: "no-store" });
    if (res.status === 404 && requested) {  // unknown station: fall back to the default
      stationId = null;
      platform = null;
      return load();
    }
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    if (requested !== stationId || requestedPlatform !== platform) return;  // the user picked another meanwhile
    board = data;
    stationId = board.station_id;
    platform = board.platform;  // null if the platform does not exist at this station
    fetchOk = true;
  } catch {
    fetchOk = false;
  }
  render();
}

function applyStationInput(revertIfUnknown) {
  const id = stationIds.get(stationInput.value.trim().toLowerCase());
  if (!id) {
    if (revertIfUnknown) stationInput.value = board ? board.station : "";
    return;
  }
  if (id === stationId) return;
  stationId = id;
  platform = null;
  resetRouteView();
  board = null;
  render();
  updateUrl();
  load();
}

// A copy of the outgoing screen is laid over the stage and shrunk away from the top by a thin
// yellow line, so the incoming screen (rendered underneath) is revealed as the line moves down.
let wiping = false;
let wipeLayer = null;

function cancelWipe() {
  wipeLayer?.remove();
  wipeLayer = null;
  wiping = false;
}

function wipeTo(change) {
  const layer = document.createElement("div");
  layer.className = "wipe";
  for (const child of stage.children) {
    if (child.classList.contains("wipe")) continue;
    const copy = child.cloneNode(true);
    copy.querySelector("#pickers")?.remove();
    copy.querySelector("#clock")?.setAttribute("data-clock", "");
    layer.appendChild(copy);
  }
  // The copy keeps its ids: most of the layout rules are keyed by id (#clock, #title, #route,
  // #message...), so without them the outgoing screen would reflow as the wipe starts. The
  // originals come first in the document, so getElementById/querySelector still find them.
  const line = document.createElement("div");
  line.className = "wipe-line";
  layer.appendChild(line);
  stage.appendChild(layer);
  change();
  wiping = true;
  wipeLayer = layer;
  const done = () => {
    if (wipeLayer !== layer) return;  // already finished or cancelled
    cancelWipe();
  };
  layer.addEventListener("animationend", (e) => { if (e.target === layer) done(); });
  setTimeout(done, WIPE_MS + 500);
}

function resetRouteView() {
  cancelWipe();
  routeView = false;
  viewSince = Date.now();
}

function updateUrl() {
  const url = new URL(location.href);
  url.searchParams.set("station", stationId);
  if (platform) url.searchParams.set("platform", platform); else url.searchParams.delete("platform");
  history.replaceState(null, "", url);
}

function renderPlatformPicker() {
  const platforms = board ? board.platforms : [];
  platformPicker.hidden = platforms.length === 0;
  platformButton.textContent = platform || "-";
  platformMenu.replaceChildren(...[null, ...platforms].map((p) => {
    const li = document.createElement("li");
    li.dataset.platform = p ?? "";
    li.textContent = p ?? "-";
    return li;
  }));
}

function choosePlatform(value) {
  platformMenu.hidden = true;
  platformButton.blur();
  const next = value || null;
  if (next === platform) return;
  platform = next;
  resetRouteView();
  updateUrl();
  load();
}

platformMenu.addEventListener("mousedown", (e) => e.preventDefault());
platformMenu.addEventListener("click", (e) => {
  if (e.target.tagName === "LI") choosePlatform(e.target.dataset.platform);
});
platformButton.addEventListener("click", () => { platformMenu.hidden = !platformMenu.hidden; });
platformButton.addEventListener("blur", () => { platformMenu.hidden = true; });
platformButton.addEventListener("keydown", (e) => { if (e.key === "Escape") platformButton.blur(); });

async function loadStations() {
  try {
    const res = await fetch("/api/stations");
    if (!res.ok) return;
    const data = await res.json();
    for (const s of data.stations) {
      stationIds.set(s.name.toLowerCase(), s.id);
      stationNames.push(s.name);
    }
  } catch {
    // the sign still works without the search box
  }
}

// The native datalist popup is misplaced inside the scaled stage, so the menu is drawn in the page.
const stationNames = [];
let menuItems = [];
let menuIndex = -1;

function showMenu() {
  const q = stationInput.value.trim().toLowerCase();
  menuItems = stationNames.filter((n) => n.toLowerCase().includes(q));
  menuIndex = -1;
  stationMenu.replaceChildren(...menuItems.map((name) => {
    const li = document.createElement("li");
    li.textContent = name;
    return li;
  }));
  stationMenu.hidden = menuItems.length === 0;
}

function highlight(i) {
  stationMenu.children[menuIndex]?.classList.remove("active");
  menuIndex = i;
  const li = stationMenu.children[i];
  li?.classList.add("active");
  li?.scrollIntoView({ block: "nearest" });
}

function chooseStation(name) {
  stationInput.value = name;
  stationInput.blur();
}

// Keep focus in the input while the menu is clicked so blur does not close it first.
stationMenu.addEventListener("mousedown", (e) => e.preventDefault());
stationMenu.addEventListener("click", (e) => {
  if (e.target.tagName === "LI") chooseStation(e.target.textContent);
});

stationInput.addEventListener("focus", () => { stationInput.value = ""; showMenu(); });
stationInput.addEventListener("blur", () => { stationMenu.hidden = true; applyStationInput(true); });
stationInput.addEventListener("input", showMenu);
stationInput.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const step = e.key === "ArrowDown" ? 1 : -1;
    highlight((menuIndex + step + menuItems.length) % menuItems.length);
  } else if (e.key === "Enter") {
    const typed = stationInput.value.trim().toLowerCase();
    const exact = menuItems.find((n) => n.toLowerCase() === typed);
    const pick = menuItems[menuIndex] ?? exact ?? menuItems[0];
    if (pick) chooseStation(pick); else stationInput.blur();
  } else if (e.key === "Escape") {
    stationInput.blur();
  }
});

// Refresh on every minute boundary (plus a small offset), i.e. 1-minute resolution.
function scheduleNext() {
  const msToNextMinute = 60000 - (Date.now() % 60000);
  setTimeout(async () => { await load(); scheduleNext(); }, msToNextMinute + 2000);
}

// The pickers fade out when the mouse stops moving, so the sign looks clean on a wall display.
const pickersEl = document.getElementById("pickers");
const IDLE_MS = 3000;
let idleTimer = null;
function wake() {
  pickersEl.classList.remove("idle");
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => pickersEl.classList.add("idle"), IDLE_MS);
}
window.addEventListener("mousemove", wake);
window.addEventListener("mousedown", wake);
wake();

window.addEventListener("resize", fitStage);
fitStage();
tickClock();
setInterval(() => {
  tickClock();
  updateCountdowns();
  if (board && board.platform && board.inbound.length
      && Date.now() - viewSince >= ROUTE_SWITCH_MS) {
    viewSince = Date.now() + WIPE_MS;
    wipeTo(() => {
      routeView = !routeView;
      render();
    });
  }
}, 1000);
load().then(scheduleNext);
loadStations();
