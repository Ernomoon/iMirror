//! Network adapters (GetAdaptersAddresses) and AirPlay announcement through Windows' own
//! mDNS responder (DnsServiceRegister), on the real Wi-Fi/Ethernet adapter only.
//! Approach from iPhoneMirror (GPL-3.0): works next to Bonjour and with VPNs enabled.
use serde::Serialize;
use std::ffi::c_void;
use std::sync::atomic::{AtomicU32, Ordering};
use windows::core::{HSTRING, PCWSTR};
use windows::Win32::Foundation::HANDLE;
use windows::Win32::NetworkManagement::Dns::*;
use windows::Win32::NetworkManagement::IpHelper::*;
use windows::Win32::Networking::WinSock::{AF_INET, SOCKADDR_IN};

const VIRTUAL: &[&str] = &[
    "virtual", "veth", "vmware", "hyper-v", "hyperv", "virtualbox", "tap", "vpn", "loopback", "tunnel", "wsl",
    "docker", "tailscale", "zerotier", "wireguard", "amnezia", "wintun", "openvpn", "radmin", "hamachi", "nordlynx",
    "outline", "happ", "sing-box", "clash", "v2ray", "xray", "npcap", "bluetooth", "teredo", "isatap", "fortinet",
    "anyconnect", "pangp", "proton", "warp", "cloudflare",
];

#[derive(Clone, Debug, Serialize)]
pub struct Adapter {
    pub index: u32,
    pub name: String,
    pub description: String,
    pub ip: String,
    pub prefix: u8,
    pub if_type: u32,
    pub gateway: bool,
    pub metric: u32,
    pub wifi: bool,
    pub virtual_: bool,
    pub reasons: Vec<String>,
    pub profile: String,
}

impl Adapter {
    pub fn lan(&self) -> bool {
        !self.virtual_
    }
}

fn wstr(p: windows::core::PWSTR) -> String {
    if p.is_null() { String::new() } else { unsafe { p.to_string().unwrap_or_default() } }
}

pub fn adapters() -> Vec<Adapter> {
    let mut out = Vec::new();
    let mut size: u32 = 32 * 1024;
    let mut buf: Vec<u8>;
    let mut tries = 0;
    loop {
        buf = vec![0u8; size as usize];
        let rc = unsafe {
            GetAdaptersAddresses(AF_INET.0 as u32, GAA_FLAG_INCLUDE_GATEWAYS, None,
                                 Some(buf.as_mut_ptr() as *mut IP_ADAPTER_ADDRESSES_LH), &mut size)
        };
        if rc == 0 { break; }
        tries += 1;
        if rc != 111 || tries > 3 { return out; }
    }
    let mut p = buf.as_ptr() as *const IP_ADAPTER_ADDRESSES_LH;
    while !p.is_null() {
        let a = unsafe { &*p };
        p = a.Next;
        if a.OperStatus.0 != 1 || a.IfType == 24 || a.IfType == 131 {   // up; not loopback/tunnel
            continue;
        }
        let name = wstr(a.FriendlyName);
        let description = wstr(a.Description);
        let low = format!("{name} {description}").to_lowercase();
        let hint = VIRTUAL.iter().find(|h| low.contains(*h));
        let physical = matches!(a.IfType, 6 | 71 | 243 | 244);
        let mut reasons = vec![];
        if let Some(h) = hint { reasons.push(format!("виртуальный адаптер или VPN ({h})")); }
        else if !physical { reasons.push("не физический адаптер".into()); }
        let mut u = a.FirstUnicastAddress;
        while !u.is_null() {
            let ua = unsafe { &*u };
            u = ua.Next;
            let sa = ua.Address.lpSockaddr;
            if sa.is_null() { continue; }
            if unsafe { (*sa).sa_family } != AF_INET { continue; }
            let sin = unsafe { &*(sa as *const SOCKADDR_IN) };
            let b = unsafe { sin.sin_addr.S_un.S_un_b };
            let ip = format!("{}.{}.{}.{}", b.s_b1, b.s_b2, b.s_b3, b.s_b4);
            if ip.starts_with("169.254.") { continue; }
            out.push(Adapter {
                index: unsafe { a.Anonymous1.Anonymous.IfIndex },
                name: name.clone(), description: description.clone(), ip, prefix: ua.OnLinkPrefixLength,
                if_type: a.IfType, gateway: !a.FirstGatewayAddress.is_null(), metric: a.Ipv4Metric,
                wifi: a.IfType == 71, virtual_: !reasons.is_empty(), reasons: reasons.clone(), profile: String::new(),
            });
        }
    }
    out
}

/// Best adapter for the iPhone: a real LAN adapter always wins over VPN/virtual ones.
pub fn best(list: &[Adapter], requested_ip: &str) -> Option<Adapter> {
    let lan: Vec<&Adapter> = list.iter().filter(|a| a.lan()).collect();
    let pool: Vec<&Adapter> = if lan.is_empty() { list.iter().collect() } else { lan };
    if let Some(a) = pool.iter().find(|a| !requested_ip.is_empty() && a.ip == requested_ip) {
        return Some((*a).clone());
    }
    let mut v = pool;
    v.sort_by_key(|a| (!a.gateway, !matches!(a.if_type, 6 | 71), a.metric, a.index));
    v.first().map(|a| (*a).clone())
}

pub fn network_profiles() -> std::collections::HashMap<u32, String> {
    let mut m = std::collections::HashMap::new();
    let out = crate::util::powershell("@(Get-NetConnectionProfile | Select-Object InterfaceIndex,@{n='c';e={[string]$_.NetworkCategory}}) | ConvertTo-Json -Compress",
                                      std::time::Duration::from_secs(20)).unwrap_or_default();
    if let Ok(v) = serde_json::from_str::<serde_json::Value>(out.trim()) {
        let arr = if v.is_array() { v.as_array().cloned().unwrap_or_default() } else { vec![v] };
        for e in arr {
            if let Some(i) = e.get("InterfaceIndex").and_then(|x| x.as_u64()) {
                m.insert(i as u32, e.get("c").and_then(|x| x.as_str()).unwrap_or("").to_string());
            }
        }
    }
    m
}

// ------------------------------------------------------------------ DNS-SD
#[derive(Clone, Debug)]
pub struct Service {
    pub kind: String,      // _airplay._tcp
    pub instance: String,  // full name: x._airplay._tcp.local
    pub port: u16,
    pub txt: Vec<(String, String)>,
}

pub fn parse_txt(raw: &[u8]) -> Vec<(String, String)> {
    let mut out = vec![];
    let mut i = 0;
    while i < raw.len() {
        let n = raw[i] as usize;
        let end = (i + 1 + n).min(raw.len());
        let item = String::from_utf8_lossy(&raw[i + 1..end]).to_string();
        i = end;
        if item.is_empty() { continue; }
        let (k, v) = item.split_once('=').unwrap_or((&item, ""));
        out.push((k.to_string(), v.to_string()));
    }
    out
}

pub fn parse_hex(s: &str) -> Vec<u8> {
    (0..s.len() / 2).filter_map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).ok()).collect()
}

fn host_name() -> String {
    use windows::Win32::System::SystemInformation::{ComputerNameDnsHostname, GetComputerNameExW};
    let mut size = 0u32;
    unsafe {
        let _ = GetComputerNameExW(ComputerNameDnsHostname, None, &mut size);
        let mut buf = vec![0u16; size as usize + 1];
        if GetComputerNameExW(ComputerNameDnsHostname, Some(windows::core::PWSTR(buf.as_mut_ptr())), &mut size).is_ok() {
            return String::from_utf16_lossy(&buf[..size as usize]) + ".local";
        }
    }
    "iMirror.local".into()
}

static DONE: AtomicU32 = AtomicU32::new(0);
static FAILED: AtomicU32 = AtomicU32::new(0);

unsafe extern "system" fn on_complete(status: u32, _ctx: *const c_void, inst: *const DNS_SERVICE_INSTANCE) {
    if status != 0 { FAILED.fetch_add(1, Ordering::SeqCst); }
    DONE.fetch_add(1, Ordering::SeqCst);
    if !inst.is_null() { DnsServiceFreeInstance(inst); }
}

unsafe extern "system" fn on_noop(_status: u32, _ctx: *const c_void, inst: *const DNS_SERVICE_INSTANCE) {
    if !inst.is_null() { DnsServiceFreeInstance(inst); }
}

struct Reg {
    req: Box<DNS_SERVICE_REGISTER_REQUEST>,
    _keys: Vec<HSTRING>,
    _vals: Vec<HSTRING>,
}
unsafe impl Send for Reg {}

#[derive(Default)]
pub struct Announcer {
    regs: Vec<Reg>,
    pub if_index: u32,
    pub ip: String,
}

impl Announcer {
    pub fn register(&mut self, services: &[Service], iface: &Adapter) -> Result<(), String> {
        self.stop();
        DONE.store(0, Ordering::SeqCst);
        FAILED.store(0, Ordering::SeqCst);
        let host = HSTRING::from(host_name());
        for s in services {
            let keys: Vec<HSTRING> = s.txt.iter().map(|(k, _)| HSTRING::from(k.as_str())).collect();
            let vals: Vec<HSTRING> = s.txt.iter().map(|(_, v)| HSTRING::from(v.as_str())).collect();
            let kp: Vec<PCWSTR> = keys.iter().map(|h| PCWSTR(h.as_ptr())).collect();
            let vp: Vec<PCWSTR> = vals.iter().map(|h| PCWSTR(h.as_ptr())).collect();
            let inst = unsafe {
                DnsServiceConstructInstance(&HSTRING::from(s.instance.as_str()), &host, None, None, s.port, 0, 0,
                                            kp.len() as u32, kp.as_ptr(), vp.as_ptr())
            };
            if inst.is_null() {
                return Err(format!("DnsServiceConstructInstance: {}", s.kind));
            }
            let req = Box::new(DNS_SERVICE_REGISTER_REQUEST {
                Version: 1,
                InterfaceIndex: iface.index,
                pServiceInstance: inst,
                pRegisterCompletionCallback: Some(on_complete),
                pQueryContext: std::ptr::null_mut(),
                hCredentials: HANDLE::default(),
                unicastEnabled: false.into(),
            });
            let rc = unsafe { DnsServiceRegister(&*req, None) };
            crate::log!("DnsServiceRegister {} on {} ({}) -> {}", s.instance, iface.name, iface.index, rc);
            self.regs.push(Reg { req, _keys: keys, _vals: vals });
            if rc != 9506 && rc != 0 {
                self.stop();
                return Err(format!("Windows DNS-SD вернул ошибку {rc}"));
            }
        }
        for _ in 0..80 {
            if DONE.load(Ordering::SeqCst) as usize >= services.len() { break; }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        if FAILED.load(Ordering::SeqCst) > 0 {
            self.stop();
            return Err("Windows DNS-SD отклонил регистрацию (имя занято?)".into());
        }
        self.if_index = iface.index;
        self.ip = iface.ip.clone();
        Ok(())
    }

    pub fn stop(&mut self) {
        for r in self.regs.drain(..) {
            let mut req = r.req;
            req.pRegisterCompletionCallback = Some(on_noop);
            unsafe { DnsServiceDeRegister(&*req, None); }
        }
        self.if_index = 0;
        self.ip.clear();
    }
}

pub fn dnssd_available() -> bool {
    unsafe {
        use windows::Win32::System::LibraryLoader::{GetProcAddress, LoadLibraryW};
        match LoadLibraryW(&HSTRING::from("dnsapi.dll")) {
            Ok(h) => GetProcAddress(h, windows::core::s!("DnsServiceRegister")).is_some(),
            Err(_) => false,
        }
    }
}
