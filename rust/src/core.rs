//! Application state and actions (port of iMirror 4.x core).
use crate::receiver::{self, Ev, Receiver};
use crate::{cfg, diag, net};
use serde_json::{json, Map, Value};
use std::collections::VecDeque;
use std::sync::mpsc::{channel, Receiver as Rx, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const STARTUP_LIMIT: Duration = Duration::from_secs(15);

#[derive(Default, Clone)]
pub struct Stream {
    pub connections: i64,
    pub codec: String,
    pub width: i64,
    pub height: i64,
    pub fps: f64,
    pub kbps: f64,
    pub hist: VecDeque<f64>,
}

pub struct Inner {
    pub cfg: Map<String, Value>,
    pub rx: Receiver,
    pub stream: Stream,
    pub mdns: Value,          // {ok, addresses, error, method}
    pub services: Vec<net::Service>,
    pub announcer: net::Announcer,
    pub fault: Value,
    pub safe_mode: bool,
    pub stage: String,
    pub checks: Value,
    pub checking: bool,
    pub log: VecDeque<String>,
    pub toasts: VecDeque<Value>,
    toast_id: u64,
    pub started: Option<Instant>,
    pub connected_since: f64,
    pub crashes: Vec<Instant>,
    pub window: bool,
    pub fix_times: std::collections::HashMap<String, Instant>,
    pub last_check: Option<Instant>,
}

#[derive(Clone)]
pub struct Core {
    pub inner: Arc<Mutex<Inner>>,
    pub tx: Sender<Ev>,
    pub notify: Arc<dyn Fn() + Send + Sync>,
    pub ctl: Arc<Mutex<Option<crate::control::Control>>>,
}

impl Core {
    pub fn new(notify: Arc<dyn Fn() + Send + Sync>) -> Core {
        let mut c = cfg::load();
        let fip = cfg::s(&c, "forced_ip").to_string();
        if !fip.is_empty() && !net::adapters().iter().any(|a| a.ip == fip && a.lan()) {
            crate::log!("dropping forced adapter {fip}");
            c.insert("forced_ip".into(), json!(""));
        }
        let (tx, rx) = channel();
        let core = Core {
            inner: Arc::new(Mutex::new(Inner {
                cfg: c, rx: Receiver::new(), stream: Stream::default(),
                mdns: json!({"ok": null, "addresses": [], "error": "", "method": ""}),
                services: vec![], announcer: net::Announcer::default(), fault: Value::Null, safe_mode: false,
                stage: String::new(), checks: json!([]), checking: false, log: VecDeque::new(), toasts: VecDeque::new(),
                toast_id: 0, started: None, connected_since: 0.0, crashes: vec![], window: false,
                fix_times: Default::default(), last_check: None,
            })),
            tx, notify, ctl: Arc::new(Mutex::new(None)),
        };
        let c2 = core.clone();
        std::thread::Builder::new().name("events".into()).spawn(move || c2.event_loop(rx)).unwrap();
        let c3 = core.clone();
        std::thread::Builder::new().name("watch".into()).spawn(move || c3.watch_loop()).unwrap();
        core.run_checks(true);
        core
    }

    fn changed(&self) { (self.notify)(); }

    pub fn logln(&self, text: &str) {
        crate::log!("{text}");
        let mut g = self.inner.lock().unwrap();
        g.log.push_back(format!("{} {}", crate::util::now_hms(), text));
        while g.log.len() > 300 { g.log.pop_front(); }
    }

    pub fn toast(&self, text: &str, kind: &str) {
        self.logln(text);
        let mut g = self.inner.lock().unwrap();
        g.toast_id += 1;
        let id = g.toast_id;
        g.toasts.push_back(json!({"id": id, "text": text, "kind": kind}));
        while g.toasts.len() > 8 { g.toasts.pop_front(); }
    }

    pub fn phase(g: &mut Inner) -> &'static str {
        if !g.rx.running() { return if g.fault.is_null() { "off" } else { "problem" }; }
        if g.stream.connections > 0 && g.stream.width > 0 { return "live"; }
        if g.stream.connections > 0 { return "connecting"; }
        match g.mdns.get("ok").and_then(|v| v.as_bool()) { Some(false) => "problem", Some(true) => "ready", None => "starting" }
    }

    pub fn state(&self) -> Value {
        let ctl = self.ctl.lock().unwrap().as_ref().map(|c| c.state());
        let mut g = self.inner.lock().unwrap();
        let phase = Self::phase(&mut g);
        let running = g.rx.running();
        let presets: Map<String, Value> = cfg::PRESETS.iter().map(|p| (p.0.to_string(),
            json!({"title": p.1, "w": p.2, "h": p.3, "hz": p.4, "fps": p.5, "h265": p.6}))).collect();
        let peak = g.stream.hist.iter().cloned().fold(0.0, f64::max);
        json!({
            "platform": "win32", "phase": phase, "cfg": g.cfg, "presets": presets,
            "sinks": ["d3d12videosink", "d3d11videosink", "autovideosink"],
            "receiver": {"running": running, "connections": g.stream.connections, "codec": g.stream.codec,
                         "width": g.stream.width, "height": g.stream.height,
                         "fps": (g.stream.fps * 10.0).round() / 10.0, "peak": (peak * 10.0).round() / 10.0,
                         "mbps": (g.stream.kbps / 100.0).round() / 10.0,
                         "fps_hist": g.stream.hist.iter().collect::<Vec<_>>(), "window": g.window, "since": g.connected_since},
            "mdns": g.mdns, "fault": g.fault, "safe_mode": g.safe_mode, "control": ctl,
            "checks": g.checks, "checking": g.checking,
            "log": g.log.iter().rev().take(200).rev().collect::<Vec<_>>(),
            "toasts": g.toasts.iter().collect::<Vec<_>>(),
            "about": {"version": env!("CARGO_PKG_VERSION"), "author": "Ernomoon", "telegram": "https://t.me/Ernomoon"},
        })
    }

    // ============================================================== actions
    pub fn dispatch(&self, name: &str, args: &Value) -> Value {
        let r = match name {
            "set" => self.act_set(args["key"].as_str().unwrap_or(""), args["value"].clone()),
            "start" => self.act_start(false),
            "safe_start" => { self.act_stop(); std::thread::sleep(Duration::from_millis(500)); self.act_start(true) }
            "stop" => { self.act_stop(); Ok(json!({"ok": true})) }
            "restart" => { self.act_stop(); std::thread::sleep(Duration::from_millis(600)); self.act_start(false) }
            "dismiss" => { self.inner.lock().unwrap().fault = Value::Null; Ok(json!({"ok": true})) }
            "reannounce" => { self.announce(); Ok(json!({"ok": true})) }
            "adapters" => Ok(serde_json::to_value(net::adapters().iter().map(|a| json!({
                "name": a.name, "ip": a.ip, "virtual": a.virtual_, "prefix": a.prefix, "reasons": a.reasons,
                "description": a.description, "index": a.index, "gateway": a.gateway, "wifi": a.wifi, "profile": a.profile
            })).collect::<Vec<_>>()).unwrap()),
            "check" => { self.run_checks(true); Ok(json!({"ok": true})) }
            "fix" => self.act_fix(args["fix"].as_str().unwrap_or(""), args["arg"].as_str().unwrap_or("")),
            "fit" => { crate::mirror::fit(); Ok(json!({"ok": true})) }
            "fullscreen" => { crate::mirror::toggle_fullscreen(); Ok(json!({"ok": true})) }
            "control" => {
                let op = args["op"].as_str().unwrap_or("").to_string();
                let r = match self.ctl.lock().unwrap().as_ref() { Some(c) => c.dispatch(&op, args), None => Err("Управление недоступно".into()) };
                if r.is_ok() && (op == "on" || op == "off") {
                    let mut g = self.inner.lock().unwrap();
                    g.cfg.insert("control_enabled".into(), json!(op == "on"));
                    cfg::save(&g.cfg);
                }
                if let Ok(v) = &r { if let Some(n) = v.get("note").and_then(|n| n.as_str()) { if !n.is_empty() { self.toast(n, "info"); } } }
                r
            }
            _ => Err(format!("unknown action {name}")),
        };
        self.changed();
        match r {
            Ok(v) => v,
            Err(e) => { self.toast(&e, "error"); json!({"ok": false, "error": e}) }
        }
    }

    fn act_set(&self, key: &str, value: Value) -> Result<Value, String> {
        let mut g = self.inner.lock().unwrap();
        if !cfg::defaults().contains_key(key) { return Err(format!("unknown setting {key}")); }
        g.cfg.insert(key.into(), value.clone());
        if key == "preset" {
            if let Some(p) = cfg::PRESETS.iter().find(|p| Some(p.0) == value.as_str()) {
                g.cfg.insert("width".into(), json!(p.2)); g.cfg.insert("height".into(), json!(p.3));
                g.cfg.insert("refresh".into(), json!(p.4)); g.cfg.insert("max_fps".into(), json!(p.5));
                g.cfg.insert("h265".into(), json!(p.6));
            }
        }
        cfg::save(&g.cfg);
        let reannounce = key == "forced_ip";
        let topmost = cfg::b(&g.cfg, "always_on_top");
        drop(g);
        if key == "always_on_top" { crate::mirror::set_topmost(topmost); }
        if reannounce { self.announce(); }
        Ok(json!({"ok": true}))
    }

    pub fn act_start(&self, safe: bool) -> Result<Value, String> {
        let mut g = self.inner.lock().unwrap();
        if g.rx.running() { return Ok(json!({"ok": true})); }
        let exe = receiver::find_uxplay().ok_or("Не найден приёмник (receiver\\bin\\uxplay.exe). Переустановите iMirror")?;
        g.fault = Value::Null;
        g.safe_mode = safe;
        g.stage.clear();
        g.mdns = json!({"ok": null, "addresses": [], "error": "", "method": ""});
        g.services.clear();
        g.stream = Stream::default();
        let mut c = g.cfg.clone();
        let args = receiver::build_args(&mut c, safe);
        if cfg::s(&c, "device_id") != cfg::s(&g.cfg, "device_id") {
            g.cfg.insert("device_id".into(), c["device_id"].clone());
            cfg::save(&g.cfg);
        }
        let line = format!("Запуск {}: {}", exe.display(), args.join(" "));
        g.rx.start(&exe, &args, self.tx.clone()).map_err(|e| format!("Не удалось запустить приёмник: {e}"))?;
        g.started = Some(Instant::now());
        drop(g);
        if safe { self.logln("Безопасный режим: настройки UxPlay по умолчанию, без звука"); }
        self.logln(&line);
        self.logln("Приёмник запущен");
        let me = self.clone();
        std::thread::spawn(move || { std::thread::sleep(Duration::from_secs(6)); me.run_checks(true); });
        Ok(json!({"ok": true}))
    }

    pub fn act_stop(&self) {
        let mut g = self.inner.lock().unwrap();
        g.fault = Value::Null;
        g.started = None;
        g.rx.stop();
        g.announcer.stop();
        g.services.clear();
        g.stream = Stream::default();
        g.mdns = json!({"ok": null, "addresses": [], "error": "", "method": ""});
        drop(g);
        crate::mirror::detach();
    }

    fn act_fix(&self, fix: &str, arg: &str) -> Result<Value, String> {
        {
            let mut g = self.inner.lock().unwrap();
            if let Some(t) = g.fix_times.get(fix) { if t.elapsed() < Duration::from_secs(20) { return Ok(json!({"ok": true, "pending": true})); } }
            g.fix_times.insert(fix.into(), Instant::now());
        }
        match fix {
            "reannounce" => { self.announce(); return Ok(json!({"ok": true})); }
            "start" => return self.act_start(false),
            _ => {}
        }
        if !diag::apply_fix(fix, arg) {
            return Err("Windows не дала запустить исправление (нужно подтвердить запрос администратора)".into());
        }
        self.toast("Подтвердите запрос Windows. Через несколько секунд проверю снова", "info");
        for s in [6u64, 15] {
            let me = self.clone();
            std::thread::spawn(move || { std::thread::sleep(Duration::from_secs(s)); me.run_checks(true); });
        }
        Ok(json!({"ok": true}))
    }

    pub fn run_checks(&self, force: bool) {
        {
            let mut g = self.inner.lock().unwrap();
            if g.checking || (!force && g.last_check.map(|t| t.elapsed() < Duration::from_secs(5)).unwrap_or(false)) { return; }
            g.checking = true;
        }
        self.changed();
        let me = self.clone();
        std::thread::Builder::new().name("checks".into()).spawn(move || {
            let (running, port, mdns) = {
                let mut g = me.inner.lock().unwrap();
                let port = g.services.first().map(|s| s.port).unwrap_or(0);
                (g.rx.running(), port, g.mdns.clone())
            };
            let checks = diag::run_checks(running, port, &mdns);
            let bad: Vec<String> = checks.iter().filter(|c| c["state"] == "bad").map(|c| c["title"].as_str().unwrap_or("").to_string()).collect();
            crate::log!("checks: {}", checks.iter().map(|c| format!("{}={}", c["id"].as_str().unwrap_or(""), c["state"].as_str().unwrap_or(""))).collect::<Vec<_>>().join(" "));
            if !bad.is_empty() { me.logln(&format!("Проверка нашла проблемы: {}", bad.join(", "))); }
            {
                let mut g = me.inner.lock().unwrap();
                g.checks = Value::Array(checks);
                g.checking = false;
                g.last_check = Some(Instant::now());
            }
            me.changed();
        }).unwrap();
    }

    // ============================================================== announcement
    pub fn announce(&self) {
        let mut g = self.inner.lock().unwrap();
        if g.services.len() < 2 { return; }
        let list = net::adapters();
        let forced = cfg::s(&g.cfg, "forced_ip").to_string();
        let Some(best) = net::best(&list, &forced) else {
            g.mdns = json!({"ok": false, "addresses": [], "error": "компьютер не подключён к сети", "method": "native"});
            return;
        };
        let services = g.services.clone();
        let mut ann = std::mem::take(&mut g.announcer);
        drop(g);
        let r = ann.register(&services, &best);
        let mut g = self.inner.lock().unwrap();
        if g.services.len() < 2 { ann.stop(); return; }   // stopped meanwhile
        g.announcer = ann;
        match r {
            Ok(()) => {
                g.mdns = json!({"ok": true, "addresses": [best.ip], "error": "", "method": "native", "iface": best.name});
                g.fault = Value::Null;
                drop(g);
                self.logln(&format!("Объявлено в сети встроенной службой Windows: {} ({})", best.ip, best.name));
            }
            Err(e) => {
                g.mdns = json!({"ok": false, "addresses": [], "error": e, "method": "native"});
                drop(g);
                self.toast(&format!("Не удалось объявить приёмник в сети: {e}"), "error");
            }
        }
        self.run_checks(true);
    }

    // ============================================================== events
    fn event_loop(&self, rx: Rx<Ev>) {
        while let Ok(ev) = rx.recv() {
            match ev {
                Ev::Line(l) => self.logln(&l),
                Ev::Stage(s) => { crate::log!("receiver stage: {s}"); self.inner.lock().unwrap().stage = s; }
                Ev::Mdns { kind, port, name, txt } => {
                    crate::log!("service data: {kind} port {port}");
                    let ready = {
                        let mut g = self.inner.lock().unwrap();
                        g.stage = "ready".into();
                        g.services.retain(|s| s.kind != kind);
                        g.services.push(net::Service { kind, instance: name, port, txt: net::parse_txt(&txt) });
                        g.services.len() >= 2
                    };
                    if ready { let me = self.clone(); std::thread::spawn(move || me.announce()); }
                }
                Ev::Conn(n) => {
                    let mut g = self.inner.lock().unwrap();
                    let was = g.stream.connections;
                    g.stream.connections = n;
                    if n > 0 && was == 0 {
                        g.connected_since = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
                        drop(g);
                        self.logln("iPhone подключился");
                    } else if n == 0 && was > 0 {
                        g.stream = Stream::default();
                        drop(g);
                        self.logln("iPhone отключился");
                        crate::mirror::detach();
                    }
                }
                Ev::Video { codec, w, h } => {
                    let first = {
                        let mut g = self.inner.lock().unwrap();
                        let first = g.stream.width == 0;
                        g.stream.codec = codec.clone(); g.stream.width = w; g.stream.height = h;
                        first
                    };
                    if first { self.logln(&format!("Картинка: {w}×{h} {}", codec.to_uppercase())); }
                    crate::mirror::video_size(w as i32, h as i32);
                }
                Ev::Stats { fps, kbps } => {
                    let mut g = self.inner.lock().unwrap();
                    g.stream.fps = fps; g.stream.kbps = kbps;
                    g.stream.hist.push_back(fps);
                    while g.stream.hist.len() > 30 { g.stream.hist.pop_front(); }
                }
                Ev::Exited { code, requested, gen } => self.on_exit(code, requested, gen),
            }
            self.changed();
        }
    }

    fn on_exit(&self, code: i64, requested: bool, gen: u64) {
        let mut g = self.inner.lock().unwrap();
        if gen != g.rx.gen { return; }
        let tail: Vec<String> = g.rx.tail.lock().unwrap().iter().rev().take(12).rev().cloned().collect();
        crate::log!("receiver exited with code {code} (requested={requested}); last output:\n{}", tail.join("\n"));
        g.announcer.stop();
        g.services.clear();
        g.stream = Stream::default();
        g.mdns = json!({"ok": null, "addresses": [], "error": "", "method": ""});
        g.started = None;
        if requested { return; }
        g.crashes.retain(|t| t.elapsed() < Duration::from_secs(120));
        g.crashes.push(Instant::now());
        let crashes = g.crashes.len();
        let early = g.stage.is_empty();
        let safe = g.safe_mode;
        drop(g);
        crate::mirror::detach();
        // Stability: restart by itself. Early crashes switch to safe mode; repeated ones stop.
        if crashes <= 4 {
            let use_safe = safe || (early && crashes >= 2);
            self.logln(&format!("Приёмник остановился (код {code}), перезапускаю{}", if use_safe { " в безопасном режиме" } else { "" }));
            let me = self.clone();
            std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(1200)); let _ = me.act_start(use_safe); me.changed(); });
            return;
        }
        let hint = exit_hint(code, &tail);
        let mut g = self.inner.lock().unwrap();
        g.fault = json!({"title": "Приёмник AirPlay остановился", "detail": format!("{hint} (код {code})."), "tail": tail, "action": "start"});
        drop(g);
        self.toast(&format!("Приёмник остановился: {hint}"), "error");
    }

    /// Start-up watchdog, network-change re-announcement, mirror window tracking.
    fn watch_loop(&self) {
        let mut last_best = String::new();
        let mut tick = 0u64;
        loop {
            std::thread::sleep(Duration::from_millis(500));
            tick += 1;
            let (stuck, pid, live, frame) = {
                let mut g = self.inner.lock().unwrap();
                let running = g.rx.running();
                let stuck = running && g.mdns["ok"].is_null() && g.started.map(|t| t.elapsed() > STARTUP_LIMIT).unwrap_or(false);
                let live = running && g.stream.connections > 0;
                (stuck, g.rx.pid, live, cfg::b(&g.cfg, "phone_frame"))
            };
            if stuck {
                let (stage, safe, tail) = {
                    let g = self.inner.lock().unwrap();
                    let t = g.rx.tail.lock().unwrap().clone();
                    (g.stage.clone(), g.safe_mode, t)
                };
                crate::log!("startup watchdog: stage={stage:?}");
                if stage == "ready" || stage == "register" {
                    self.announce();
                    self.inner.lock().unwrap().started = None;
                } else {
                    self.act_stop();
                    if !safe {
                        self.logln("Приёмник не запустился за 15 с, пробую безопасный режим");
                        let _ = self.act_start(true);
                    } else {
                        let mut g = self.inner.lock().unwrap();
                        g.fault = json!({"title": "Приёмник AirPlay не запускается",
                            "detail": "UxPlay завис при запуске даже в безопасном режиме. Последние строки его вывода ниже и в журнале.",
                            "tail": tail.iter().rev().take(12).rev().collect::<Vec<_>>(), "action": "start"});
                    }
                }
                self.changed();
            }
            // network change: re-announce on the new best adapter
            if tick % 8 == 0 {
                let (has, forced, cur) = {
                    let g = self.inner.lock().unwrap();
                    (g.services.len() >= 2, cfg::s(&g.cfg, "forced_ip").to_string(), g.announcer.ip.clone())
                };
                if has {
                    let best = net::best(&net::adapters(), &forced).map(|a| a.ip).unwrap_or_default();
                    if best != last_best || (cur.is_empty() && !best.is_empty()) || (!cur.is_empty() && cur != best) {
                        if !last_best.is_empty() && best != cur { self.logln(&format!("Сеть изменилась: {best}")); self.announce(); }
                        last_best = best;
                    }
                }
            }
            if live && frame { crate::mirror::track(pid); }
            let w = crate::mirror::attached();
            let changed = { let mut g = self.inner.lock().unwrap(); let c = g.window != w; g.window = w; c };
            if changed { self.changed(); }
        }
    }

    pub fn shutdown(&self) {
        if let Some(c) = self.ctl.lock().unwrap().as_ref() { c.shutdown(); }
        self.act_stop();
        cfg::save(&self.inner.lock().unwrap().cfg);
    }
}

fn exit_hint(code: i64, tail: &[String]) -> String {
    let t = tail.join("\n").to_lowercase();
    if t.contains("address already in use") || t.contains("10048") { return "порт занят другой программой AirPlay — закройте её".into(); }
    match code as u32 {
        0xC000001D => "недопустимая инструкция процессора".into(),
        0xC0000005 => "ошибка доступа к памяти".into(),
        0xC0000135 => "не найдена библиотека приёмника — переустановите iMirror".into(),
        _ => "приёмник неожиданно завершился".into(),
    }
}
