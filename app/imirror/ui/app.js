/* iMirror UI. State comes from Python (Core.state) via window.__imirror.push(); actions go
   through pywebview.api.call(name, args). Without pywebview (opening the file in a browser)
   a demo backend is used: ?demo=off|wait|live|cap */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let S = null;               // last state
let api = null;             // backend
let lastToast = 0;
let editing = null;         // element currently edited by the user (don't overwrite)

/* ------------------------------------------------------------------ backend */
function demoBackend() {
  const mode = new URLSearchParams(location.search).get("demo") || "wait";
  const st = {
    platform: "demo",
    cfg: { preset: "max4k", width: 3840, height: 2160, refresh: 120, max_fps: 120, h265: true, low_latency: true, audio: true,
      always_on_top: false, auto_fit: true, software_decode: false, server_name: "iMirror", pin: "", video_sink: "d3d12videosink",
      control_mode: "capture", sensitivity: 1, wheel_multiplier: 1, swap_ctrl_cmd: true, invert_wheel: false, release_hotkey: "rctrl",
      transport: "ble", serial_port: "", flash_chip: "esp32", reduce_motion: false },
    presets: { max4k: { title: "Максимум: 4K H.265, запрос 120 Гц" }, "1440p": { title: "1440p H.265, запрос 120 Гц" },
      "1080p": { title: "1080p H.264 (совместимость, старые iPhone)" }, custom: { title: "Свои значения" } },
    sinks: ["d3d12videosink", "d3d11videosink", "autovideosink"],
    hotkeys: { rctrl: "Правый Ctrl", ralt: "Правый Alt", scroll: "Scroll Lock", pause: "Pause/Break" },
    hotkey_name: "Правый Ctrl", chips: { esp32: "ESP32 (WROOM / DevKit V1)", esp32s3: "ESP32-S3", esp32c3: "ESP32-C3" },
    receiver: { running: mode !== "off", connections: mode === "live" || mode === "cap" ? 1 : 0, codec: "h265", width: 1179, height: 2556,
      fps: 59.8, peak: 60, mbps: 31.4, fps_hist: Array.from({ length: 60 }, (_, i) => 44 + 16 * Math.abs(Math.sin(i / 6))), window: true },
    mdns: { ok: mode !== "off" ? true : null, addresses: ["192.168.1.34"], error: "" },
    hid: { transport: "ble", ble_available: true, ready: true, phone: mode === "live" || mode === "cap", info: "Bluetooth компьютера", error: "", port: "" },
    control: { mode: "capture", captured: mode === "cap", calibrated: false, calib_active: false, calib_step: 0 },
    log: ["12:00:01 Приёмник запущен", "12:00:02 Объявлено в сети: 192.168.1.34"], toasts: [],
  };
  if (!(st.receiver.connections)) Object.assign(st.receiver, { codec: "", width: 0, height: 0, fps: 0, peak: 0, mbps: 0, fps_hist: Array(60).fill(0) });
  const adapters = [{ name: "Wi-Fi", ip: "192.168.1.34", virtual: false, reasons: [] },
    { name: "vEthernet (WSL)", ip: "172.27.80.1", virtual: true, reasons: ["виртуальный/VPN адаптер (wsl)"] },
    { name: "WireGuard Tunnel", ip: "10.8.0.2", virtual: true, reasons: ["виртуальный/VPN адаптер (wireguard)"] }];
  if (st.receiver.connections) setInterval(() => {
    if (!api || !api.isDemo) return;
    st.receiver.fps = +(52 + Math.random() * 8).toFixed(1);
    st.receiver.fps_hist = st.receiver.fps_hist.slice(1).concat(st.receiver.fps);
    st.receiver.mbps = +(28 + Math.random() * 6).toFixed(1); push(st);
  }, 1000);
  return {
    isDemo: true,
    state: async () => st,
    call: async (name, args = {}) => {
      if (name === "set") st.cfg[args.key] = args.value;
      if (name === "start") { st.receiver.running = true; st.mdns.ok = true; }
      if (name === "stop") { st.receiver.running = false; st.mdns.ok = null; }
      if (name === "adapters") return adapters;
      if (name === "ports") return [{ dev: "COM5", label: "COM5 — Silicon Labs CP210x", likely: true }];
      if (name === "transport") st.cfg.transport = st.hid.transport = args.transport;
      if (name === "action" || name === "media") st.toasts = [{ id: ++lastToast + 1000, text: "Отправлено на iPhone", kind: "ok" }];
      setTimeout(() => api && api.isDemo && push(st), 30); return { ok: true };
    },
    ready: async () => true, minimize() {}, toggle_maximize() {}, close() {},
  };
}

function connect() {
  if (window.pywebview && window.pywebview.api) {
    api = window.pywebview.api;
  } else {
    api = demoBackend();
  }
  api.state().then(push).then(() => api.ready());
}
window.addEventListener("pywebviewready", () => { if (!api || api.isDemo) connect(); });
// Outside the app (plain browser) fall back to the demo backend; the real bridge replaces it
// whenever pywebviewready fires, even if that happens later.
window.addEventListener("load", () => setTimeout(() => { if (!api) connect(); }, window.pywebview ? 3000 : 700));

function call(name, args) {
  return Promise.resolve(api.call(name, args || {})).then((r) => { if (r && r.ok === false && r.error) toast({ text: r.error, kind: "error" }); return r; });
}
const setCfg = (key, value) => call("set", { key, value });

/* ------------------------------------------------------------------ render */
function bind(name, value) {
  for (const el of $$(`[data-bind="${name}"]`)) {
    const v = String(value);
    if (el.textContent !== v) {
      el.textContent = v;
      if (el.closest(".m-value")) { el.closest(".m-value").classList.remove("bump"); void el.offsetWidth; el.closest(".m-value").classList.add("bump"); }
    }
  }
}

function deviceState(s) {
  if (!s.receiver.running) return "off";
  if (s.control.captured) return "cap";
  if (s.receiver.connections > 0 && s.receiver.width) return "live";
  return "wait";
}

function push(s) {
  const prev = S;
  S = s;
  window.__imirror.rendered = true;
  const st = deviceState(s);
  const dev = $("#device");
  if (dev.dataset.state !== st) {
    if (st === "live" && prev && deviceState(prev) === "wait") { const b = $(".burst"); b.classList.remove("go"); void b.offsetWidth; b.classList.add("go"); }
    dev.dataset.state = st;
    document.body.dataset.state = st;
  }
  document.body.dataset.transport = s.cfg.transport;
  document.body.classList.toggle("phone", !!s.hid.phone);
  document.body.classList.toggle("calm", !!s.cfg.reduce_motion);

  // pill
  const pill = $("#statusPill");
  const tone = { off: "", wait: "wait", live: "live", cap: "cap" }[st];
  pill.dataset.tone = s.mdns.ok === false ? "err" : tone;
  bind("pill", { off: "Приёмник выключен", wait: s.mdns.ok === false ? "Сеть: ошибка объявления" : "Ждём iPhone", live: "iPhone на экране", cap: "Управление на iPhone" }[st]);

  // cta
  const cta = $("#mainCta");
  cta.classList.toggle("stop", s.receiver.running);
  bind("cta", s.receiver.running ? "Остановить приёмник" : "Запустить приёмник");
  for (const b of $$(".mini-actions [data-act=fit], .mini-actions [data-act=fullscreen]")) b.disabled = !s.receiver.window;
  $(".mini-actions [data-act=capture]").disabled = !(s.hid.phone && s.receiver.window);

  // stream numbers
  const r = s.receiver;
  bind("name", s.cfg.server_name || "iMirror");
  bind("fps", r.connections ? Math.round(r.fps) : 0);
  bind("peak", Math.round(r.peak));
  bind("mbps", r.mbps.toFixed(1));
  bind("res", r.width ? `${r.width} × ${r.height}, ${r.codec.toUpperCase()}` : "—");
  bind("resBig", r.width ? `${r.width} × ${r.height}` : "—");
  bind("codec", r.codec ? `кодек ${r.codec.toUpperCase()}` : "кодек —");
  bind("requested", `запрошено ${s.cfg.width} × ${s.cfg.height}, ${s.cfg.refresh} Гц, до ${s.cfg.max_fps} к/с`);
  bind("hotkey", s.hotkey_name);
  drawSpark(r.fps_hist);

  // presets
  const pc = $("#presetChoice");
  if (!pc.children.length) {
    const names = { max4k: ["Максимум, 4K H.265", "Самое высокое, что умеет iPhone"], "1440p": ["1440p H.265", "Если Wi-Fi не тянет 4K"],
      "1080p": ["1080p H.264", "Для старых iPhone без H.265"], custom: ["Свои значения", "Задаются в «Настройках»"] };
    for (const [k, v] of Object.entries(s.presets)) {
      const b = document.createElement("button");
      b.dataset.preset = k;
      const [t, sub] = names[k] || [v.title, ""];
      b.innerHTML = "<b></b><em></em>"; b.firstChild.textContent = t; b.lastChild.textContent = sub;
      b.onclick = () => setCfg("preset", k);
      pc.appendChild(b);
    }
  }
  for (const b of $$("#presetChoice button")) b.classList.toggle("on", b.dataset.preset === s.cfg.preset);

  // settings fields
  for (const el of $$("[data-cfg]")) {
    if (el === editing) continue;
    const v = s.cfg[el.dataset.cfg];
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.tagName === "SELECT") { if (el.options.length && el.value !== String(v)) el.value = String(v); }
    else if (String(el.value) !== String(v ?? "")) el.value = v ?? "";
    if (el.type === "range") paintRange(el);
  }
  bind("sens", "×" + Number(s.cfg.sensitivity).toFixed(2));
  bind("wheel", "×" + Number(s.cfg.wheel_multiplier).toFixed(2));
  fillSelect($("#sinkSelect"), s.sinks.map((x) => [x, x]), s.cfg.video_sink);
  fillSelect($("#hotkeySelect"), Object.entries(s.hotkeys), s.cfg.release_hotkey);
  fillSelect($("#chipSelect"), Object.entries(s.chips), s.cfg.flash_chip);

  // control
  segSelect($("#transportSeg"), `[data-transport="${s.cfg.transport}"]`);
  for (const b of $$("#modeChoice button")) b.classList.toggle("on", b.dataset.mode === s.cfg.control_mode);
  const ble = s.cfg.transport === "ble";
  bind("chkReadyTitle", ble ? "Bluetooth компьютера" : "Плата ESP32");
  const ready = s.hid.ready, err = s.hid.error;
  bind("chkReady", err ? err : ready ? (ble ? "Готов, компьютер виден iPhone как клавиатура и мышь" : "Плата на связи") : (ble ? "Включаю Bluetooth…" : "Выберите порт и подключите плату"));
  setCheck("ready", ready ? "done" : err ? "bad" : "now");
  setCheck("phone", s.hid.phone ? "done" : ready ? "now" : "");
  setCheck("assist", s.hid.phone ? "now" : "");
  bind("calibState", s.control.calibrated ? (s.control.calib_fast ? "Выполнена, быстрый режим" : "Выполнена, точный режим") : "Не выполнена");
  $("#flashBtn").disabled = !!s.hid.flashing;
  $("#flashBtn").textContent = s.hid.flashing ? "Прошиваю…" : "Прошить плату";

  // calibration overlay
  const ov = $("#calibOverlay");
  ov.hidden = !s.control.calib_active;
  $$(".calib-dots i").forEach((d, i) => { d.classList.toggle("on", i + 1 === s.control.calib_step); d.classList.toggle("done", i + 1 < s.control.calib_step); });

  // network
  const an = $("#announce");
  an.dataset.ok = s.mdns.ok === true ? "1" : s.mdns.ok === false ? "0" : "";
  bind("mdnsTitle", s.mdns.ok === true ? "iMirror виден в сети" : s.mdns.ok === false ? "Объявление не удалось" : "Объявление не запущено");
  bind("mdnsSub", s.mdns.ok === true ? "Адреса: " + s.mdns.addresses.join(", ") : s.mdns.ok === false ? s.mdns.error : "Запустите приёмник");

  // log
  const log = $("#log");
  const text = s.log.join("\n");
  if (log.textContent !== text) { const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8; log.textContent = text; if (atEnd) log.scrollTop = log.scrollHeight; }

  // toasts
  for (const t of s.toasts) if (t.id > lastToast) { lastToast = t.id; if (prev) toast(t); }
  document.body.classList.remove("booting");
}

function setCheck(name, cls) { const li = $(`[data-check="${name}"]`); li.classList.remove("done", "now", "bad"); if (cls) li.classList.add(cls); }
function fillSelect(sel, pairs, value) {
  if (sel.options.length !== pairs.length) { sel.innerHTML = ""; for (const [v, t] of pairs) sel.add(new Option(t, v)); }
  if (sel !== editing) sel.value = value;
}
function paintRange(el) { const p = ((el.value - el.min) / (el.max - el.min)) * 100; el.style.setProperty("--p", p + "%"); }

/* sparkline */
function drawSpark(hist) {
  const c = $("#spark"); if (!c) return;
  const dpr = window.devicePixelRatio || 1, w = 180, h = 52;
  if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  const max = Math.max(60, ...hist);
  const pts = hist.map((v, i) => [i / (hist.length - 1) * w, h - 4 - (v / max) * (h - 8)]);
  const grad = g.createLinearGradient(0, 0, w, 0); grad.addColorStop(0, "rgba(159,214,255,.2)"); grad.addColorStop(1, "#9fd6ff");
  g.beginPath(); pts.forEach(([x, y], i) => i ? g.lineTo(x, y) : g.moveTo(x, y));
  g.strokeStyle = grad; g.lineWidth = 2; g.lineJoin = "round"; g.stroke();
  g.lineTo(w, h); g.lineTo(0, h); g.closePath();
  const fill = g.createLinearGradient(0, 0, 0, h); fill.addColorStop(0, "rgba(159,214,255,.25)"); fill.addColorStop(1, "rgba(159,214,255,0)");
  g.fillStyle = fill; g.fill();
  const [lx, ly] = pts[pts.length - 1]; g.beginPath(); g.arc(lx - 2, ly, 3, 0, 7); g.fillStyle = "#fff"; g.fill();
}

/* toasts */
function toast(t) {
  const el = document.createElement("div");
  el.className = "toast " + (t.kind || "");
  el.innerHTML = "<i></i><span></span>"; el.lastChild.textContent = t.text;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 500); }, t.kind === "error" ? 7000 : 4200);
}

/* ------------------------------------------------------------------ segmented controls with sliding thumb */
function segSelect(seg, selector) {
  const btn = $(selector, seg); if (!btn) return;
  $$("button", seg).forEach((b) => b.classList.toggle("on", b === btn));
  const th = $(".seg-thumb", seg);
  th.style.width = btn.offsetWidth + "px";
  th.style.transform = `translateX(${btn.offsetLeft}px)`;
}

/* navigation between pages */
let pageOrder = ["screen", "control", "network", "settings"];
function go(name) {
  const cur = $(".page.on");
  const from = pageOrder.indexOf(cur.dataset.page), to = pageOrder.indexOf(name);
  if (from === to) return;
  for (const p of $$(".page")) { p.classList.remove("on", "left"); if (pageOrder.indexOf(p.dataset.page) < to) p.classList.add("left"); }
  $(`.page[data-page="${name}"]`).classList.add("on");
  segSelect($("#nav"), `[data-nav="${name}"]`);
  if (name === "network") loadAdapters();
  if (name === "control" && S && S.cfg.transport === "esp32") loadPorts();
}

async function loadAdapters() {
  const list = await api.call("adapters", {});
  const box = $("#adapters"); box.innerHTML = "";
  const forced = S ? S.cfg.forced_ip : "";
  for (const a of list || []) {
    const used = forced ? a.ip === forced : !a.virtual;
    const el = document.createElement("div"); el.className = "adapter";
    el.innerHTML = `<div><b></b><em></em></div><span class="chip ${used ? "use" : "skip"}">${used ? "используется" : "пропущен"}</span><button class="btn"></button>`;
    el.querySelector("b").textContent = a.name;
    el.querySelector("em").textContent = a.ip + (a.reasons.length ? ", " + a.reasons.join(", ") : "");
    const b = el.querySelector("button");
    b.textContent = forced === a.ip ? "Все сети" : "Только эта";
    b.onclick = async () => { await setCfg("forced_ip", forced === a.ip ? "" : a.ip); loadAdapters(); };
    box.appendChild(el);
  }
  if (!box.children.length) box.innerHTML = '<p class="note">Сетевые адаптеры не найдены. Подключите компьютер к Wi-Fi или кабелю.</p>';
}
async function loadPorts() {
  const ports = await api.call("ports", {});
  const sel = $("#portSelect"); sel.innerHTML = "";
  const want = S ? S.cfg.serial_port : "";
  for (const p of ports || []) sel.add(new Option(p.label, p.dev, false, p.dev === want || (!want && p.likely)));
  if (!sel.options.length) sel.add(new Option("Плата не найдена: подключите её USB-кабелем с данными", ""));
}

/* ------------------------------------------------------------------ wire up */
window.__imirror = { push, rendered: false };

document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => document.body.classList.remove("booting"), 2500);   // never stay invisible
  $$("[data-win]").forEach((b) => b.onclick = () => api && api[b.dataset.win] && api[b.dataset.win]());
  $$("[data-nav]").forEach((b) => b.onclick = () => go(b.dataset.nav));
  $$("[data-act]").forEach((b) => b.onclick = () => call(b.dataset.act));
  $("#mainCta").onclick = async (e) => {
    const c = e.currentTarget; c.classList.add("busy");
    await call(S && S.receiver.running ? "stop" : "start"); setTimeout(() => c.classList.remove("busy"), 400);
  };
  $$("[data-transport]").forEach((b) => b.onclick = () => { call("transport", { transport: b.dataset.transport }); if (b.dataset.transport === "esp32") loadPorts(); });
  $$("[data-mode]").forEach((b) => b.onclick = () => setCfg("control_mode", b.dataset.mode));
  const fire = (b) => { b.classList.remove("fired"); void b.offsetWidth; b.classList.add("fired"); };
  $$("[data-iact]").forEach((b) => b.onclick = () => { fire(b); call("action", { name: b.dataset.iact }); });
  $$("[data-media]").forEach((b) => b.onclick = () => { fire(b); call("media", { name: b.dataset.media }); });
  $("#typeSend").onclick = () => { const t = $("#typeText"); if (t.value) call("type", { text: t.value }).then((r) => { if (r && r.ok) t.value = ""; }); };
  $("#typeClip").onclick = () => call("type", { clipboard: true });
  $("#calibBtn").onclick = () => call("calibrate");
  $("#calibCancel").onclick = () => call("calibrate_cancel");
  $("#portRefresh").onclick = loadPorts;
  $("#portConnect").onclick = () => call("transport", { transport: "esp32", port: $("#portSelect").value });
  $("#flashBtn").onclick = () => call("flash", { chip: $("#chipSelect").value, port: $("#portSelect").value || (S && S.cfg.serial_port) });

  for (const el of $$("[data-cfg]")) {
    const key = el.dataset.cfg;
    el.addEventListener("focus", () => editing = el);
    el.addEventListener("blur", () => { if (editing === el) editing = null; });
    if (el.type === "checkbox") el.addEventListener("change", () => setCfg(key, el.checked));
    else if (el.type === "range") {
      el.addEventListener("input", () => { paintRange(el); bind(key === "sensitivity" ? "sens" : "wheel", "×" + Number(el.value).toFixed(2)); });
      el.addEventListener("change", () => setCfg(key, Number(el.value)));
    } else if (el.tagName === "SELECT") el.addEventListener("change", () => setCfg(key, el.value));
    else el.addEventListener("change", () => {
      const v = el.type === "number" ? Number(el.value) : el.value.trim();
      setCfg(key, v);
      if (["width", "height", "refresh", "max_fps"].includes(key)) setCfg("preset", "custom");
    });
  }

  // device: tilt + glare follow the pointer (the one decorative interaction)
  const wrap = $("#deviceWrap"), dev = $("#device");
  document.addEventListener("pointermove", (e) => {
    if (document.body.classList.contains("calm")) return;
    const r = wrap.getBoundingClientRect();
    const nx = (e.clientX - (r.left + r.width / 2)) / (window.innerWidth / 2);
    const ny = (e.clientY - (r.top + r.height / 2)) / (window.innerHeight / 2);
    dev.style.setProperty("--ry", (nx * 9).toFixed(2) + "deg");
    dev.style.setProperty("--rx", (-ny * 7).toFixed(2) + "deg");
    dev.style.setProperty("--mx", (50 + nx * 45).toFixed(1) + "%");
    dev.style.setProperty("--my", (30 + ny * 40).toFixed(1) + "%");
  });
  window.addEventListener("resize", () => { segSelect($("#nav"), ".on"); if (S) segSelect($("#transportSeg"), `[data-transport="${S.cfg.transport}"]`); });
  requestAnimationFrame(() => segSelect($("#nav"), '[data-nav="screen"]'));
});
