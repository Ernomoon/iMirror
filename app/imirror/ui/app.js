/* iMirror UI. State from Python (Core.state) via window.__imirror.push(); actions via
   pywebview.api.call(name, args). Without pywebview (plain browser) a demo backend is used:
   index.html?demo=off|starting|ready|problem|connecting|live */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let S = null, api = null, lastToast = 0, editing = null;
const calm = () => document.body.classList.contains("calm") || matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ================================================================== demo backend */
function demoBackend() {
  const phase = new URLSearchParams(location.search).get("demo") || "ready";
  const live = phase === "live";
  const st = {
    platform: "demo", phase,
    cfg: { preset: "max4k", width: 3840, height: 2160, refresh: 120, max_fps: 120, h265: true, low_latency: true, audio: true,
      always_on_top: false, phone_frame: true, auto_fit: true, software_decode: false, server_name: "iMirror", pin: "", video_sink: "d3d12videosink",
      forced_ip: "", wifi_boost: true, reduce_motion: false, autostart: true },
    presets: { max4k: {}, "1440p": {}, "1080p": {}, custom: {} }, sinks: ["d3d12videosink", "d3d11videosink", "autovideosink"],
    receiver: { running: phase !== "off", connections: live ? 1 : 0, codec: live ? "h265" : "", width: live ? 1179 : 0, height: live ? 2556 : 0,
      fps: live ? 59.6 : 0, peak: live ? 60 : 0, mbps: live ? 31.2 : 0, window: live, since: live ? Date.now() / 1000 - 83 : 0,
      fps_hist: Array.from({ length: 60 }, (_, i) => live ? 42 + 17 * Math.abs(Math.sin(i / 5)) : 0) },
    mdns: { ok: ["ready", "live", "connecting"].includes(phase) ? true : phase === "problem" ? false : null, method: "bonjour", addresses: ["192.168.1.34"], error: "" },
    checks: [
      { id: "lan", title: "Подключение к сети", state: "ok", detail: "Wi-Fi: Беспроводная сеть, адрес 192.168.1.34" },
      { id: "vpn", title: "VPN на компьютере", state: "warn", detail: "Активен: AmneziaWG Tunnel. VPN с блокировкой локальной сети не пускает iPhone к компьютеру." },
      { id: "profile", title: "Тип сети Windows", state: phase === "problem" ? "bad" : "ok", detail: phase === "problem" ? "Сеть помечена как общедоступная" : "Частная сеть", fix: phase === "problem" ? "profile" : undefined, fix_title: "Сделать частной" },
      { id: "fw", title: "Брандмауэр Windows", state: "ok", detail: "iMirror разрешён" },
      { id: "bonjour", title: "Служба Bonjour", state: "ok", detail: "Работает: iMirror объявляется через неё" },
      { id: "mdns", title: "Виден в «Повторе экрана»", state: phase === "off" ? "info" : "ok", detail: "Объявлен через Bonjour: 192.168.1.34" },
    ],
    checking: false, log: ["12:00:01 Приёмник запущен", "12:00:02 Объявлено в сети встроенной службой Windows: 192.168.1.34"], toasts: [],
    fault: phase === "problem" ? { title: "Приёмник AirPlay не запускается", detail: "UxPlay завис при запуске даже в безопасном режиме. Последние строки его вывода ниже и в журнале.",
      tail: ["UxPlay 1.74: An Open-Source AirPlay mirroring and audio-streaming server.", "using network ports UDP 6000 6001 7011 TCP 7100 7000 7001", "Initialized server socket(s)"], action: "start" } : null,
    control: { available: true, status: new URLSearchParams(location.search).get("bt") || (live ? "connected" : "off"), error: "", adapter: "", captured: !!new URLSearchParams(location.search).get("cap"), typing: false },
  };
  setInterval(() => {
    if (!api || !api.isDemo || !live) return;
    st.receiver.fps = +(50 + Math.random() * 10).toFixed(1);
    st.receiver.fps_hist = st.receiver.fps_hist.slice(1).concat(st.receiver.fps);
    st.receiver.mbps = +(27 + Math.random() * 7).toFixed(1); push(st);
  }, 1000);
  const adapters = [{ name: "Беспроводная сеть", ip: "192.168.1.34", virtual: false, reasons: [], wifi: true },
    { name: "AmneziaWG", ip: "10.8.1.2", virtual: true, reasons: ["виртуальный адаптер или VPN (amnezia)"] }];
  return { isDemo: true, state: async () => st, ready: async () => true, minimize() {}, toggle_maximize() {}, close() {},
    call: async (n, a = {}) => {
      if (n === "set") st.cfg[a.key] = a.value;
      if (n === "adapters") return adapters;
      if (n === "start") { st.phase = "starting"; setTimeout(() => { st.phase = "ready"; push(st); }, 1500); }
      if (n === "stop") st.phase = "off";
      if (n === "control") { const c = st.control; if (a.op === "on") { c.status = "waiting"; setTimeout(() => { c.status = "connected"; push(st); }, 2000); }
        if (a.op === "off") c.status = "off"; if (a.op === "capture") c.captured = true; if (a.op === "release") c.captured = false; }
      st.receiver.running = st.phase !== "off";
      setTimeout(() => api && api.isDemo && push(st), 30);
      return { ok: true };
    } };
}
function connect() {
  api = (window.pywebview && window.pywebview.api) ? window.pywebview.api : demoBackend();
  Promise.resolve(api.state()).then(push).catch((e) => console.error("render", e))
    .finally(() => api.ready && api.ready());
}
window.addEventListener("pywebviewready", () => { if (!api || api.isDemo) connect(); });
window.addEventListener("load", () => setTimeout(() => { if (!api) connect(); }, window.pywebview ? 3000 : 600));
const call = (n, a) => Promise.resolve(api.call(n, a || {})).then((r) => { if (r && r.ok === false && r.error) toast({ text: r.error, kind: "error" }); return r; });
const setCfg = (key, value) => call("set", { key, value });

/* ================================================================== text */
const TEXT = {
  off:        { pill: "Выключен",        head: "Приёмник выключен",            sub: "Нажмите «Включить», и компьютер появится в «Повторе экрана» на iPhone.", cta: "Включить" },
  starting:   { pill: "Включаюсь",       head: "Включаюсь…",                   sub: "Запускаю приёмник и объявляю компьютер в сети. Обычно это 2–3 секунды.", cta: "Отменить" },
  ready:      { pill: "Ждёт iPhone",     head: "Готов. Подключите iPhone",      sub: "Три шага ниже. Компьютер объявлен в сети, iPhone должен его видеть.", cta: "Выключить" },
  problem:    { pill: "Есть проблема",   head: "iPhone может не увидеть компьютер", sub: "Ниже видно, что мешает, и есть кнопки исправления.", cta: "Выключить" },
  connecting: { pill: "Подключение",     head: "iPhone подключается…",          sub: "Сейчас появится окно с экраном iPhone.", cta: "Выключить" },
  live:       { pill: "На экране",       head: "Экран iPhone на компьютере",    sub: "Окно с картинкой открыто. Чтобы закончить, остановите повтор на iPhone.", cta: "Выключить" },
};

function setHeadline(text) {
  const h = $("#headline");
  if (h.dataset.text === text) return;
  h.dataset.text = text;
  h.textContent = "";
  let i = 0;
  for (const word of text.split(" ")) {
    if (i) { const sp = document.createElement("span"); sp.className = "sp"; h.appendChild(sp); }
    const w = document.createElement("span"); w.style.whiteSpace = "nowrap";
    for (const ch of word) {
      const s = document.createElement("span"); s.className = "ch"; s.textContent = ch;
      s.style.animationDelay = (calm() ? 0 : i * 22) + "ms"; w.appendChild(s); i++;
    }
    h.appendChild(w);
  }
}
function bind(name, v) { for (const el of $$(`[data-bind="${name}"]`)) if (el.textContent !== String(v)) el.textContent = v; }

/* animated numbers */
const tweens = new Map();
function num(name, target) {
  for (const el of $$(`[data-num="${name}"]`)) {
    const dec = +(el.dataset.dec || 0);
    const from = parseFloat(el.textContent) || 0;
    if (Math.abs(from - target) < Math.pow(10, -dec) / 2) continue;
    const t0 = performance.now(), dur = calm() ? 1 : 650;
    tweens.set(el, { from, target, t0, dur, dec });
  }
}
(function tick(now) {
  for (const [el, t] of tweens) {
    const k = Math.min(1, (now - t.t0) / t.dur), e = 1 - Math.pow(1 - k, 3);
    el.textContent = (t.from + (t.target - t.from) * e).toFixed(t.dec);
    if (k >= 1) tweens.delete(el);
  }
  requestAnimationFrame(tick);
})(0);

/* ================================================================== render */
function push(s) {
  const prev = S; S = s;
  window.__imirror.rendered = true;
  const ph = s.phase, T = TEXT[ph] || TEXT.off;
  if (document.body.dataset.phase !== ph) {
    if (ph === "live" && prev && prev.phase !== "live") celebrate();
    document.body.dataset.phase = ph;
    FX.phase(ph);
  }
  document.body.classList.toggle("has-window", !!s.receiver.window);
  document.body.classList.toggle("calm", !!s.cfg.reduce_motion);
  const fault = s.fault;
  bind("pill", T.pill); setHeadline(fault && ph === "problem" && !s.receiver.running ? "Приёмник остановился" : T.head); bind("sub", T.sub);
  bind("cta", ph === "problem" && !s.receiver.running ? "Включить снова" : T.cta);
  $("#cta").classList.toggle("stop", s.receiver.running);
  renderFault(fault);
  if (s.about) bind("version", s.about.version);
  renderControl(s);
  const r = s.receiver;
  bind("name", s.cfg.server_name || "iMirror");
  num("fps", r.connections ? r.fps : 0); num("peak", r.peak); num("mbps", r.mbps);
  bind("res", r.width ? `${r.width} × ${r.height}, ${r.codec.toUpperCase()}` : "");
  bind("resBig", r.width ? `${r.width} × ${r.height}` : "—");
  bind("codec", r.codec ? `${r.codec.toUpperCase()}, запрошено ${s.cfg.width}×${s.cfg.height}` : "");
  drawSpark(r.fps_hist);
  renderChecks(s);
  renderSettings(s);
  const log = $("#log"), text = s.log.join("\n");
  if (log.textContent !== text) { const end = log.scrollTop + log.clientHeight >= log.scrollHeight - 8; log.textContent = text; if (end) log.scrollTop = log.scrollHeight; }
  for (const t of s.toasts) if (t.id > lastToast) { lastToast = t.id; if (prev) toast(t); }
  document.body.classList.remove("booting");
}

function renderFault(f) {
  const box = $("#fault");
  box.hidden = !f;
  if (!f) return;
  bind("faultTitle", f.title); bind("faultDetail", f.detail);
  const tail = (f.tail || []).join("\n"), pre = $("#faultTail");
  if (pre.textContent !== tail) pre.textContent = tail;
  pre.hidden = !tail;
  $("#faultRetry").textContent = f.action === "reannounce" ? "Объявить снова" : "Попробовать снова";
  $("#faultRetry").dataset.action = f.action || "start";
}

const BT = {
  off:       ["Выключено", "Включите, и iPhone найдёт компьютер в Настройки → Bluetooth.", "Включить"],
  starting:  ["Включаю Bluetooth…", "Публикую мышь и клавиатуру.", "Выключить"],
  waiting:   ["Жду iPhone", "Откройте на iPhone Настройки → Bluetooth и выберите этот компьютер.", "Выключить"],
  connected: ["iPhone подключён", "Мышь, клавиатура и кнопки ниже управляют iPhone.", "Выключить"],
  error:     ["Bluetooth недоступен", "", "Повторить"],
};
let lastBt = "";
function renderControl(s) {
  const c = s.control || { available: false, status: "off" };
  const st = c.available ? c.status : "error";
  if (lastBt && st === "connected" && lastBt !== "connected") { const r = $("#device").getBoundingClientRect(); FX.pulse(r.left + r.width / 2, r.top + r.height / 2, true); toast({ text: "iPhone подключён по Bluetooth", kind: "ok" }); }
  lastBt = st;
  document.body.dataset.bt = st;
  document.body.classList.toggle("captured", !!c.captured);
  const T = BT[st] || BT.off;
  bind("btTitle", T[0]);
  bind("btDetail", st === "error" ? (c.available ? c.error : "Компоненты Bluetooth Windows не найдены. Переустановите iMirror") : T[1]);
  bind("btCta", T[2]);
  $("#btToggle").classList.toggle("stop", st !== "off" && st !== "error");
  $("#pad").classList.toggle("disabled", st !== "connected");
  bind("capTitle", c.captured ? "Мышь у iPhone" : "Взять мышь и клавиатуру");
  bind("capSub", c.captured ? "Нажмите правый Ctrl, чтобы вернуть" : "Правый Ctrl возвращает их компьютеру");
  $("#captureBtn").classList.toggle("on", !!c.captured);
  $("#typeBtn").textContent = c.typing ? "Печатаю…" : "Напечатать";
}

const ICON = { ok: '<svg viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7"/></svg>', bad: '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  warn: '<svg viewBox="0 0 16 16"><path d="M8 3.5v5.5M8 12.2v.3"/></svg>', info: '<svg viewBox="0 0 16 16"><path d="M8 7v5M8 4v.3"/></svg>' };
function renderChecks(s) {
  const box = $("#checks"), key = JSON.stringify(s.checks);
  $(".checks").classList.toggle("busy", !!s.checking);
  $("#recheck").classList.toggle("busy", !!s.checking);
  $("#recheck").textContent = s.checking ? "Проверяю…" : "Проверить снова";
  if (box.dataset.key === key) return;
  box.dataset.key = key; box.innerHTML = "";
  const order = { bad: 0, warn: 1, info: 2, ok: 3 };
  [...s.checks].sort((a, b) => order[a.state] - order[b.state]).forEach((c, i) => {
    const li = document.createElement("li"); li.className = "check"; li.dataset.s = c.state; li.style.animationDelay = i * 60 + "ms";
    li.innerHTML = `<span class="ic">${ICON[c.state] || ICON.info}</span><div><b></b><em></em></div>`;
    li.querySelector("b").textContent = c.title; li.querySelector("em").textContent = c.detail;
    if (c.fix) {
      const b = document.createElement("button"); b.className = "fix magnetic"; b.textContent = c.fix_title || "Исправить";
      b.onclick = (e) => { ripple(e); call(c.fix === "reannounce" ? "reannounce" : "fix", c.fix === "reannounce" ? {} : { fix: c.fix, arg: c.fix_arg || "" }); };
      li.appendChild(b);
    }
    box.appendChild(li);
  });
  if (!s.checks.length) box.innerHTML = '<li class="check" data-s="info"><span class="ic"></span><div><b>Проверяю сеть…</b></div></li>';
}

function renderSettings(s) {
  const pc = $("#presets");
  if (!pc.children.length) {
    const names = { max4k: ["Максимум", "4K, H.265. Самое высокое, что умеет iPhone"], "1440p": ["Высокое", "1440p, H.265. Если Wi-Fi не тянет 4K"],
      "1080p": ["Совместимое", "1080p, H.264. Для старых iPhone"], custom: ["Своё", "Значения из полей ниже"] };
    for (const k of Object.keys(s.presets)) {
      const b = document.createElement("button"); b.dataset.preset = k; b.className = "magnetic";
      b.innerHTML = "<b></b><em></em>"; b.firstChild.textContent = names[k][0]; b.lastChild.textContent = names[k][1];
      b.onclick = async (e) => { ripple(e); await setCfg("preset", k); restartIfRunning(); };
      pc.appendChild(b);
    }
  }
  $$("#presets button").forEach((b) => b.classList.toggle("on", b.dataset.preset === s.cfg.preset));
  const sel = $("#sinkSel");
  if (sel.options.length !== s.sinks.length) { sel.innerHTML = ""; s.sinks.forEach((x) => sel.add(new Option(x, x))); }
  for (const el of $$("[data-cfg]")) {
    if (el === editing) continue;
    const v = s.cfg[el.dataset.cfg];
    if (el.type === "checkbox") el.checked = !!v; else if (String(el.value) !== String(v ?? "")) el.value = v ?? "";
  }
}
function restartIfRunning() { if (S && S.receiver.running) call("restart"); }

async function loadAdapters() {
  const list = await api.call("adapters", {});
  const box = $("#adapters"); box.innerHTML = "";
  const forced = S ? S.cfg.forced_ip : "";
  (list || []).forEach((a, i) => {
    const used = forced ? a.ip === forced : !a.virtual;
    const el = document.createElement("div"); el.className = "adapter"; el.style.animationDelay = i * 60 + "ms";
    el.innerHTML = `<div><b></b><em></em></div><span class="chip ${used ? "use" : "skip"}">${used ? "объявляю здесь" : "пропускаю"}</span><button class="small-btn"></button>`;
    el.querySelector("b").textContent = a.name;
    el.querySelector("em").textContent = a.ip + (a.reasons && a.reasons.length ? ", " + a.reasons.join(", ") : "");
    const b = el.querySelector("button"); b.textContent = forced === a.ip ? "Автоматически" : "Только эта сеть";
    b.onclick = async () => { await setCfg("forced_ip", forced === a.ip ? "" : a.ip); loadAdapters(); };
    box.appendChild(el);
  });
}

/* ================================================================== sparkline */
function drawSpark(hist) {
  const c = $("#spark"); if (!c) return;
  const dpr = devicePixelRatio || 1, w = 190, h = 56;
  if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  const max = Math.max(60, ...hist), pts = hist.map((v, i) => [i / (hist.length - 1) * w, h - 5 - (v / max) * (h - 10)]);
  const gr = g.createLinearGradient(0, 0, w, 0); gr.addColorStop(0, "rgba(94,242,194,.1)"); gr.addColorStop(1, "#7ff7d3");
  g.beginPath(); pts.forEach(([x, y], i) => i ? g.lineTo(x, y) : g.moveTo(x, y));
  g.strokeStyle = gr; g.lineWidth = 2.2; g.lineJoin = "round"; g.shadowColor = "rgba(94,242,194,.8)"; g.shadowBlur = 10; g.stroke();
  g.shadowBlur = 0; g.lineTo(w, h); g.lineTo(0, h); g.closePath();
  const f = g.createLinearGradient(0, 0, 0, h); f.addColorStop(0, "rgba(94,242,194,.28)"); f.addColorStop(1, "rgba(94,242,194,0)"); g.fillStyle = f; g.fill();
  const [lx, ly] = pts[pts.length - 1]; g.beginPath(); g.arc(lx - 2, ly, 3.2, 0, 7); g.fillStyle = "#fff"; g.fill();
}

/* ================================================================== toasts, ripple, magnetic */
function toast(t) {
  const el = document.createElement("div"); el.className = "toast " + (t.kind || "");
  el.innerHTML = "<i></i><span></span>"; el.lastChild.textContent = t.text; $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 500); }, t.kind === "error" ? 7000 : 4200);
}
function ripple(e) {
  const b = e.currentTarget; if (!b || calm()) return;
  const r = b.getBoundingClientRect(), d = Math.max(r.width, r.height) * 2.2, s = document.createElement("span");
  s.className = "ripple"; s.style.width = s.style.height = d + "px";
  s.style.left = (e.clientX - r.left - d / 2) + "px"; s.style.top = (e.clientY - r.top - d / 2) + "px";
  if (getComputedStyle(b).position === "static") b.style.position = "relative";
  b.style.overflow = "hidden"; b.appendChild(s); setTimeout(() => s.remove(), 700);
}
document.addEventListener("pointermove", (e) => {
  for (const m of $$(".magnetic")) {
    const r = m.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const dx = e.clientX - cx, dy = e.clientY - cy, dist = Math.hypot(dx, dy), R = Math.max(r.width, r.height);
    const cl = (v) => Math.max(-6, Math.min(6, v));
    if (dist < R * .75 && !calm()) m.style.translate = `${cl(dx * .08)}px ${cl(dy * .12)}px`; else if (m.style.translate) m.style.translate = "";
  }
});

/* ================================================================== segmented nav */
function segSelect(seg, btn) {
  $$("button", seg).forEach((b) => b.classList.toggle("on", b === btn));
  const th = $(".thumb", seg); th.style.width = btn.offsetWidth + "px"; th.style.transform = `translateX(${btn.offsetLeft - 4}px)`;
}
function go(name) {
  const order = ["home", "control", "settings"], to = order.indexOf(name);
  for (const p of $$(".page")) { p.classList.remove("on", "left"); if (order.indexOf(p.dataset.page) < to) p.classList.add("left"); }
  $(`.page[data-page="${name}"]`).classList.add("on");
  segSelect($("#nav"), $(`[data-nav="${name}"]`));
  if (name === "settings") loadAdapters();
}

/* ================================================================== celebration */
function celebrate() {
  const f = $(".flash"); f.classList.remove("go"); void f.offsetWidth; f.classList.add("go");
  const r = $("#device").getBoundingClientRect();
  FX.burst(r.left + r.width / 2, r.top + r.height * 0.42);
}

/* ================================================================== sky + particles */
const FX = (() => {
  const sky = $("#sky"), fx = $("#fx"), sg = sky.getContext("2d"), fg = fx.getContext("2d");
  let W = 0, H = 0, dpr = 1, phase = "off", mx = 0.5, my = 0.5, t0 = performance.now();
  const blobs = [
    { x: .15, y: .1, r: .55, c: [70, 55, 190], sx: .07, sy: .05, p: 0 },
    { x: .85, y: .35, r: .45, c: [20, 110, 160], sx: .05, sy: .08, p: 2 },
    { x: .45, y: .95, r: .5, c: [120, 40, 130], sx: .09, sy: .04, p: 4 },
    { x: .7, y: .8, r: .35, c: [20, 160, 130], sx: .06, sy: .07, p: 1 },
  ];
  const parts = [], sparks = [], pulses = [];
  function resize() {
    dpr = Math.min(devicePixelRatio || 1, 2); W = innerWidth; H = innerHeight;
    for (const c of [sky, fx]) { c.width = W * dpr; c.height = H * dpr; }
    parts.length = 0;
    for (let i = 0; i < Math.round(W * H / 9000); i++) parts.push({ x: Math.random() * W, y: Math.random() * H, z: Math.random(), a: Math.random() * 6.28, s: .2 + Math.random() * .8 });
  }
  addEventListener("resize", resize); resize();
  addEventListener("pointermove", (e) => { mx = e.clientX / W; my = e.clientY / H; });
  function center() { const d = $("#device"); if (!d) return [W * .2, H * .5]; const r = d.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height * .45]; }
  function frame(now) {
    const t = (now - t0) / 1000, still = calm();
    sg.setTransform(dpr, 0, 0, dpr, 0, 0); sg.fillStyle = "#070a1c"; sg.fillRect(0, 0, W, H);
    sg.globalCompositeOperation = "lighter";
    for (const b of blobs) {
      const x = (b.x + Math.sin(t * b.sx + b.p) * .12 + (mx - .5) * .04) * W, y = (b.y + Math.cos(t * b.sy + b.p) * .1 + (my - .5) * .04) * H;
      let c = b.c;
      if (phase === "live" && b === blobs[3]) c = [30, 200, 150];
      if (phase === "problem" && b === blobs[0]) c = [160, 90, 30];
      const R = b.r * Math.max(W, H), g = sg.createRadialGradient(x, y, 0, x, y, R);
      g.addColorStop(0, `rgba(${c[0]},${c[1]},${c[2]},.55)`); g.addColorStop(1, "rgba(0,0,0,0)");
      sg.fillStyle = g; sg.fillRect(0, 0, W, H);
    }
    sg.globalCompositeOperation = "source-over";
    fg.setTransform(dpr, 0, 0, dpr, 0, 0); fg.clearRect(0, 0, W, H);
    const [cx, cy] = center();
    for (const p of parts) {
      if (!still) {
        if (phase === "ready" || phase === "connecting") {          // orbit the iPhone
          const dx = p.x - cx, dy = p.y - cy, d = Math.hypot(dx, dy) + 1;
          p.x += (-dy / d) * p.s * 1.1 - dx / d * (d > 330 ? .5 : -.05); p.y += (dx / d) * p.s * 1.1 - dy / d * (d > 330 ? .5 : -.05);
        } else if (phase === "live") {                               // stream from the iPhone to the right
          p.x += p.s * (2.2 + p.z * 2.5); p.y += Math.sin(t * 2 + p.a) * .4;
          if (p.x > W + 10) { p.x = cx + (Math.random() - .5) * 60; p.y = cy + (Math.random() - .5) * 220; }
        } else { p.y -= p.s * .25; p.x += Math.sin(t + p.a) * .15; }
        if (p.y < -10) p.y = H + 10; if (p.y > H + 10) p.y = -10; if (p.x < -10) p.x = W + 10;
      }
      const tw = .35 + .65 * Math.abs(Math.sin(t * 1.3 + p.a));
      if (phase === "live" && !still) {                               // warp streaks out of the iPhone
        const len = 6 + p.z * 26 * p.s, gl = fg.createLinearGradient(p.x - len, p.y, p.x, p.y);
        gl.addColorStop(0, "rgba(127,247,211,0)"); gl.addColorStop(1, `rgba(190,255,236,${.75 * tw})`);
        fg.strokeStyle = gl; fg.lineWidth = .6 + p.z * 1.5; fg.beginPath(); fg.moveTo(p.x - len, p.y); fg.lineTo(p.x, p.y); fg.stroke();
        continue;
      }
      fg.fillStyle = phase === "problem" ? `rgba(255,200,100,${.4 * tw})` : `rgba(190,220,255,${.45 * tw})`;
      fg.beginPath(); fg.arc(p.x, p.y, .6 + p.z * 1.4, 0, 6.29); fg.fill();
    }
    for (let i = pulses.length - 1; i >= 0; i--) {
      const q = pulses[i]; q.k += .022; if (q.k >= 1) { pulses.splice(i, 1); continue; }
      const e = 1 - Math.pow(1 - q.k, 3);
      fg.strokeStyle = `rgba(${q.c},${(1 - q.k) * .8})`; fg.lineWidth = 2 * (1 - q.k) + .5;
      fg.beginPath(); fg.arc(q.x, q.y, 6 + e * q.r, 0, 6.29); fg.stroke();
    }
    for (let i = sparks.length - 1; i >= 0; i--) {
      const s = sparks[i]; s.vx *= .985; s.vy = s.vy * .985 + .09; s.x += s.vx; s.y += s.vy; s.life -= .012; s.rot += s.vr;
      if (s.life <= 0) { sparks.splice(i, 1); continue; }
      fg.save(); fg.translate(s.x, s.y); fg.rotate(s.rot); fg.globalAlpha = Math.min(1, s.life * 1.5);
      fg.fillStyle = s.c; fg.fillRect(-s.w / 2, -s.h / 2, s.w, s.h); fg.restore();
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  return {
    phase(p) { phase = p; },
    pulse(x, y, big) {
      if (calm()) return;
      for (let i = 0; i < (big ? 3 : 2); i++) setTimeout(() => pulses.push({ x, y, k: 0, r: big ? 260 : 90, c: big ? "180,155,255" : "159,214,255" }), i * 120);
    },
    burst(x, y) {
      if (calm()) return;
      const cols = ["#9fd6ff", "#b49bff", "#ff8fc7", "#ffd08a", "#5ef2c2", "#ffffff"];
      for (let i = 0; i < 160; i++) {
        const a = Math.random() * 6.28, v = 3 + Math.random() * 9;
        sparks.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 4, life: 1 + Math.random() * .6, c: cols[i % cols.length],
          w: 4 + Math.random() * 6, h: 2 + Math.random() * 4, rot: Math.random() * 6, vr: (Math.random() - .5) * .3 });
      }
    },
  };
})();

/* ================================================================== wire up */
window.__imirror = { push, rendered: false };
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => document.body.classList.remove("booting"), 2500);
  $$("[data-win]").forEach((b) => b.onclick = () => {
    if (!api) return;
    api[b.dataset.win] && api[b.dataset.win]();
    if (b.dataset.win === "toggle_maximize") {
      const g = $("#maxGlyph"), max = g.dataset.max === "1"; g.dataset.max = max ? "0" : "1";
      g.innerHTML = max ? '<rect x=".5" y=".5" width="9" height="9" rx="1"/>' : '<rect x=".5" y="2.5" width="7" height="7" rx="1"/><path d="M2.5 2.5V1.5a1 1 0 0 1 1-1h5a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-1"/>';
    }
  });
  $$("[data-nav]").forEach((b) => b.onclick = () => go(b.dataset.nav));
  $$("[data-act]").forEach((b) => b.addEventListener("click", (e) => { ripple(e); call(b.dataset.act); }));
  $("#cta").addEventListener("click", (e) => { ripple(e); call(S && S.receiver.running ? "stop" : "start"); });
  $("#faultRetry").addEventListener("click", (e) => { ripple(e); const a = e.currentTarget.dataset.action;
    call(a === "reannounce" ? "reannounce" : "restart"); });
  $("#btToggle").addEventListener("click", (e) => { ripple(e); const st = S && S.control ? S.control.status : "off";
    call("control", { op: st === "off" || st === "error" ? "on" : "off" }); });
  $("#captureBtn").addEventListener("click", (e) => { ripple(e); call("control", { op: S && S.control && S.control.captured ? "release" : "capture" }); });
  $$("[data-ctl]").forEach((b) => b.addEventListener("click", (e) => { ripple(e); FX.pulse(e.clientX, e.clientY);
    call("control", b.dataset.key ? { op: b.dataset.ctl, key: b.dataset.key } : { op: b.dataset.ctl }); }));
  $("#typeBtn").addEventListener("click", (e) => { ripple(e); const t = $("#typeText").value; if (t.trim()) call("control", { op: "type", text: t }); });
  $$("[data-url]").forEach((el) => el.addEventListener("click", (e) => { e.preventDefault(); ripple(e);
    if (api && api.open_url) api.open_url(el.dataset.url); else window.open(el.dataset.url, "_blank"); }));
  const glow = $(".cursor-glow");
  document.addEventListener("pointermove", (e) => { glow.style.translate = `${e.clientX}px ${e.clientY}px`; });
  for (const el of $$("[data-cfg]")) {
    const key = el.dataset.cfg;
    el.addEventListener("focus", () => editing = el);
    el.addEventListener("blur", () => { if (editing === el) editing = null; });
    el.addEventListener("change", async () => {
      const v = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value.trim();
      await setCfg(key, v);
      if (el.hasAttribute("data-custom")) await setCfg("preset", "custom");
      if (el.hasAttribute("data-restart") || el.hasAttribute("data-custom")) restartIfRunning();
    });
  }
  const panel = $("#panel");
  panel.addEventListener("pointermove", (e) => { const r = panel.getBoundingClientRect(); panel.style.setProperty("--sx", (e.clientX - r.left) + "px"); panel.style.setProperty("--sy", (e.clientY - r.top) + "px"); });
  const wrap = $("#deviceWrap"), dev = $("#device");
  document.addEventListener("pointermove", (e) => {
    if (calm()) return;
    const r = wrap.getBoundingClientRect(), nx = (e.clientX - (r.left + r.width / 2)) / (innerWidth / 2), ny = (e.clientY - (r.top + r.height / 2)) / (innerHeight / 2);
    dev.style.setProperty("--ry", (nx * 11).toFixed(2) + "deg"); dev.style.setProperty("--rx", (-ny * 8).toFixed(2) + "deg");
    dev.style.setProperty("--mx", (50 + nx * 45).toFixed(1) + "%"); dev.style.setProperty("--my", (30 + ny * 40).toFixed(1) + "%");
  });
  const fit = () => { const h = $(".stage").clientHeight, w = $(".stage").clientWidth;
    document.documentElement.style.setProperty("--stage-scale", Math.max(.5, Math.min(1.05, (h - 70) / 514, (w - 40) / 250)).toFixed(3)); };
  fit(); addEventListener("resize", () => { fit(); segSelect($("#nav"), $("#nav button.on")); });
  requestAnimationFrame(() => segSelect($("#nav"), $('[data-nav="home"]')));
  setInterval(() => {
    if (!S || !S.receiver.since) return bind("elapsed", "");
    const sec = Math.max(0, Math.floor(Date.now() / 1000 - S.receiver.since));
    bind("elapsed", `подключён ${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`);
  }, 500);
  // walk through the three steps while waiting for the iPhone
  let step = 0;
  setInterval(() => { step = (step % 3) + 1; $$(".steps li").forEach((li) => li.classList.toggle("now", +li.dataset.step === step)); }, 2200);
});
