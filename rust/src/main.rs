//! iMirror — iPhone screen mirroring for Windows. Created by Ernomoon (https://t.me/Ernomoon).
#![windows_subsystem = "windows"]

mod cfg;
mod control;
mod core;
mod diag;
mod mirror;
mod net;
mod receiver;
mod util;

use serde_json::{json, Value};
use std::borrow::Cow;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tao::dpi::LogicalSize;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use tao::window::WindowBuilder;
use wry::http::{header::CONTENT_TYPE, Response};
use wry::WebViewBuilder;

const INDEX: &str = include_str!("../../app/imirror/ui/index.html");
const CSS: &str = include_str!("../../app/imirror/ui/styles.css");
const JS: &str = include_str!("../../app/imirror/ui/app.js");
const ICON_PNG: &[u8] = include_bytes!("../../packaging/icon.png");

const SHIM: &str = r#"
(() => {
  let seq = 0; const waiting = new Map();
  const send = (name, args) => new Promise((res) => { const id = ++seq; waiting.set(id, res);
    window.ipc.postMessage(JSON.stringify({ id, name, args: args || {} })); });
  window.__imirrorReply = (id, value) => { const r = waiting.get(id); if (r) { waiting.delete(id); r(value); } };
  window.pywebview = { api: {
    state: () => send("state"), call: (n, a) => send("call", { name: n, args: a || {} }), ready: () => send("ready"),
    minimize: () => send("minimize"), toggle_maximize: () => send("toggle_maximize"), close: () => send("close"),
    open_url: (u) => send("open_url", { url: u }),
  } };
  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    const d = e.target.closest && e.target.closest(".pywebview-drag-region");
    if (d && !e.target.closest("button, a, input")) { send(e.detail === 2 ? "toggle_maximize" : "drag"); }
  });
  window.addEventListener("DOMContentLoaded", () => window.dispatchEvent(new Event("pywebviewready")));
})();
"#;

enum UserEvent {
    Reply(u64, Value),
    Push,
    Show,
    Focus,
}

fn single_instance() -> bool {
    use windows::core::w;
    use windows::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
    use windows::Win32::System::Threading::CreateMutexW;
    use windows::Win32::UI::WindowsAndMessaging::{FindWindowW, SetForegroundWindow, ShowWindow, SW_RESTORE};
    unsafe {
        let _m = CreateMutexW(None, true, w!("Local\\iMirror-Ernomoon-single"));
        if GetLastError() == ERROR_ALREADY_EXISTS {
            if let Ok(hw) = FindWindowW(None, w!("iMirror")) { let _ = ShowWindow(hw, SW_RESTORE); let _ = SetForegroundWindow(hw); }
            return false;
        }
        std::mem::forget(_m);
    }
    true
}

fn selftest() -> i32 {
    let core = core::Core::new(Arc::new(|| {}));
    let _ = core.act_start(false);
    let mut ok = false;
    for _ in 0..80 {
        std::thread::sleep(Duration::from_millis(500));
        if core.state()["mdns"]["ok"] == json!(true) { ok = true; break; }
    }
    if ok {
        // stay reachable so the CI browser (and GET /info) can verify it from outside
        let secs: u64 = std::env::var("IMIRROR_SELFTEST_HOLD").ok().and_then(|v| v.parse().ok()).unwrap_or(30);
        std::thread::sleep(Duration::from_secs(secs));
    }
    let st = core.state();
    let res = json!({"ok": ok, "phase": st["phase"], "mdns": st["mdns"], "checks": st["checks"], "log": st["log"]});
    let _ = std::fs::write(util::data_dir().join("selftest-result.json"), serde_json::to_vec_pretty(&res).unwrap());
    core.shutdown();
    if ok { 0 } else { 1 }
}

fn main() {
    util::log_init();
    log!("iMirror {} start {:?}", env!("CARGO_PKG_VERSION"), std::env::args().collect::<Vec<_>>());
    if std::env::args().any(|a| a == "--selftest") { std::process::exit(selftest()); }
    if !single_instance() { return; }

    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    let dirty = Arc::new(AtomicBool::new(true));
    let d2 = dirty.clone();
    let p2 = proxy.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(80));
        if d2.swap(false, Ordering::SeqCst) && p2.send_event(UserEvent::Push).is_err() { break; }
    });
    let d3 = dirty.clone();
    let core = core::Core::new(Arc::new(move || d3.store(true, Ordering::SeqCst)));
    {
        let d4 = dirty.clone();
        *core.ctl.lock().unwrap() = Some(control::Control::new(Arc::new(move || d4.store(true, Ordering::SeqCst))));
    }
    {
        let g = core.inner.lock().unwrap();
        mirror::set_defaults(cfg::b(&g.cfg, "always_on_top"), cfg::f(&g.cfg, "window_scale"));
    }
    let cm = core.clone();
    mirror::set_action_handler(Box::new(move |a: &str| {
        let c = cm.clone();
        let a = a.to_string();
        std::thread::spawn(move || {
            match a.as_str() {
                "home" | "switcher" => { c.dispatch("control", &json!({"op": a})); }
                "pin_on" | "pin_off" => { c.dispatch("set", &json!({"key": "always_on_top", "value": a == "pin_on"})); }
                "close" => { c.dispatch("restart", &json!({})); }
                _ => {}
            }
        });
    }));

    let mut icon = None;
    {
        use tao::platform::windows::IconExtWindows;
        if let Ok(i) = tao::window::Icon::from_resource(1, None) { icon = Some(i); }
    }
    let window = WindowBuilder::new()
        .with_title("iMirror")
        .with_decorations(false)
        .with_inner_size(LogicalSize::new(1180.0, 780.0))
        .with_min_inner_size(LogicalSize::new(860.0, 600.0))
        .with_visible(false)
        .with_window_icon(icon)
        .build(&event_loop)
        .expect("window");
    let window = Arc::new(window);

    let cc = core.clone();
    let pr = proxy.clone();
    let w2 = window.clone();
    let webview = WebViewBuilder::new()
        .with_initialization_script(SHIM)
        .with_background_color((10, 10, 14, 255))
        .with_custom_protocol("imirror".into(), move |_id, req| {
            let path = req.uri().path().trim_start_matches('/');
            let (body, mime): (&[u8], &str) = match path {
                "" | "index.html" => (INDEX.as_bytes(), "text/html; charset=utf-8"),
                "styles.css" => (CSS.as_bytes(), "text/css; charset=utf-8"),
                "app.js" => (JS.as_bytes(), "text/javascript; charset=utf-8"),
                "icon.png" => (ICON_PNG, "image/png"),
                _ => (b"", "text/plain"),
            };
            Response::builder().header(CONTENT_TYPE, mime).body(Cow::Owned(body.to_vec())).unwrap()
        })
        .with_url("imirror://localhost/index.html")
        .with_ipc_handler(move |req| {
            let msg: Value = serde_json::from_str(req.body()).unwrap_or(Value::Null);
            let id = msg["id"].as_u64().unwrap_or(0);
            let name = msg["name"].as_str().unwrap_or("").to_string();
            let args = msg["args"].clone();
            match name.as_str() {
                "drag" => { let _ = w2.drag_window(); }
                "minimize" => w2.set_minimized(true),
                "toggle_maximize" => w2.set_maximized(!w2.is_maximized()),
                "close" => { let _ = pr.send_event(UserEvent::Focus); w2.set_visible(false); cc.shutdown(); std::process::exit(0); }
                "open_url" => util::open_url(args["url"].as_str().unwrap_or("")),
                "ready" => { let _ = pr.send_event(UserEvent::Show); }
                _ => {}
            }
            let (c, p) = (cc.clone(), pr.clone());
            std::thread::spawn(move || {
                let v = match name.as_str() {
                    "state" => c.state(),
                    "call" => c.dispatch(args["name"].as_str().unwrap_or(""), &args["args"]),
                    _ => json!(true),
                };
                let _ = p.send_event(UserEvent::Reply(id, v));
            });
        })
        .build(&*window)
        .expect("webview");

    // show even if the page never says it is ready
    let p3 = proxy.clone();
    std::thread::spawn(move || { std::thread::sleep(Duration::from_secs(4)); let _ = p3.send_event(UserEvent::Show); });

    {
        let g = core.inner.lock().unwrap();
        let (auto, bt) = (cfg::b(&g.cfg, "autostart"), cfg::b(&g.cfg, "control_enabled"));
        drop(g);
        let c = core.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(700));
            if auto { c.dispatch("start", &json!({})); }
            if bt { c.dispatch("control", &json!({"op": "on"})); }
        });
    }

    let mut shown = false;
    event_loop.run(move |event, _, flow| {
        *flow = ControlFlow::Wait;
        match event {
            Event::UserEvent(UserEvent::Reply(id, v)) => {
                let _ = webview.evaluate_script(&format!("window.__imirrorReply({id}, {v})"));
            }
            Event::UserEvent(UserEvent::Push) => {
                let st = core.state();
                let _ = webview.evaluate_script(&format!("window.__imirror && window.__imirror.push({st})"));
            }
            Event::UserEvent(UserEvent::Show) => {
                if !shown { shown = true; window.set_visible(true); window.set_focus(); }
            }
            Event::UserEvent(UserEvent::Focus) => {}
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } => {
                core.shutdown();
                *flow = ControlFlow::Exit;
            }
            _ => {}
        }
    });
}
