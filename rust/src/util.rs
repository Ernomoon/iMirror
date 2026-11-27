//! Logging, paths and small Windows helpers.
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

pub const CREATE_NO_WINDOW: u32 = 0x0800_0000;

static LOG: Mutex<Option<File>> = Mutex::new(None);

pub fn data_dir() -> PathBuf {
    let base = std::env::var("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(|_| std::env::temp_dir());
    let d = base.join("iMirror");
    let _ = std::fs::create_dir_all(&d);
    d
}

pub fn config_dir() -> PathBuf {
    let base = std::env::var("APPDATA").map(PathBuf::from).unwrap_or_else(|_| data_dir());
    let d = base.join("iMirror");
    let _ = std::fs::create_dir_all(&d);
    d
}

pub fn exe_dir() -> PathBuf {
    std::env::current_exe().ok().and_then(|p| p.parent().map(|p| p.to_path_buf())).unwrap_or_default()
}

pub fn now_hms() -> String {
    #[cfg(windows)]
    unsafe {
        let t = windows::Win32::System::SystemInformation::GetLocalTime();
        return format!("{:02}:{:02}:{:02}", t.wHour, t.wMinute, t.wSecond);
    }
    #[allow(unreachable_code)]
    String::from("00:00:00")
}

fn now_full() -> String {
    #[cfg(windows)]
    unsafe {
        let t = windows::Win32::System::SystemInformation::GetLocalTime();
        return format!("{:04}-{:02}-{:02} {:02}:{:02}:{:02},{:03}", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute,
                       t.wSecond, t.wMilliseconds);
    }
    #[allow(unreachable_code)]
    String::new()
}

pub fn log_init() {
    let p = data_dir().join("imirror.log");
    if std::fs::metadata(&p).map(|m| m.len() > 4_000_000).unwrap_or(false) {
        let _ = std::fs::rename(&p, data_dir().join("imirror.old.log"));
    }
    if let Ok(f) = OpenOptions::new().create(true).append(true).open(&p) {
        *LOG.lock().unwrap() = Some(f);
    }
}

pub fn log(msg: &str) {
    let line = format!("{} {}\n", now_full(), msg);
    if let Some(f) = LOG.lock().unwrap().as_mut() {
        let _ = f.write_all(line.as_bytes());
    }
}

#[macro_export]
macro_rules! log {
    ($($t:tt)*) => { $crate::util::log(&format!($($t)*)) };
}

/// Run a hidden command and return its stdout (lossy UTF-8) with a timeout.
pub fn run_hidden(exe: &str, args: &[&str], timeout: Duration) -> Option<String> {
    let mut cmd = Command::new(exe);
    cmd.args(args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = cmd.spawn().ok()?;
    let mut out = child.stdout.take()?;
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = std::io::Read::read_to_end(&mut out, &mut buf);
        let _ = tx.send(buf);
    });
    match rx.recv_timeout(timeout) {
        Ok(buf) => {
            let _ = child.wait();
            Some(String::from_utf8_lossy(&buf).into_owned())
        }
        Err(_) => {
            let _ = child.kill();
            None
        }
    }
}

pub fn powershell(script: &str, timeout: Duration) -> Option<String> {
    let full = format!("[Console]::OutputEncoding=[Text.Encoding]::UTF8;{script}");
    run_hidden("powershell.exe", &["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", &full], timeout)
}

/// Run PowerShell elevated (UAC prompt). Returns false if the user refused.
#[cfg(windows)]
pub fn powershell_elevated(script: &str) -> bool {
    use windows::core::{HSTRING, PCWSTR};
    use windows::Win32::UI::Shell::ShellExecuteW;
    use windows::Win32::UI::WindowsAndMessaging::SW_HIDE;
    let args = format!("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command \"{}\"", script.replace('"', "\\\""));
    let r = unsafe {
        ShellExecuteW(None, &HSTRING::from("runas"), &HSTRING::from("powershell.exe"), &HSTRING::from(args),
                      PCWSTR::null(), SW_HIDE)
    };
    r.0 as isize > 32
}

#[cfg(windows)]
pub fn open_url(url: &str) {
    use windows::core::{HSTRING, PCWSTR};
    use windows::Win32::UI::Shell::ShellExecuteW;
    use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return;
    }
    unsafe {
        ShellExecuteW(None, &HSTRING::from("open"), &HSTRING::from(url), PCWSTR::null(), PCWSTR::null(), SW_SHOWNORMAL);
    }
}

#[cfg(not(windows))]
pub fn powershell_elevated(_: &str) -> bool { false }
#[cfg(not(windows))]
pub fn open_url(_: &str) {}
