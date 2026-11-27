//! The iPhone window, macOS "iPhone Mirroring" style: the video from UxPlay is placed inside
//! a phone-shaped frameless window (rounded corners, thin bezel, aspect-locked resize, drag by
//! the bezel) with a floating toolbar that fades in when the pointer is near.
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::sync::{Mutex, OnceLock};
use windows::core::{w, BOOL, HSTRING, PCWSTR};
use windows::Win32::Foundation::*;
use windows::Win32::Graphics::Dwm::*;
use windows::Win32::Graphics::Gdi::*;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Input::KeyboardAndMouse::{ReleaseCapture, TrackMouseEvent, TME_LEAVE, TRACKMOUSEEVENT};
use windows::Win32::UI::WindowsAndMessaging::*;

const WM_ATTACH: u32 = WM_APP + 1;
const WM_DETACH: u32 = WM_APP + 2;
const WM_VSIZE: u32 = WM_APP + 3;
const WM_FIT: u32 = WM_APP + 4;
const WM_FULL: u32 = WM_APP + 5;
const WM_TOP: u32 = WM_APP + 6;

static FRAME: AtomicIsize = AtomicIsize::new(0);
static BAR: AtomicIsize = AtomicIsize::new(0);
static CHILD: AtomicIsize = AtomicIsize::new(0);
static STARTED: AtomicBool = AtomicBool::new(false);
static ACTION: OnceLock<Box<dyn Fn(&str) + Send + Sync>> = OnceLock::new();

struct St {
    vw: i32,
    vh: i32,
    full: bool,
    saved: RECT,
    top: bool,
    alpha: i32,
    hover: i32,
    scale: f64,
    pending_pid: u32,
}
static ST: Mutex<St> = Mutex::new(St { vw: 0, vh: 0, full: false, saved: RECT { left: 0, top: 0, right: 0, bottom: 0 },
                                       top: false, alpha: 0, hover: -1, scale: 0.82, pending_pid: 0 });

pub fn set_action_handler(f: Box<dyn Fn(&str) + Send + Sync>) { let _ = ACTION.set(f); }
pub fn set_defaults(top: bool, scale: f64) { let mut s = ST.lock().unwrap(); s.top = top; if scale > 0.3 { s.scale = scale.min(0.98); } }
fn act(name: &str) { if let Some(f) = ACTION.get() { f(name); } }

fn h(v: isize) -> HWND { HWND(v as *mut _) }
fn frame() -> Option<HWND> { let v = FRAME.load(Ordering::SeqCst); if v == 0 { None } else { Some(h(v)) } }
fn post(msg: u32, w: usize, l: isize) {
    ensure_thread();
    for _ in 0..50 {
        if let Some(f) = frame() { unsafe { let _ = PostMessageW(Some(f), msg, WPARAM(w), LPARAM(l)); } return; }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
}

pub fn attached() -> bool { CHILD.load(Ordering::SeqCst) != 0 }
pub fn video_hwnd() -> isize { CHILD.load(Ordering::SeqCst) }
pub fn video_size(w: i32, hh: i32) { post(WM_VSIZE, w as usize, hh as isize); }
pub fn detach() { if STARTED.load(Ordering::SeqCst) { post(WM_DETACH, 0, 0); } }
pub fn fit() { if attached() { post(WM_FIT, 0, 0); } }
pub fn toggle_fullscreen() { if attached() { post(WM_FULL, 0, 0); } }
pub fn set_topmost(on: bool) { ST.lock().unwrap().top = on; if STARTED.load(Ordering::SeqCst) { post(WM_TOP, on as usize, 0); } }

/// Find UxPlay's video window and move it into the phone frame.
pub fn track(pid: u32) {
    let child = CHILD.load(Ordering::SeqCst);
    if child != 0 && unsafe { IsWindow(Some(h(child))).as_bool() } { return; }
    if child != 0 { CHILD.store(0, Ordering::SeqCst); }
    if pid == 0 { return; }
    ST.lock().unwrap().pending_pid = pid;
    let mut found: isize = 0;
    unsafe extern "system" fn enum_cb(hwnd: HWND, l: LPARAM) -> BOOL {
        let out = &mut *(l.0 as *mut (u32, isize));
        let mut p = 0u32;
        GetWindowThreadProcessId(hwnd, Some(&mut p));
        if p == out.0 && IsWindowVisible(hwnd).as_bool() {
            let mut r = RECT::default();
            let _ = GetWindowRect(hwnd, &mut r);
            if r.right - r.left > 40 && r.bottom - r.top > 40 { out.1 = hwnd.0 as isize; return FALSE; }
        }
        TRUE
    }
    let mut data = (pid, 0isize);
    unsafe { let _ = EnumWindows(Some(enum_cb), LPARAM(&mut data as *mut _ as isize)); }
    found = found.max(data.1);
    if found != 0 { post(WM_ATTACH, 0, found); }
}

fn ensure_thread() {
    if STARTED.swap(true, Ordering::SeqCst) { return; }
    std::thread::Builder::new().name("iphone-window".into()).spawn(|| unsafe { run() }).unwrap();
}

// ------------------------------------------------------------------ geometry
fn work_area(hwnd: Option<HWND>) -> RECT {
    unsafe {
        let mon = match hwnd { Some(w) => MonitorFromWindow(w, MONITOR_DEFAULTTONEAREST), None => MonitorFromPoint(POINT::default(), MONITOR_DEFAULTTOPRIMARY) };
        let mut mi = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
        let _ = GetMonitorInfoW(mon, &mut mi);
        mi.rcWork
    }
}
fn monitor_rect(hwnd: HWND) -> RECT {
    unsafe {
        let mut mi = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
        let _ = GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), &mut mi);
        mi.rcMonitor
    }
}
fn bezel(w: i32, hh: i32) -> i32 { ((w.min(hh) as f64) * 0.028).round().max(6.0) as i32 }
fn radius(w: i32, hh: i32) -> i32 { ((w.min(hh) as f64) * 0.16).round() as i32 }

fn aspect() -> f64 {
    let s = ST.lock().unwrap();
    if s.vw > 0 && s.vh > 0 { s.vw as f64 / s.vh as f64 } else { 0.4615 }
}

fn default_rect(hwnd: HWND) -> RECT {
    let wa = work_area(Some(hwnd));
    let (ww, wh) = ((wa.right - wa.left) as f64, (wa.bottom - wa.top) as f64);
    let a = aspect();
    let scale = ST.lock().unwrap().scale;
    let (mut hh, mut w) = (wh * scale, wh * scale * a);
    if w > ww * 0.8 { w = ww * 0.8; hh = w / a; }
    let b = bezel(w as i32, hh as i32) as f64;
    let (fw, fh) = ((w + 2.0 * b) as i32, (hh + 2.0 * b) as i32);
    let x = wa.left + ((ww as i32) - fw) / 2;
    let y = wa.top + ((wh as i32) - fh) / 2;
    RECT { left: x, top: y, right: x + fw, bottom: y + fh }
}

unsafe fn apply_shape(hwnd: HWND) {
    let mut r = RECT::default();
    let _ = GetClientRect(hwnd, &mut r);
    let (w, hh) = (r.right, r.bottom);
    let full = ST.lock().unwrap().full;
    if full {
        let _ = SetWindowRgn(hwnd, None, true);
    } else {
        let rad = radius(w, hh);
        let rgn = CreateRoundRectRgn(0, 0, w + 1, hh + 1, rad * 2, rad * 2);
        let _ = SetWindowRgn(hwnd, Some(rgn), true);
    }
    let child = CHILD.load(Ordering::SeqCst);
    if child != 0 {
        let b = if full { 0 } else { bezel(w, hh) };
        let (cw, ch) = (w - 2 * b, hh - 2 * b);
        // keep the video's aspect inside (letterbox in fullscreen)
        let a = aspect();
        let (mut vw, mut vh) = (cw, (cw as f64 / a) as i32);
        if vh > ch { vh = ch; vw = (ch as f64 * a) as i32; }
        let (x, y) = (b + (cw - vw) / 2, b + (ch - vh) / 2);
        let _ = MoveWindow(h(child), x, y, vw, vh, true);
        if !full {
            let rad = (radius(w, hh) - b).max(4);
            let rgn = CreateRoundRectRgn(0, 0, vw + 1, vh + 1, rad * 2, rad * 2);
            let _ = SetWindowRgn(h(child), Some(rgn), true);
        } else {
            let _ = SetWindowRgn(h(child), None, true);
        }
    }
    place_bar();
}

unsafe fn resize_to(hwnd: HWND, r: RECT) {
    let _ = SetWindowPos(hwnd, None, r.left, r.top, r.right - r.left, r.bottom - r.top, SWP_NOZORDER | SWP_NOACTIVATE);
    apply_shape(hwnd);
}

// ------------------------------------------------------------------ toolbar
const BTN: &[(&str, u16, &str)] = &[
    ("home", 0xE80F, "Домой"), ("switcher", 0xE7C4, "Приложения"), ("pin", 0xE718, "Поверх окон"),
    ("fullscreen", 0xE740, "Во весь экран"), ("close", 0xE711, "Отключить iPhone"),
];
const BAR_W: i32 = 300;
const BAR_H: i32 = 40;
const TITLE_W: i32 = 92;

unsafe fn place_bar() {
    let (Some(f), b) = (frame(), BAR.load(Ordering::SeqCst)) else { return };
    if b == 0 { return; }
    let mut r = RECT::default();
    let _ = GetWindowRect(f, &mut r);
    let x = (r.left + r.right) / 2 - BAR_W / 2;
    let full = ST.lock().unwrap().full;
    let mut y = r.top - BAR_H - 10;
    if full || y < work_area(Some(f)).top { y = r.top + 14; }
    let _ = SetWindowPos(h(b), Some(HWND_TOP), x, y, BAR_W, BAR_H, SWP_NOACTIVATE);
    let rgn = CreateRoundRectRgn(0, 0, BAR_W + 1, BAR_H + 1, BAR_H, BAR_H);
    let _ = SetWindowRgn(h(b), Some(rgn), true);
}

fn btn_at(x: i32) -> i32 {
    if x < TITLE_W { return -1; }
    let n = BTN.len() as i32;
    let w = (BAR_W - TITLE_W - 8) / n;
    ((x - TITLE_W) / w).min(n - 1)
}

unsafe extern "system" fn bar_proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    match msg {
        WM_PAINT => {
            let mut ps = PAINTSTRUCT::default();
            let dc = BeginPaint(hwnd, &mut ps);
            let mut r = RECT { left: 0, top: 0, right: BAR_W, bottom: BAR_H };
            let bg = CreateSolidBrush(COLORREF(0x00241f1c));
            FillRect(dc, &r, bg);
            let _ = DeleteObject(bg.into());
            SetBkMode(dc, TRANSPARENT);
            let (hover, top) = { let s = ST.lock().unwrap(); (s.hover, s.top) };
            let title = CreateFontW(-14, 0, 0, 0, 600, 0, 0, 0, DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                    CLEARTYPE_QUALITY, 0, w!("Segoe UI Variable Display"));
            let old = SelectObject(dc, title.into());
            SetTextColor(dc, COLORREF(0x00f2eeea));
            let mut tr = RECT { left: 18, top: 0, right: TITLE_W, bottom: BAR_H };
            let mut t: Vec<u16> = "iPhone".encode_utf16().collect();
            DrawTextW(dc, &mut t, &mut tr, DT_SINGLELINE | DT_VCENTER | DT_LEFT);
            SelectObject(dc, old);
            let _ = DeleteObject(title.into());
            let icon = CreateFontW(-16, 0, 0, 0, 400, 0, 0, 0, DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                   CLEARTYPE_QUALITY, 0, w!("Segoe Fluent Icons"));
            let icon2 = CreateFontW(-16, 0, 0, 0, 400, 0, 0, 0, DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                    CLEARTYPE_QUALITY, 0, w!("Segoe MDL2 Assets"));
            let n = BTN.len() as i32;
            let bw = (BAR_W - TITLE_W - 8) / n;
            for (i, (id, glyph, _)) in BTN.iter().enumerate() {
                let x = TITLE_W + i as i32 * bw;
                r = RECT { left: x + 3, top: 5, right: x + bw - 3, bottom: BAR_H - 5 };
                if hover == i as i32 {
                    let hb = CreateSolidBrush(COLORREF(if *id == "close" { 0x002b2bc4 } else { 0x00403a36 }));
                    let rr = CreateRoundRectRgn(r.left, r.top, r.right, r.bottom, 14, 14);
                    let _ = FillRgn(dc, rr, hb);
                    let _ = DeleteObject(rr.into()); let _ = DeleteObject(hb.into());
                }
                let g = if *id == "pin" && top { 0xE77A } else { *glyph };
                let font = if is_win11(dc) { icon } else { icon2 };
                let o = SelectObject(dc, font.into());
                SetTextColor(dc, COLORREF(if *id == "pin" && top { 0x00c8f25e } else { 0x00e6e0da }));
                let mut s = [g];
                DrawTextW(dc, &mut s, &mut r, DT_SINGLELINE | DT_VCENTER | DT_CENTER);
                SelectObject(dc, o);
            }
            let _ = DeleteObject(icon.into());
            let _ = DeleteObject(icon2.into());
            let _ = EndPaint(hwnd, &ps);
            LRESULT(0)
        }
        WM_MOUSEMOVE => {
            let x = (lp.0 & 0xffff) as i16 as i32;
            let b = btn_at(x);
            let changed = { let mut s = ST.lock().unwrap(); let c = s.hover != b; s.hover = b; c };
            if changed {
                let _ = InvalidateRect(Some(hwnd), None, false);
                if b >= 0 {
                    let mut ti = TRACKMOUSEEVENT { cbSize: std::mem::size_of::<TRACKMOUSEEVENT>() as u32, dwFlags: TME_LEAVE, hwndTrack: hwnd, dwHoverTime: 0 };
                    let _ = TrackMouseEvent(&mut ti);
                }
            }
            LRESULT(0)
        }
        0x02A3 => { ST.lock().unwrap().hover = -1; let _ = InvalidateRect(Some(hwnd), None, false); LRESULT(0) }
        WM_LBUTTONDOWN => {
            let x = (lp.0 & 0xffff) as i16 as i32;
            if btn_at(x) < 0 {
                if let Some(f) = frame() {
                    let _ = ReleaseCapture();
                    SendMessageW(f, WM_NCLBUTTONDOWN, Some(WPARAM(HTCAPTION as usize)), Some(LPARAM(0)));
                }
            }
            LRESULT(0)
        }
        WM_LBUTTONUP => {
            let x = (lp.0 & 0xffff) as i16 as i32;
            let b = btn_at(x);
            if b >= 0 {
                match BTN[b as usize].0 {
                    "pin" => { let on = !ST.lock().unwrap().top; act(if on { "pin_on" } else { "pin_off" }); }
                    "fullscreen" => { if let Some(f) = frame() { let _ = PostMessageW(Some(f), WM_FULL, WPARAM(0), LPARAM(0)); } }
                    other => act(other),
                }
                let _ = InvalidateRect(Some(hwnd), None, false);
            }
            LRESULT(0)
        }
        WM_MOUSEACTIVATE => LRESULT(MA_NOACTIVATE as isize),
        _ => DefWindowProcW(hwnd, msg, wp, lp),
    }
}

unsafe fn is_win11(_dc: HDC) -> bool {
    // Segoe Fluent Icons ships with Windows 11; Windows 10 has Segoe MDL2 Assets (same code points).
    static HAS: OnceLock<bool> = OnceLock::new();
    *HAS.get_or_init(|| build_number() >= 22000)
}

fn build_number() -> u32 {
    let out = crate::util::run_hidden("cmd", &["/c", "ver"], std::time::Duration::from_secs(3)).unwrap_or_default();
    out.split(|c: char| !c.is_ascii_digit() && c != '.').filter(|s| s.matches('.').count() >= 2)
        .next().and_then(|v| v.split('.').nth(2)).and_then(|b| b.parse().ok()).unwrap_or(19045)
}

// ------------------------------------------------------------------ frame
unsafe extern "system" fn frame_proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    match msg {
        WM_ATTACH => {
            let child = lp.0;
            if !IsWindow(Some(h(child))).as_bool() { return LRESULT(0); }
            CHILD.store(child, Ordering::SeqCst);
            let st = GetWindowLongPtrW(h(child), GWL_STYLE);
            let _ = st;
            SetWindowLongPtrW(h(child), GWL_STYLE, (WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS).0 as isize);
            SetWindowLongPtrW(h(child), GWL_EXSTYLE, 0);
            let _ = SetParent(h(child), Some(hwnd));
            let first = !IsWindowVisible(hwnd).as_bool();
            if first { resize_to(hwnd, default_rect(hwnd)); } else { apply_shape(hwnd); }
            let top = ST.lock().unwrap().top;
            let _ = SetWindowPos(hwnd, Some(if top { HWND_TOPMOST } else { HWND_TOP }), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            let _ = ShowWindow(hwnd, SW_SHOW);
            let _ = SetForegroundWindow(hwnd);
            crate::log!("iPhone window attached");
            LRESULT(0)
        }
        WM_DETACH => {
            CHILD.store(0, Ordering::SeqCst);
            ST.lock().unwrap().vw = 0;
            let _ = ShowWindow(hwnd, SW_HIDE);
            if let Some(b) = Some(BAR.load(Ordering::SeqCst)).filter(|b| *b != 0) { let _ = ShowWindow(h(b), SW_HIDE); }
            LRESULT(0)
        }
        WM_VSIZE => {
            let (w, hh) = (wp.0 as i32, lp.0 as i32);
            let changed = { let mut s = ST.lock().unwrap(); let c = (s.vw > s.vh) != (w > hh) || s.vw == 0; s.vw = w; s.vh = hh; c };
            if IsWindowVisible(hwnd).as_bool() {
                if changed && !ST.lock().unwrap().full {
                    // rotation: keep the centre, swap orientation
                    let mut r = RECT::default();
                    let _ = GetWindowRect(hwnd, &mut r);
                    let d = default_rect(hwnd);
                    let (cx, cy) = ((r.left + r.right) / 2, (r.top + r.bottom) / 2);
                    let (dw, dh) = (d.right - d.left, d.bottom - d.top);
                    resize_to(hwnd, RECT { left: cx - dw / 2, top: cy - dh / 2, right: cx + dw / 2, bottom: cy + dh / 2 });
                } else { apply_shape(hwnd); }
            }
            LRESULT(0)
        }
        WM_FIT => { ST.lock().unwrap().full = false; resize_to(hwnd, default_rect(hwnd)); let _ = ShowWindow(hwnd, SW_SHOW); LRESULT(0) }
        WM_FULL => {
            let full = { let s = ST.lock().unwrap(); s.full };
            if !full {
                let mut r = RECT::default(); let _ = GetWindowRect(hwnd, &mut r);
                { let mut s = ST.lock().unwrap(); s.saved = r; s.full = true; }
                resize_to(hwnd, monitor_rect(hwnd));
            } else {
                let r = { let mut s = ST.lock().unwrap(); s.full = false; s.saved };
                resize_to(hwnd, r);
            }
            LRESULT(0)
        }
        WM_TOP => {
            let on = wp.0 != 0;
            let _ = SetWindowPos(hwnd, Some(if on { HWND_TOPMOST } else { HWND_NOTOPMOST }), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            let b = BAR.load(Ordering::SeqCst);
            if b != 0 { let _ = InvalidateRect(Some(h(b)), None, false); }
            LRESULT(0)
        }
        WM_NCHITTEST => {
            if ST.lock().unwrap().full { return LRESULT(HTCLIENT as isize); }
            let (x, y) = ((lp.0 & 0xffff) as i16 as i32, ((lp.0 >> 16) & 0xffff) as i16 as i32);
            let mut r = RECT::default(); let _ = GetWindowRect(hwnd, &mut r);
            let c = 28;
            if x > r.right - c && y > r.bottom - c { return LRESULT(HTBOTTOMRIGHT as isize); }
            if x < r.left + c && y > r.bottom - c { return LRESULT(HTBOTTOMLEFT as isize); }
            if x > r.right - c && y < r.top + c { return LRESULT(HTTOPRIGHT as isize); }
            if x < r.left + c && y < r.top + c { return LRESULT(HTTOPLEFT as isize); }
            LRESULT(HTCAPTION as isize)
        }
        WM_NCCALCSIZE if wp.0 != 0 => LRESULT(0),
        WM_NCLBUTTONDBLCLK => { let _ = PostMessageW(Some(hwnd), WM_FULL, WPARAM(0), LPARAM(0)); LRESULT(0) }
        WM_SIZING => {
            // aspect-locked resize from the corners
            let r = &mut *(lp.0 as *mut RECT);
            let a = aspect();
            let (w, _hh) = (r.right - r.left, r.bottom - r.top);
            let b = bezel(w, w);
            let vw = (w - 2 * b).max(120) as f64;
            let nh = (vw / a) as i32 + 2 * b;
            match wp.0 as u32 {
                WMSZ_TOPLEFT | WMSZ_TOPRIGHT | WMSZ_TOP => r.top = r.bottom - nh,
                _ => r.bottom = r.top + nh,
            }
            LRESULT(1)
        }
        WM_SIZE | WM_MOVE => { apply_shape(hwnd); LRESULT(0) }
        WM_GETMINMAXINFO => {
            let mi = &mut *(lp.0 as *mut MINMAXINFO);
            mi.ptMinTrackSize = POINT { x: 160, y: 160 };
            LRESULT(0)
        }
        WM_ERASEBKGND => {
            let dc = HDC(wp.0 as *mut _);
            let mut r = RECT::default(); let _ = GetClientRect(hwnd, &mut r);
            let br = CreateSolidBrush(COLORREF(if ST.lock().unwrap().full { 0 } else { 0x00100d0b }));
            FillRect(dc, &r, br);
            let _ = DeleteObject(br.into());
            LRESULT(1)
        }
        WM_TIMER => {
            // floating toolbar: fade in near the phone, fade out otherwise
            let b = BAR.load(Ordering::SeqCst);
            if b == 0 || !IsWindowVisible(hwnd).as_bool() { return LRESULT(0); }
            let mut p = POINT::default(); let _ = GetCursorPos(&mut p);
            let mut fr = RECT::default(); let _ = GetWindowRect(hwnd, &mut fr);
            let mut br = RECT::default(); let _ = GetWindowRect(h(b), &mut br);
            let near = (p.x >= fr.left - 24 && p.x <= fr.right + 24 && p.y >= fr.top - 70 && p.y <= fr.bottom + 24)
                || (p.x >= br.left && p.x <= br.right && p.y >= br.top && p.y <= br.bottom);
            let fg = GetForegroundWindow();
            let active = fg == hwnd || fg == h(b);
            let target = if near && (active || ST.lock().unwrap().top || true) { 245 } else { 0 };
            let a = { let mut s = ST.lock().unwrap(); s.alpha += ((target - s.alpha) as f64 * 0.35) as i32 + (target - s.alpha).signum() * 6;
                      s.alpha = s.alpha.clamp(0, 245); s.alpha };
            let _ = active;
            if a > 0 {
                if !IsWindowVisible(h(b)).as_bool() { place_bar(); let _ = ShowWindow(h(b), SW_SHOWNOACTIVATE); }
                let _ = SetLayeredWindowAttributes(h(b), COLORREF(0), a as u8, LWA_ALPHA);
            } else if IsWindowVisible(h(b)).as_bool() { let _ = ShowWindow(h(b), SW_HIDE); }
            LRESULT(0)
        }
        WM_CLOSE => { act("close"); LRESULT(0) }
        _ => DefWindowProcW(hwnd, msg, wp, lp),
    }
}

unsafe fn run() {
    let inst: HINSTANCE = GetModuleHandleW(None).unwrap_or_default().into();
    let icon = LoadIconW(Some(inst), PCWSTR(1 as *const u16)).unwrap_or_default();
    let wc = WNDCLASSEXW {
        cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32, style: CS_DBLCLKS, lpfnWndProc: Some(frame_proc),
        hInstance: inst, hIcon: icon, hCursor: LoadCursorW(None, IDC_ARROW).unwrap_or_default(),
        lpszClassName: w!("iMirrorPhone"), ..Default::default()
    };
    RegisterClassExW(&wc);
    let wb = WNDCLASSEXW {
        cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32, lpfnWndProc: Some(bar_proc), hInstance: inst,
        hCursor: LoadCursorW(None, IDC_HAND).unwrap_or_default(), lpszClassName: w!("iMirrorPhoneBar"), ..Default::default()
    };
    RegisterClassExW(&wb);
    let f = CreateWindowExW(WS_EX_APPWINDOW, w!("iMirrorPhone"), &HSTRING::from("iPhone — iMirror"),
                            WS_POPUP | WS_CLIPCHILDREN | WS_THICKFRAME, 100, 100, 400, 800, None, None, Some(inst), None)
        .unwrap_or_default();
    // Windows 11: no DWM border, dark mode
    let dark: BOOL = TRUE;
    let _ = DwmSetWindowAttribute(f, DWMWA_USE_IMMERSIVE_DARK_MODE, &dark as *const _ as _, 4);
    let none = DWMWCP_DONOTROUND;
    let _ = DwmSetWindowAttribute(f, DWMWA_WINDOW_CORNER_PREFERENCE, &none as *const _ as _, 4);
    let b = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE, w!("iMirrorPhoneBar"), w!("iMirror"),
                            WS_POPUP, 0, 0, BAR_W, BAR_H, Some(f), None, Some(inst), None).unwrap_or_default();
    let _ = SetLayeredWindowAttributes(b, COLORREF(0), 0, LWA_ALPHA);
    BAR.store(b.0 as isize, Ordering::SeqCst);
    FRAME.store(f.0 as isize, Ordering::SeqCst);
    SetTimer(Some(f), 1, 30, None);
    let mut m = MSG::default();
    while GetMessageW(&mut m, None, 0, 0).as_bool() {
        let _ = TranslateMessage(&m);
        DispatchMessageW(&m);
    }
}
