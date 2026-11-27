//! Settings (%APPDATA%\iMirror\config.json), compatible with iMirror 4.x.
use serde_json::{json, Map, Value};

pub fn defaults() -> Map<String, Value> {
    let v = json!({
        "server_name": "iMirror",
        "preset": "max4k",
        "width": 3840, "height": 2160, "refresh": 120, "max_fps": 120, "h265": true,
        "low_latency": true,
        "audio": true,
        "video_sink": "d3d12videosink",
        "software_decode": false,
        "always_on_top": false,
        "auto_fit": true,
        "pin": "",
        "forced_ip": "",
        "reduce_motion": false,
        "autostart": true,
        "device_id": "",
        "control_enabled": false,
        "phone_frame": true,
        "window_scale": 0.82
    });
    v.as_object().cloned().unwrap()
}

pub fn load() -> Map<String, Value> {
    let mut cfg = defaults();
    if let Ok(text) = std::fs::read_to_string(crate::util::config_dir().join("config.json")) {
        if let Ok(Value::Object(m)) = serde_json::from_str::<Value>(&text) {
            for (k, v) in m {
                if cfg.contains_key(&k) && std::mem::discriminant(&v) == std::mem::discriminant(&cfg[&k]) {
                    cfg.insert(k, v);
                } else if cfg.contains_key(&k) && cfg[&k].is_number() && v.is_number() {
                    cfg.insert(k, v);
                }
            }
        }
    }
    cfg
}

pub fn save(cfg: &Map<String, Value>) {
    let p = crate::util::config_dir().join("config.json");
    let tmp = p.with_extension("tmp");
    if std::fs::write(&tmp, serde_json::to_vec_pretty(cfg).unwrap_or_default()).is_ok() {
        let _ = std::fs::rename(&tmp, &p);
    }
}

pub fn s<'a>(cfg: &'a Map<String, Value>, k: &str) -> &'a str {
    cfg.get(k).and_then(|v| v.as_str()).unwrap_or("")
}
pub fn b(cfg: &Map<String, Value>, k: &str) -> bool {
    cfg.get(k).and_then(|v| v.as_bool()).unwrap_or(false)
}
pub fn i(cfg: &Map<String, Value>, k: &str) -> i64 {
    cfg.get(k).and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))).unwrap_or(0)
}
pub fn f(cfg: &Map<String, Value>, k: &str) -> f64 {
    cfg.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0)
}

pub const PRESETS: &[(&str, &str, i64, i64, i64, i64, bool)] = &[
    ("max4k", "Максимум: 4K H.265, запрос 120 Гц", 3840, 2160, 120, 120, true),
    ("1440p", "1440p H.265, запрос 120 Гц", 2560, 1440, 120, 120, true),
    ("1080p", "1080p H.264 (совместимость, старые iPhone)", 1920, 1080, 120, 120, false),
];
