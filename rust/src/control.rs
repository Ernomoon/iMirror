//! iPhone control over the PC's own Bluetooth: HID over GATT (keyboard + mouse + consumer +
//! navigation reports, layout from iPhoneMirror, GPL-3.0) and PC input capture.
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
use windows::core::GUID;
use windows::Devices::Bluetooth::BluetoothAdapter;
use windows::Devices::Bluetooth::GenericAttributeProfile::*;
use windows::Foundation::TypedEventHandler;
use windows::Storage::Streams::{DataWriter, IBuffer};

fn u16uuid(s: u16) -> GUID { GUID::from_values(s as u32, 0x0000, 0x1000, [0x80, 0x00, 0x00, 0x80, 0x5f, 0x9b, 0x34, 0xfb]) }

const REPORT_MAP: &[u8] = &[
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x75, 0x08, 0x95, 0x01, 0x81, 0x01, 0x75, 0x08, 0x95, 0x06, 0x15, 0x00, 0x25, 0x65,
    0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00, 0xC0,
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x02, 0x09, 0x01, 0xA1, 0x00, 0x05, 0x09, 0x19, 0x01, 0x29, 0x05, 0x15, 0x00,
    0x25, 0x01, 0x75, 0x01, 0x95, 0x05, 0x81, 0x02, 0x75, 0x03, 0x95, 0x01, 0x81, 0x01, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31,
    0x16, 0x01, 0x80, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02, 0x81, 0x06, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08,
    0x95, 0x01, 0x81, 0x06, 0x85, 0x03, 0x09, 0x48, 0x15, 0x00, 0x25, 0x0A, 0x75, 0x08, 0x95, 0x01, 0xB1, 0x02, 0xC0, 0xC0,
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x04, 0x15, 0x00, 0x26, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01,
    0x19, 0x00, 0x2A, 0xFF, 0x03, 0x81, 0x00, 0xC0,
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x05, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x0D,
    0x0A, 0x24, 0x02, 0x09, 0x40, 0x0A, 0x23, 0x02, 0x0A, 0xAE, 0x01, 0x0A, 0x21, 0x02, 0x81, 0x02,
    0x95, 0x03, 0x75, 0x01, 0x81, 0x03, 0xC0,
];
const NAV_MENU: u16 = 0x02;
const NAV_SEARCH: u16 = 0x10;
const MOTION_CAP: f64 = 1200.0;

fn buf(b: &[u8]) -> windows::core::Result<IBuffer> {
    let w = DataWriter::new()?;
    w.WriteBytes(b)?;
    w.DetachBuffer()
}

#[derive(Default)]
struct Shared {
    status: String,
    error: String,
    dx: f64, dy: f64, wheel: f64,
    buttons: u8,
    pending: VecDeque<(u8, Vec<u8>)>, // rid 0 = sleep(ms in data[0])
    subscribers: u32,
}

pub struct Hid {
    sh: Arc<Mutex<Shared>>,
    stop: Arc<AtomicBool>,
    running: AtomicBool,
    on_change: Arc<dyn Fn() + Send + Sync>,
}

impl Hid {
    fn set(&self, status: &str, err: &str) { set_status(&self.sh, status, err, &self.on_change); }
    pub fn connected(&self) -> bool { self.sh.lock().unwrap().status == "connected" }
    pub fn mv(&self, dx: f64, dy: f64) { let mut s = self.sh.lock().unwrap(); s.dx = (s.dx + dx).clamp(-MOTION_CAP, MOTION_CAP); s.dy = (s.dy + dy).clamp(-MOTION_CAP, MOTION_CAP); }
    pub fn buttons(&self, b: u8) { self.sh.lock().unwrap().buttons = b & 0x1f; }
    pub fn scroll(&self, steps: f64) { let mut s = self.sh.lock().unwrap(); s.wheel = (s.wheel + steps).clamp(-127.0, 127.0); }
    fn queue(&self, rid: u8, d: Vec<u8>) { let mut s = self.sh.lock().unwrap(); if s.pending.len() < 512 { s.pending.push_back((rid, d)); } }
    pub fn keys(&self, mods: u8, pressed: &[u8]) {
        let mut r = vec![mods, 0, 0, 0, 0, 0, 0, 0];
        for (i, k) in pressed.iter().take(6).enumerate() { r[2 + i] = *k; }
        self.queue(1, r);
    }
    pub fn consumer_tap(&self, usage: u16) { self.queue(4, usage.to_le_bytes().to_vec()); self.queue(4, vec![0, 0]); }
    pub fn nav_tap(&self, bits: u16, double: bool) {
        self.queue(5, bits.to_le_bytes().to_vec()); self.queue(5, vec![0, 0]);
        if double { self.queue(0, vec![100]); self.queue(5, bits.to_le_bytes().to_vec()); self.queue(5, vec![0, 0]); }
    }
    pub fn release_all(&self) {
        let mut s = self.sh.lock().unwrap();
        s.dx = 0.0; s.dy = 0.0; s.wheel = 0.0; s.buttons = 0;
        s.pending.push_back((1, vec![0; 8]));
    }
}

fn set_status(sh: &Arc<Mutex<Shared>>, status: &str, err: &str, cb: &Arc<dyn Fn() + Send + Sync>) {
    let changed = { let mut s = sh.lock().unwrap(); let c = s.status != status || s.error != err; s.status = status.into(); s.error = err.into(); c };
    if changed { crate::log!("bluetooth: {status} {err}"); cb(); }
}

fn ble_error(code: i32) -> String {
    match code {
        1 => "Bluetooth выключен (включите его в параметрах Windows)".into(),
        2 => "Bluetooth-адаптер занят другим приложением".into(),
        5 => "Windows запретила публикацию сервиса".into(),
        6 => "адаптер не поддерживает режим периферийного устройства".into(),
        7 => "Bluetooth отключён".into(),
        8 => "нет разрешения на Bluetooth (Параметры → Конфиденциальность → Bluetooth)".into(),
        9 => "адаптер не поддерживает Bluetooth LE".into(),
        c => format!("ошибка Bluetooth {c}"),
    }
}

fn run_ble(sh: Arc<Mutex<Shared>>, stop: Arc<AtomicBool>, cb: Arc<dyn Fn() + Send + Sync>) -> windows::core::Result<()> {
    let set = |a: &str, b: &str| set_status(&sh, a, b, &cb);
    let adapter = match BluetoothAdapter::GetDefaultAsync()?.get() {
        Ok(a) => a,
        Err(_) => { set("error", "на компьютере нет Bluetooth или он выключен"); return Ok(()); }
    };
    if !adapter.IsLowEnergySupported()? { set("error", &ble_error(9)); return Ok(()); }
    crate::log!("bluetooth adapter: peripheral={}", adapter.IsPeripheralRoleSupported()?);
    let res = GattServiceProvider::CreateAsync(u16uuid(0x1812))?.get()?;
    if res.Error()?.0 != 0 { set("error", &ble_error(res.Error()?.0)); return Ok(()); }
    let provider = res.ServiceProvider()?;
    let service = provider.Service()?;
    let enc = GattProtectionLevel::EncryptionRequired;
    let make = |uid: u16, props: GattCharacteristicProperties, value: &[u8]| -> windows::core::Result<GattLocalCharacteristic> {
        let p = GattLocalCharacteristicParameters::new()?;
        p.SetCharacteristicProperties(props)?;
        let readable = props.0 & GattCharacteristicProperties::Read.0 != 0;
        let writable = props.0 & (GattCharacteristicProperties::Write.0 | GattCharacteristicProperties::WriteWithoutResponse.0) != 0;
        p.SetReadProtectionLevel(if readable { enc } else { GattProtectionLevel::Plain })?;
        p.SetWriteProtectionLevel(if writable { enc } else { GattProtectionLevel::Plain })?;
        if readable { p.SetStaticValue(&buf(value)?)?; }
        let r = service.CreateCharacteristicAsync(u16uuid(uid), &p)?.get()?;
        if r.Error()?.0 != 0 { return Err(windows::core::Error::new(windows::core::HRESULT(-1), ble_error(r.Error()?.0))); }
        r.Characteristic()
    };
    let refdesc = |c: &GattLocalCharacteristic, rid: u8, kind: u8| -> windows::core::Result<()> {
        let d = GattLocalDescriptorParameters::new()?;
        d.SetReadProtectionLevel(enc)?;
        d.SetWriteProtectionLevel(enc)?;
        d.SetStaticValue(&buf(&[rid, kind])?)?;
        c.CreateDescriptorAsync(u16uuid(0x2908), &d)?.get()?;
        Ok(())
    };
    use GattCharacteristicProperties as P;
    let proto = make(0x2A4E, P(P::Read.0 | P::Write.0), &[1])?;
    let feature = make(0x2A4D, P(P::Read.0 | P::Write.0), &[0])?;
    refdesc(&feature, 3, 3)?;
    make(0x2A4A, P::Read, &[0x11, 0x01, 0x00, 0x03])?;
    make(0x2A4B, P::Read, REPORT_MAP)?;
    let ctrl = make(0x2A4C, P::WriteWithoutResponse, &[0])?;
    let notify = P(P::Read.0 | P::Notify.0);
    let kb = make(0x2A4D, notify, &[0; 8])?; refdesc(&kb, 1, 1)?;
    let mouse = make(0x2A4D, notify, &[0; 6])?; refdesc(&mouse, 2, 1)?;
    let cons = make(0x2A4D, notify, &[0; 2])?; refdesc(&cons, 4, 1)?;
    let nav = make(0x2A4D, notify, &[0; 2])?; refdesc(&nav, 5, 1)?;
    make(0x2A22, notify, &[0; 8])?;
    make(0x2A33, notify, &[0; 3])?;
    let handler = TypedEventHandler::<GattLocalCharacteristic, GattWriteRequestedEventArgs>::new(|_, args| {
        if let Some(a) = args.as_ref() {
            let d = a.GetDeferral()?;
            if let Ok(req) = a.GetRequestAsync().and_then(|o| o.get()) {
                if req.Option()? == GattWriteOption::WriteWithResponse { let _ = req.Respond(); }
            }
            d.Complete()?;
        }
        Ok(())
    });
    for c in [&proto, &feature, &ctrl] { c.WriteRequested(&handler)?; }
    let adv = GattServiceProviderAdvertisingParameters::new()?;
    adv.SetIsConnectable(true)?;
    adv.SetIsDiscoverable(true)?;
    provider.StartAdvertisingWithParameters(&adv)?;
    let mut ok = false;
    for _ in 0..60 {
        let st = provider.AdvertisementStatus()?;
        if st == GattServiceProviderAdvertisementStatus::Started || st == GattServiceProviderAdvertisementStatus::StartedWithoutAllAdvertisementData { ok = true; break; }
        if st == GattServiceProviderAdvertisementStatus::Aborted { break; }
        std::thread::sleep(Duration::from_millis(100));
    }
    if !ok { set("error", "Windows не дала включить Bluetooth-объявление (адаптер занят или не умеет режим периферии)"); return Ok(()); }
    set("waiting", "");
    let chars = [(1u8, kb), (2, mouse), (4, cons), (5, nav)];
    let subs = |rid: u8| chars.iter().find(|(r, _)| *r == rid).and_then(|(_, c)| c.SubscribedClients().ok()).and_then(|v| v.Size().ok()).unwrap_or(0);
    let send = |rid: u8, data: &[u8]| -> bool {
        if let Some((_, c)) = chars.iter().find(|(r, _)| *r == rid) {
            if let Ok(b) = buf(data) { return c.NotifyValueAsync(&b).and_then(|o| o.get()).is_ok(); }
        }
        false
    };
    let mut last_check = Instant::now() - Duration::from_secs(1);
    let mut sent_buttons = 0u8;
    while !stop.load(Ordering::SeqCst) {
        if last_check.elapsed() > Duration::from_millis(500) {
            last_check = Instant::now();
            let n = subs(2).max(subs(1));
            sh.lock().unwrap().subscribers = n;
            set(if n > 0 { "connected" } else { "waiting" }, "");
            let st = provider.AdvertisementStatus()?;
            if st == GattServiceProviderAdvertisementStatus::Stopped || st == GattServiceProviderAdvertisementStatus::Aborted {
                let _ = provider.StartAdvertisingWithParameters(&adv);
            }
        }
        let connected = sh.lock().unwrap().subscribers > 0;
        let pending: Vec<(u8, Vec<u8>)> = sh.lock().unwrap().pending.drain(..).collect();
        for (rid, d) in pending {
            if rid == 0 { std::thread::sleep(Duration::from_millis(d[0] as u64)); continue; }
            if connected { send(rid, &d); }
        }
        let (dx, dy, wh, b) = {
            let mut s = sh.lock().unwrap();
            let (dx, dy, wh) = (s.dx.trunc(), s.dy.trunc(), s.wheel.trunc());
            s.dx -= dx; s.dy -= dy; s.wheel -= wh;
            (dx as i32, dy as i32, wh as i32, s.buttons)
        };
        if connected && (dx != 0 || dy != 0 || wh != 0 || b != sent_buttons) {
            let (x, y) = (dx.clamp(-32767, 32767) as i16, dy.clamp(-32767, 32767) as i16);
            let rep = [b, x.to_le_bytes()[0], x.to_le_bytes()[1], y.to_le_bytes()[0], y.to_le_bytes()[1], (wh.clamp(-127, 127) as i8) as u8];
            if send(2, &rep) { sent_buttons = b; }
            let mut s = sh.lock().unwrap();
            s.dx = s.dx.clamp(-MOTION_CAP, MOTION_CAP); s.dy = s.dy.clamp(-MOTION_CAP, MOTION_CAP);
        }
        std::thread::sleep(Duration::from_millis(8));
    }
    let _ = provider.StopAdvertising();
    Ok(())
}

// ====================================================================== keymap
pub fn vk_to_hid(vk: u32, extended: bool) -> Option<u8> {
    let u = match vk {
        0x41..=0x5A => 0x04 + (vk - 0x41) as u8,
        0x31..=0x39 => 0x1E + (vk - 0x31) as u8,
        0x30 => 0x27, 0x0D => if extended { 0x58 } else { 0x28 }, 0x1B => 0x29, 0x08 => 0x2A, 0x09 => 0x2B, 0x20 => 0x2C,
        0xBD => 0x2D, 0xBB => 0x2E, 0xDB => 0x2F, 0xDD => 0x30, 0xDC => 0x31, 0xBA => 0x33, 0xDE => 0x34, 0xC0 => 0x35,
        0xBC => 0x36, 0xBE => 0x37, 0xBF => 0x38, 0x14 => 0x39, 0x70..=0x7B => 0x3A + (vk - 0x70) as u8,
        0x2C => 0x46, 0x91 => 0x47, 0x13 => 0x48, 0x2D => 0x49, 0x24 => 0x4A, 0x21 => 0x4B, 0x2E => 0x4C, 0x23 => 0x4D,
        0x22 => 0x4E, 0x27 => 0x4F, 0x25 => 0x50, 0x28 => 0x51, 0x26 => 0x52, 0x90 => 0x53,
        0x6F => 0x54, 0x6A => 0x55, 0x6D => 0x56, 0x6B => 0x57, 0x61..=0x69 => 0x59 + (vk - 0x61) as u8, 0x60 => 0x62, 0x6E => 0x63,
        0xE2 => 0x64, 0x5D => 0x65,
        0xA2 => 0xE0, 0xA0 => 0xE1, 0xA4 => 0xE2, 0x5B => 0xE3, 0xA3 => 0xE4, 0xA1 => 0xE5, 0xA5 => 0xE6, 0x5C => 0xE7,
        0x11 => if extended { 0xE4 } else { 0xE0 }, 0x10 => 0xE1, 0x12 => if extended { 0xE6 } else { 0xE2 },
        _ => return None,
    };
    Some(u)
}

pub fn vk_to_consumer(vk: u32) -> Option<u16> {
    Some(match vk { 0xAD => 0xE2, 0xAE => 0xEA, 0xAF => 0xE9, 0xB0 => 0xB5, 0xB1 => 0xB6, 0xB3 => 0xCD, _ => return None })
}

const RU: &str = "йцукенгшщзхъфывапролджэячсмитьбю";
const RU_KEYS: &str = "qwertyuiop[]asdfghjkl;'zxcvbnm,.";

/// Text → (modifiers, usage) strokes for the iPhone hardware keyboard (US or Russian layout).
pub fn text_to_strokes(text: &str) -> (Vec<(u8, u8)>, bool, bool, usize) {
    let shifted = "~!@#$%^&*()_+{}|:\"<>?";
    let base = "`1234567890-=[]\\;',./";
    let mut out = vec![];
    let (mut has_ru, mut has_en, mut skipped) = (false, false, 0);
    for ch in text.chars() {
        let (mut c, mut shift) = (ch, false);
        if let Some(i) = RU.chars().position(|r| r == ch.to_lowercase().next().unwrap_or(ch)) {
            has_ru = true; shift = ch.is_uppercase(); c = RU_KEYS.chars().nth(i).unwrap();
        } else if ch == 'ё' || ch == 'Ё' { has_ru = true; shift = ch == 'Ё'; c = '`'; }
        else if ch.is_ascii_alphabetic() { has_en = true; shift = ch.is_ascii_uppercase(); c = ch.to_ascii_lowercase(); }
        else if let Some(i) = shifted.find(ch) { shift = true; c = base.chars().nth(i).unwrap(); }
        let usage = match c {
            'a'..='z' => 0x04 + (c as u8 - b'a'), '1'..='9' => 0x1E + (c as u8 - b'1'), '0' => 0x27, '\n' => 0x28, '\t' => 0x2B, ' ' => 0x2C,
            '-' => 0x2D, '=' => 0x2E, '[' => 0x2F, ']' => 0x30, '\\' => 0x31, ';' => 0x33, '\'' => 0x34, '`' => 0x35, ',' => 0x36, '.' => 0x37, '/' => 0x38,
            _ => { skipped += 1; continue; }
        };
        out.push((if shift { 0x02 } else { 0 }, usage));
    }
    (out, has_ru, has_en, skipped)
}

// ====================================================================== capture
static CAPTURED: AtomicBool = AtomicBool::new(false);
static HID: OnceLock<Arc<Hid>> = OnceLock::new();
static ON_CAPTURE: OnceLock<Arc<dyn Fn(bool) + Send + Sync>> = OnceLock::new();
static PARK: Mutex<(i32, i32)> = Mutex::new((0, 0));
static INPUT_THREAD: AtomicIsize = AtomicIsize::new(0);
struct KeyState { down: Vec<u8>, mods: u8, buttons: u8, hot_down: bool, hot_clean: bool, hot_swallowed: bool, raw_seen: Option<Instant>, wheel: f64, swallow_up: bool }
static KS: Mutex<KeyState> = Mutex::new(KeyState { down: vec![], mods: 0, buttons: 0, hot_down: false, hot_clean: false, hot_swallowed: false, raw_seen: None, wheel: 0.0, swallow_up: false });

fn hid() -> Option<&'static Arc<Hid>> { HID.get() }

pub fn start_capture() -> bool {
    use windows::Win32::Foundation::{POINT, RECT};
    use windows::Win32::UI::WindowsAndMessaging::*;
    let Some(h) = hid() else { return false };
    if CAPTURED.load(Ordering::SeqCst) || !h.connected() { return false; }
    unsafe {
        let v = crate::mirror::video_hwnd();
        let mut p = POINT::default();
        if v != 0 {
            let mut r = RECT::default();
            let _ = GetWindowRect(windows::Win32::Foundation::HWND(v as *mut _), &mut r);
            p = POINT { x: (r.left + r.right) / 2, y: (r.top + r.bottom) / 2 };
        } else { let _ = GetCursorPos(&mut p); }
        *PARK.lock().unwrap() = (p.x, p.y);
        let _ = SetCursorPos(p.x, p.y);
    }
    { let mut k = KS.lock().unwrap(); k.down.clear(); k.mods = 0; k.buttons = 0; }
    h.release_all();
    CAPTURED.store(true, Ordering::SeqCst);
    if let Some(f) = ON_CAPTURE.get() { f(true); }
    true
}

pub fn release_capture() {
    if !CAPTURED.swap(false, Ordering::SeqCst) { return; }
    { let mut k = KS.lock().unwrap(); k.down.clear(); k.mods = 0; k.buttons = 0; }
    if let Some(h) = hid() { h.release_all(); }
    if let Some(f) = ON_CAPTURE.get() { f(false); }
}

fn send_keys(k: &KeyState) { if let Some(h) = hid() { h.keys(k.mods, &k.down); } }

unsafe extern "system" fn kbd_hook(code: i32, wp: windows::Win32::Foundation::WPARAM, lp: windows::Win32::Foundation::LPARAM) -> windows::Win32::Foundation::LRESULT {
    use windows::Win32::UI::WindowsAndMessaging::*;
    if code == HC_ACTION as i32 {
        let k = &*(lp.0 as *const KBDLLHOOKSTRUCT);
        if k.flags.0 & LLKHF_INJECTED.0 == 0 {
            let up = k.flags.0 & LLKHF_UP.0 != 0;
            let ext = k.flags.0 & LLKHF_EXTENDED.0 != 0;
            let vk = k.vkCode;
            let captured = CAPTURED.load(Ordering::SeqCst);
            if vk == 0xA3 {   // Right Ctrl toggles capture
                let mut st = KS.lock().unwrap();
                if !up {
                    if !st.hot_down { st.hot_down = true; st.hot_clean = true; st.hot_swallowed = captured; }
                    if st.hot_swallowed { return windows::Win32::Foundation::LRESULT(1); }
                } else {
                    st.hot_down = false;
                    let (clean, sw) = (st.hot_clean, st.hot_swallowed);
                    drop(st);
                    if clean { if captured { release_capture(); } else { start_capture(); } }
                    if sw { return windows::Win32::Foundation::LRESULT(1); }
                }
            } else {
                { let mut st = KS.lock().unwrap(); if st.hot_down { st.hot_clean = false; } }
                if captured {
                    if let Some(c) = vk_to_consumer(vk) { if !up { if let Some(h) = hid() { h.consumer_tap(c); } } return windows::Win32::Foundation::LRESULT(1); }
                    if vk == 0xA2 && k.scanCode & 0x200 != 0 { return windows::Win32::Foundation::LRESULT(1); }
                    if let Some(u) = vk_to_hid(vk, ext) {
                        let mut st = KS.lock().unwrap();
                        if (0xE0..=0xE7).contains(&u) {
                            // swap Ctrl and Win so Ctrl+C/V work like Cmd+C/V on the iPhone
                            let u2 = match u { 0xE0 => 0xE3, 0xE3 => 0xE0, 0xE4 => 0xE7, 0xE7 => 0xE4, x => x };
                            let bit = 1u8 << (u2 - 0xE0);
                            if up { st.mods &= !bit } else { st.mods |= bit }
                        } else if up { st.down.retain(|x| *x != u); } else if !st.down.contains(&u) { st.down.push(u); }
                        send_keys(&st);
                    }
                    return windows::Win32::Foundation::LRESULT(1);
                }
            }
        }
    }
    CallNextHookEx(None, code, wp, lp)
}

unsafe extern "system" fn mouse_hook(code: i32, wp: windows::Win32::Foundation::WPARAM, lp: windows::Win32::Foundation::LPARAM) -> windows::Win32::Foundation::LRESULT {
    use windows::Win32::UI::WindowsAndMessaging::*;
    if code == HC_ACTION as i32 {
        let m = &*(lp.0 as *const MSLLHOOKSTRUCT);
        let msg = wp.0 as u32;
        if m.flags & LLMHF_INJECTED == 0 {
            if KS.lock().unwrap().swallow_up && msg == WM_LBUTTONUP { KS.lock().unwrap().swallow_up = false; return windows::Win32::Foundation::LRESULT(1); }
            if CAPTURED.load(Ordering::SeqCst) {
                let h = hid().unwrap();
                let mut st = KS.lock().unwrap();
                match msg {
                    WM_MOUSEMOVE => {
                        if st.raw_seen.map(|t| t.elapsed() > Duration::from_millis(250)).unwrap_or(true) {
                            let p = *PARK.lock().unwrap();
                            h.mv((m.pt.x - p.0) as f64, (m.pt.y - p.1) as f64);
                        }
                    }
                    WM_LBUTTONDOWN => st.buttons |= 1, WM_LBUTTONUP => st.buttons &= !1,
                    WM_RBUTTONDOWN => st.buttons |= 2, WM_RBUTTONUP => st.buttons &= !2,
                    WM_MBUTTONDOWN => st.buttons |= 4, WM_MBUTTONUP => st.buttons &= !4,
                    WM_MOUSEWHEEL => {
                        st.wheel += ((m.mouseData >> 16) as i16) as f64 / 120.0;
                        let s = st.wheel.trunc();
                        if s != 0.0 { st.wheel -= s; h.scroll(s); }
                    }
                    _ => {}
                }
                h.buttons(st.buttons);
                return windows::Win32::Foundation::LRESULT(1);
            }
            if msg == WM_LBUTTONDOWN {
                let v = crate::mirror::video_hwnd();
                if v != 0 && hid().map(|h| h.connected()).unwrap_or(false) {
                    let under = WindowFromPoint(m.pt);
                    if under.0 as isize == v && start_capture() { KS.lock().unwrap().swallow_up = true; return windows::Win32::Foundation::LRESULT(1); }
                }
            }
        }
    }
    CallNextHookEx(None, code, wp, lp)
}

unsafe extern "system" fn raw_proc(hwnd: windows::Win32::Foundation::HWND, msg: u32, wp: windows::Win32::Foundation::WPARAM, lp: windows::Win32::Foundation::LPARAM) -> windows::Win32::Foundation::LRESULT {
    use windows::Win32::UI::Input::*;
    use windows::Win32::UI::WindowsAndMessaging::*;
    if msg == WM_INPUT && CAPTURED.load(Ordering::SeqCst) {
        let mut ri = RAWINPUT::default();
        let mut size = std::mem::size_of::<RAWINPUT>() as u32;
        let got = GetRawInputData(HRAWINPUT(lp.0 as *mut _), RID_INPUT, Some(&mut ri as *mut _ as *mut _), &mut size,
                                  std::mem::size_of::<RAWINPUTHEADER>() as u32);
        if got != u32::MAX && got > 0 && ri.header.dwType == RIM_TYPEMOUSE.0 {
            let m = ri.data.mouse;
            if m.usFlags.0 & 1 == 0 {
                KS.lock().unwrap().raw_seen = Some(Instant::now());
                if m.lLastX != 0 || m.lLastY != 0 { if let Some(h) = hid() { h.mv(m.lLastX as f64, m.lLastY as f64); } }
            }
        }
    }
    DefWindowProcW(hwnd, msg, wp, lp)
}

fn input_thread() {
    use windows::core::w;
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::UI::Input::*;
    use windows::Win32::UI::WindowsAndMessaging::*;
    unsafe {
        INPUT_THREAD.store(windows::Win32::System::Threading::GetCurrentThreadId() as isize, Ordering::SeqCst);
        let inst: windows::Win32::Foundation::HINSTANCE = GetModuleHandleW(None).unwrap_or_default().into();
        let wc = WNDCLASSW { lpfnWndProc: Some(raw_proc), hInstance: inst, lpszClassName: w!("iMirrorRaw"), ..Default::default() };
        RegisterClassW(&wc);
        if let Ok(hw) = CreateWindowExW(Default::default(), w!("iMirrorRaw"), w!(""), Default::default(), 0, 0, 0, 0, Some(HWND_MESSAGE), None, Some(inst), None) {
            let rid = RAWINPUTDEVICE { usUsagePage: 1, usUsage: 2, dwFlags: RIDEV_INPUTSINK, hwndTarget: hw };
            let _ = RegisterRawInputDevices(&[rid], std::mem::size_of::<RAWINPUTDEVICE>() as u32);
        }
        let _mh = SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_hook), Some(inst), 0);
        let _kh = SetWindowsHookExW(WH_KEYBOARD_LL, Some(kbd_hook), Some(inst), 0);
        let mut m = MSG::default();
        while GetMessageW(&mut m, None, 0, 0).as_bool() { let _ = TranslateMessage(&m); DispatchMessageW(&m); }
    }
}

// ====================================================================== facade
pub struct Control {
    hid: Arc<Hid>,
    typing: Arc<AtomicBool>,
    captured: Arc<AtomicBool>,
}

impl Control {
    pub fn new(on_change: Arc<dyn Fn() + Send + Sync>) -> Control {
        let hid = Arc::new(Hid { sh: Arc::new(Mutex::new(Shared { status: "off".into(), ..Default::default() })),
                                 stop: Arc::new(AtomicBool::new(false)), running: AtomicBool::new(false), on_change: on_change.clone() });
        let _ = HID.set(hid.clone());
        let captured = Arc::new(AtomicBool::new(false));
        let c2 = captured.clone();
        let cb = on_change.clone();
        let _ = ON_CAPTURE.set(Arc::new(move |v| { c2.store(v, Ordering::SeqCst); cb(); }));
        Control { hid, typing: Arc::new(AtomicBool::new(false)), captured }
    }

    pub fn state(&self) -> Value {
        let s = self.hid.sh.lock().unwrap();
        json!({"available": true, "status": s.status, "error": s.error, "adapter": "",
               "captured": self.captured.load(Ordering::SeqCst), "typing": self.typing.load(Ordering::SeqCst)})
    }

    fn need(&self) -> Result<(), String> { if self.hid.connected() { Ok(()) } else { Err("iPhone не подключён по Bluetooth".into()) } }

    pub fn dispatch(&self, op: &str, args: &Value) -> Result<Value, String> {
        match op {
            "on" => {
                if self.hid.running.swap(true, Ordering::SeqCst) { return Ok(json!({"ok": true})); }
                self.hid.stop.store(false, Ordering::SeqCst);
                self.hid.set("starting", "");
                let (sh, stop, cb, hid) = (self.hid.sh.clone(), self.hid.stop.clone(), self.hid.on_change.clone(), self.hid.clone());
                std::thread::Builder::new().name("bluetooth".into()).spawn(move || {
                    if let Err(e) = run_ble(sh.clone(), stop, cb.clone()) { set_status(&sh, "error", &e.message(), &cb); }
                    hid.running.store(false, Ordering::SeqCst);
                    if sh.lock().unwrap().status != "error" { set_status(&sh, "off", "", &cb); }
                }).map_err(|e| e.to_string())?;
                if INPUT_THREAD.load(Ordering::SeqCst) == 0 { std::thread::Builder::new().name("input".into()).spawn(input_thread).ok(); }
            }
            "off" => { release_capture(); self.hid.stop.store(true, Ordering::SeqCst); }
            "capture" => { if !self.hid.connected() { return Err("Сначала подключите iPhone по Bluetooth".into()); } start_capture(); }
            "release" => release_capture(),
            "home" => { self.need()?; self.hid.nav_tap(NAV_MENU, false); }
            "switcher" => { self.need()?; self.hid.nav_tap(NAV_MENU, true); }
            "search" => { self.need()?; self.hid.nav_tap(NAV_SEARCH, false); }
            "media" => {
                self.need()?;
                let u = match args["key"].as_str().unwrap_or("") { "vol_up" => 0xE9, "vol_down" => 0xEA, "mute" => 0xE2, "play" => 0xCD,
                                                                 "next" => 0xB5, "prev" => 0xB6, "lock" => 0x30, _ => return Err("?".into()) };
                self.hid.consumer_tap(u);
            }
            "type" => {
                self.need()?;
                if self.typing.swap(true, Ordering::SeqCst) { return Ok(json!({"ok": false})); }
                let text: String = args["text"].as_str().unwrap_or("").chars().take(4000).collect();
                let (strokes, ru, en, skipped) = text_to_strokes(&text);
                let (hid, typing) = (self.hid.clone(), self.typing.clone());
                let cb = self.hid.on_change.clone();
                std::thread::spawn(move || {
                    cb();
                    for (m, u) in strokes { hid.keys(m, &[u]); hid.keys(0, &[]); std::thread::sleep(Duration::from_millis(14)); }
                    typing.store(false, Ordering::SeqCst); cb();
                });
                let mut note = String::new();
                if ru && en { note = "Текст смешанный: раскладку на iPhone переключайте вручную (Globe)".into(); }
                else if ru { note = "Включите на iPhone русскую раскладку аппаратной клавиатуры".into(); }
                if skipped > 0 { note += &format!("{}Пропущено символов: {skipped}", if note.is_empty() { "" } else { ". " }); }
                return Ok(json!({"ok": true, "note": note}));
            }
            _ => return Err(format!("unknown control op {op}")),
        }
        Ok(json!({"ok": true}))
    }

    pub fn shutdown(&self) { release_capture(); self.hid.stop.store(true, Ordering::SeqCst); }
}
