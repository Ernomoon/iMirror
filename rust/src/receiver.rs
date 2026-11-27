//! UxPlay process: arguments, start/stop, output tail and parsing.
use crate::cfg;
use serde_json::{Map, Value};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub enum Ev {
    Line(String),
    Stage(String),
    Mdns { kind: String, port: u16, name: String, txt: Vec<u8> },
    Conn(i64),
    Video { codec: String, w: i64, h: i64 },
    Stats { fps: f64, kbps: f64 },
    Exited { code: i64, requested: bool, gen: u64 },
}

pub const PORT_SETS: &[([u16; 3], [u16; 3])] = &[
    ([7100, 7000, 7001], [6000, 6001, 7011]),
    ([7120, 7020, 7021], [6020, 6021, 7031]),
    ([7140, 7040, 7041], [6040, 6041, 7051]),
];

fn ports_free(tcp: &[u16; 3], udp: &[u16; 3]) -> bool {
    tcp.iter().all(|p| std::net::TcpListener::bind(("0.0.0.0", *p)).is_ok())
        && udp.iter().all(|p| std::net::UdpSocket::bind(("0.0.0.0", *p)).is_ok())
}

pub fn pick_ports() -> Option<([u16; 3], [u16; 3])> {
    PORT_SETS.iter().find(|(t, u)| ports_free(t, u)).cloned()
}

pub fn safe_name(name: &str) -> String {
    let mut s: String = name.split_whitespace().collect::<Vec<_>>().join("-");
    s.retain(|c| c.is_alphanumeric() || "-_.()[]".contains(c));
    let s = s.trim_matches(|c| c == '-' || c == '.').chars().take(48).collect::<String>();
    if s.is_empty() { "iMirror".into() } else { s }
}

pub fn device_id(cfg: &mut Map<String, Value>) -> String {
    let v = cfg::s(cfg, "device_id").to_string();
    let ok = v.len() == 17 && v.starts_with("02:") && v.chars().all(|c| c == ':' || c.is_ascii_hexdigit());
    if ok { return v; }
    let mut seed = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(7) as u64
        ^ (std::process::id() as u64) << 17;
    let mut parts = vec!["02".to_string()];
    for _ in 0..5 {
        seed ^= seed << 13; seed ^= seed >> 7; seed ^= seed << 17;
        parts.push(format!("{:02x}", (seed & 0xff) as u8));
    }
    let id = parts.join(":");
    cfg.insert("device_id".into(), Value::String(id.clone()));
    id
}

pub fn bundle_dir() -> PathBuf {
    crate::util::exe_dir().join("receiver")
}

pub fn find_uxplay() -> Option<PathBuf> {
    let p = bundle_dir().join("bin").join("uxplay.exe");
    if p.is_file() { Some(p) } else { None }
}

pub fn build_args(cfg: &mut Map<String, Value>, minimal: bool) -> Vec<String> {
    let mut a: Vec<String> = vec!["-n".into(), safe_name(cfg::s(cfg, "server_name")), "-nh".into(), "-m".into(), device_id(cfg)];
    if let Some((t, u)) = pick_ports() {
        a.extend(["-p".into(), "tcp".into(), t.map(|x| x.to_string()).join(","), "-p".into(), "udp".into(),
                  u.map(|x| x.to_string()).join(",")]);
    }
    if minimal {
        a.extend(["-as".into(), "0".into(), "-vs".into(), "autovideosink".into()]);
        return a;
    }
    let (w, h) = (cfg::i(cfg, "width").clamp(320, 9999), cfg::i(cfg, "height").clamp(240, 9999));
    let (r, fps) = (cfg::i(cfg, "refresh").clamp(1, 255), cfg::i(cfg, "max_fps").clamp(1, 255));
    a.extend([format!("-s"), format!("{w}x{h}@{r}"), "-fps".into(), fps.to_string(), "-nohold".into(), "-nofreeze".into()]);
    if cfg::b(cfg, "h265") { a.push("-h265".into()); }
    if cfg::b(cfg, "low_latency") { a.extend(["-vsync".into(), "no".into()]); }
    if cfg::b(cfg, "audio") { a.extend(["-as".into(), "wasapisink".into()]); } else { a.extend(["-as".into(), "0".into()]); }
    let sink = cfg::s(cfg, "video_sink");
    a.extend(["-vs".into(), if sink.is_empty() { "d3d12videosink".into() } else { sink.to_string() }]);
    if cfg::b(cfg, "software_decode") { a.push("-avdec".into()); }
    let pin = cfg::s(cfg, "pin");
    if pin.len() == 4 && pin.chars().all(|c| c.is_ascii_digit()) { a.extend(["-pin".into(), pin.to_string()]); }
    a
}

fn env_for(exe: &Path) -> Vec<(String, String)> {
    let bin = exe.parent().unwrap().to_path_buf();
    let root = bin.parent().unwrap().to_path_buf();
    let sys32 = std::env::var("SystemRoot").unwrap_or("C:\\Windows".into()) + "\\System32";
    let mut env: Vec<(String, String)> = std::env::vars()
        .filter(|(k, _)| { let u = k.to_uppercase(); !(u.starts_with("GST_") || u.starts_with("GIO_") || u.starts_with("GTK_") || u == "PATH") })
        .collect();
    env.push(("PATH".into(), format!("{};{};{}", bin.display(), sys32, std::env::var("PATH").unwrap_or_default())));
    let plugins = root.join("lib").join("gstreamer-1.0");
    env.push(("GST_PLUGIN_SYSTEM_PATH_1_0".into(), plugins.display().to_string()));
    env.push(("GST_PLUGIN_SYSTEM_PATH".into(), plugins.display().to_string()));
    let scanner = root.join("libexec").join("gstreamer-1.0").join("gst-plugin-scanner.exe");
    env.push(("GST_PLUGIN_SCANNER_1_0".into(), scanner.display().to_string()));
    env.push(("GST_PLUGIN_SCANNER".into(), scanner.display().to_string()));
    // one plugin cache per receiver build
    let tag = std::fs::metadata(exe).map(|m| format!("{:x}-{:x}", m.len(),
        m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs()).unwrap_or(0)))
        .unwrap_or_else(|_| "x".into());
    let dir = crate::util::data_dir();
    let reg = dir.join(format!("gst-registry-{tag}.bin"));
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for e in rd.flatten() {
            let n = e.file_name().to_string_lossy().to_string();
            if n.starts_with("gst-registry") && n.ends_with(".bin") && e.path() != reg { let _ = std::fs::remove_file(e.path()); }
        }
    }
    env.push(("GST_REGISTRY_1_0".into(), reg.display().to_string()));
    env.push(("IMIRROR_EXTERNAL_MDNS".into(), "1".into()));
    env
}

pub struct Receiver {
    child: Option<Child>,
    pub gen: u64,
    pub stopping: Arc<Mutex<bool>>,
    pub tail: Arc<Mutex<Vec<String>>>,
    pub pid: u32,
}

impl Receiver {
    pub fn new() -> Self {
        Receiver { child: None, gen: 0, stopping: Arc::new(Mutex::new(false)), tail: Arc::new(Mutex::new(vec![])), pid: 0 }
    }

    pub fn running(&mut self) -> bool {
        match self.child.as_mut() { Some(c) => matches!(c.try_wait(), Ok(None)), None => false }
    }

    pub fn start(&mut self, exe: &Path, args: &[String], tx: Sender<Ev>) -> std::io::Result<()> {
        use std::os::windows::process::CommandExt;
        let out_path = crate::util::data_dir().join("uxplay-output.log");
        let out = std::fs::File::create(&out_path)?;
        let err = out.try_clone()?;
        let mut cmd = Command::new(exe);
        cmd.args(args).stdin(Stdio::null()).stdout(out).stderr(err).current_dir(exe.parent().unwrap())
            .env_clear().envs(env_for(exe)).creation_flags(crate::util::CREATE_NO_WINDOW | 0x0000_8000 /* ABOVE_NORMAL */);
        let child = cmd.spawn()?;
        self.pid = child.id();
        self.gen += 1;
        *self.stopping.lock().unwrap() = false;
        self.tail.lock().unwrap().clear();
        let gen = self.gen;
        let stopping = self.stopping.clone();
        let tail = self.tail.clone();
        let pid = child.id();
        self.child = Some(child);
        std::thread::Builder::new().name("uxplay-out".into()).spawn(move || {
            tail_output(out_path, pid, gen, stopping, tail, tx);
        })?;
        Ok(())
    }

    pub fn stop(&mut self) {
        *self.stopping.lock().unwrap() = true;
        if let Some(mut c) = self.child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
        self.pid = 0;
    }
}

fn process_alive(pid: u32) -> Option<i64> {
    use windows::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows::Win32::System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let mut code = 0u32;
        let ok = GetExitCodeProcess(h, &mut code).is_ok();
        let _ = CloseHandle(h);
        if ok && code != STILL_ACTIVE.0 as u32 { Some(code as i64) } else { None }
    }
}

fn tail_output(path: PathBuf, pid: u32, gen: u64, stopping: Arc<Mutex<bool>>, tail: Arc<Mutex<Vec<String>>>, tx: Sender<Ev>) {
    let mut f = match std::fs::File::open(&path) { Ok(f) => f, Err(_) => return };
    let mut buf: Vec<u8> = vec![];
    let mut pos = 0u64;
    let mut chunk = vec![0u8; 65536];
    let mut finished: Option<i64> = None;
    loop {
        let _ = f.seek(SeekFrom::Start(pos));
        let n = f.read(&mut chunk).unwrap_or(0);
        if n > 0 {
            pos += n as u64;
            buf.extend_from_slice(&chunk[..n]);
            while let Some(i) = buf.iter().position(|&b| b == b'\n') {
                let line: Vec<u8> = buf.drain(..=i).collect();
                let line = String::from_utf8_lossy(&line).trim_end_matches(['\r', '\n']).to_string();
                {
                    let mut t = tail.lock().unwrap();
                    t.push(line.chars().take(300).collect());
                    let excess = t.len().saturating_sub(40);
                    t.drain(..excess);
                }
                if let Some(ev) = parse(&line) { let _ = tx.send(ev); }
            }
            continue;
        }
        if let Some(code) = finished {
            let requested = *stopping.lock().unwrap();
            let _ = tx.send(Ev::Exited { code, requested, gen });
            return;
        }
        if let Some(code) = process_alive(pid) { finished = Some(code); continue; }
        if *stopping.lock().unwrap() && process_alive(pid).is_none() {
            // killed and handle gone
            std::thread::sleep(Duration::from_millis(30));
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}

fn kv<'a>(line: &'a str, key: &str) -> Option<&'a str> {
    let i = line.find(&format!("{key}="))?;
    let rest = &line[i + key.len() + 1..];
    Some(rest.split(' ').next().unwrap_or(""))
}

pub fn parse(line: &str) -> Option<Ev> {
    if let Some(r) = line.strip_prefix("IMIRROR_STAGE ") { return Some(Ev::Stage(r.trim().into())); }
    if line.starts_with("IMIRROR_MDNS ") {
        let kind = kv(line, "type")?.to_string();
        let port = kv(line, "port")?.parse().ok()?;
        let ni = line.find(" name=")? + 6;
        let ti = line.rfind(" txt=")?;
        let name = line[ni..ti].to_string();
        let txt = crate::net::parse_hex(line[ti + 5..].trim());
        return Some(Ev::Mdns { kind, port, name, txt });
    }
    if line.starts_with("IMIRROR_CONN ") { return Some(Ev::Conn(kv(line, "open")?.trim().parse().ok()?)); }
    if line.starts_with("IMIRROR_VIDEO ") {
        return Some(Ev::Video { codec: kv(line, "codec")?.into(), w: kv(line, "w")?.parse().ok()?, h: kv(line, "h")?.trim().parse().ok()? });
    }
    if line.starts_with("IMIRROR_STATS ") {
        return Some(Ev::Stats { fps: kv(line, "fps")?.parse().ok()?, kbps: kv(line, "kbps")?.parse().ok()? });
    }
    if line.trim().is_empty() { return None; }
    Some(Ev::Line(line.to_string()))
}
