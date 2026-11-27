//! Network self-check and one-click fixes.
use crate::net;
use serde_json::{json, Value};
use std::time::Duration;

const RULE_APP: &str = "iMirror";
const RULE_RX: &str = "iMirror AirPlay";
const RULE_PORTS: &str = "iMirror AirPlay ports";
const TCP_PORTS: &str = "7000,7001,7100,7020,7021,7120,7040,7041,7140";
const UDP_PORTS: &str = "5353,6000,6001,7011,6020,6021,7031,6040,6041,7051";

fn firewall_rules() -> std::collections::HashMap<String, bool> {
    let mut m = std::collections::HashMap::new();
    let out = crate::util::powershell(&format!(
        "@(Get-NetFirewallRule -DisplayName '{RULE_APP}','{RULE_RX}','{RULE_PORTS}' -ErrorAction SilentlyContinue | \
         Select-Object DisplayName,@{{n='e';e={{[string]$_.Enabled}}}},@{{n='a';e={{[string]$_.Action}}}},@{{n='d';e={{[string]$_.Direction}}}}) | ConvertTo-Json -Compress"),
        Duration::from_secs(20)).unwrap_or_default();
    if let Ok(v) = serde_json::from_str::<Value>(out.trim()) {
        let arr = if v.is_array() { v.as_array().cloned().unwrap_or_default() } else { vec![v] };
        for r in arr {
            let ok = r["e"] == "True" && r["a"] == "Allow" && r["d"] == "Inbound";
            let n = r["DisplayName"].as_str().unwrap_or("").to_string();
            let prev = *m.get(&n).unwrap_or(&false);
            m.insert(n, prev || ok);
        }
    }
    m
}

fn port_listening(port: u16) -> bool {
    let out = crate::util::run_hidden("netstat", &["-an", "-p", "TCP"], Duration::from_secs(10)).unwrap_or_default();
    out.lines().any(|l| l.contains(&format!(":{port} ")) && (l.contains("LISTEN") || l.contains("ПРОСЛУШ")))
}

pub fn run_checks(running: bool, port: u16, mdns: &Value) -> Vec<Value> {
    let mut c = vec![];
    let mut adapters = net::adapters();
    let prof = net::network_profiles();
    for a in adapters.iter_mut() { a.profile = prof.get(&a.index).cloned().unwrap_or_default(); }
    let lan: Vec<&net::Adapter> = adapters.iter().filter(|a| a.lan()).collect();
    let vpn: Vec<&net::Adapter> = adapters.iter().filter(|a| a.virtual_ && a.gateway).collect();
    if let Some(a) = lan.first() {
        let more = if lan.len() > 1 { format!(" и ещё {}", lan.len() - 1) } else { String::new() };
        c.push(json!({"id": "lan", "title": "Подключение к сети", "state": "ok",
            "detail": format!("{}: {}, адрес {}{more}", if a.wifi { "Wi-Fi" } else { "Кабель" }, a.name, a.ip)}));
    } else {
        c.push(json!({"id": "lan", "title": "Подключение к сети", "state": "bad",
            "detail": "Не найдено ни Wi-Fi, ни кабеля. Подключите компьютер к той же сети, что и iPhone."}));
    }
    if vpn.is_empty() {
        c.push(json!({"id": "vpn", "title": "VPN на компьютере", "state": "ok", "detail": "Не мешает"}));
    } else {
        let mut names: Vec<String> = vpn.iter().map(|a| a.name.clone()).collect();
        names.sort(); names.dedup();
        c.push(json!({"id": "vpn", "title": "VPN на компьютере", "state": "ok",
            "detail": format!("{}: iMirror объявляется мимо VPN, через {}", names.join(", "),
                lan.first().map(|a| a.name.as_str()).unwrap_or("локальную сеть"))}));
    }
    let public: Vec<&&net::Adapter> = lan.iter().filter(|a| a.profile.eq_ignore_ascii_case("public")).collect();
    if let Some(p) = public.first() {
        let idx: Vec<String> = public.iter().map(|a| a.index.to_string()).collect();
        c.push(json!({"id": "profile", "title": "Тип сети Windows", "state": "bad",
            "detail": format!("Сеть «{}» помечена как общедоступная: Windows прячет компьютер от других устройств.", p.name),
            "fix": "profile", "fix_title": "Сделать частной", "fix_arg": idx.join(",")}));
    } else if lan.iter().any(|a| !a.profile.is_empty()) {
        c.push(json!({"id": "profile", "title": "Тип сети Windows", "state": "ok", "detail": "Частная сеть"}));
    }
    let rules = firewall_rules();
    if *rules.get(RULE_RX).unwrap_or(&false) && *rules.get(RULE_PORTS).unwrap_or(&false) {
        c.push(json!({"id": "fw", "title": "Брандмауэр Windows", "state": "ok", "detail": "iMirror разрешён"}));
    } else {
        c.push(json!({"id": "fw", "title": "Брандмауэр Windows", "state": "bad",
            "detail": "Нет разрешающего правила для iMirror: iPhone не сможет подключиться.", "fix": "firewall", "fix_title": "Разрешить"}));
    }
    if net::dnssd_available() {
        c.push(json!({"id": "bonjour", "title": "Служба объявления", "state": "ok", "detail": "Встроенная в Windows, Bonjour не нужен"}));
    } else {
        c.push(json!({"id": "bonjour", "title": "Служба объявления", "state": "bad",
            "detail": "Нужна Windows 10 версии 1809 или новее."}));
    }
    if running && port > 0 {
        let l = port_listening(port);
        c.push(json!({"id": "port", "title": "Приёмник AirPlay", "state": if l { "ok" } else { "bad" },
            "detail": if l { format!("Принимает подключения на порту {port}") } else { format!("Порт {port} не отвечает") }}));
        match mdns["ok"].as_bool() {
            Some(true) => c.push(json!({"id": "mdns", "title": "Виден в «Повторе экрана»", "state": "ok",
                "detail": format!("Объявлен через {}: {}", mdns["iface"].as_str().unwrap_or("сеть"),
                    mdns["addresses"].as_array().map(|a| a.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(", ")).unwrap_or_default())})),
            Some(false) => c.push(json!({"id": "mdns", "title": "Виден в «Повторе экрана»", "state": "bad",
                "detail": mdns["error"], "fix": "reannounce", "fix_title": "Повторить"})),
            None => c.push(json!({"id": "mdns", "title": "Виден в «Повторе экрана»", "state": "info", "detail": "Объявляю…"})),
        }
    } else if running {
        c.push(json!({"id": "port", "title": "Приёмник AirPlay", "state": "info", "detail": "Запускается…"}));
    } else {
        c.push(json!({"id": "port", "title": "Приёмник AirPlay", "state": "info", "detail": "Выключен"}));
    }
    c
}

pub fn apply_fix(fix: &str, arg: &str) -> bool {
    let app = std::env::current_exe().map(|p| p.display().to_string()).unwrap_or_default();
    let ux = crate::receiver::find_uxplay().map(|p| p.display().to_string()).unwrap_or_default();
    let script = match fix {
        "firewall" => format!(
            "Remove-NetFirewallRule -DisplayName '{RULE_APP}','{RULE_RX}','{RULE_PORTS}' -ErrorAction SilentlyContinue; \
             New-NetFirewallRule -DisplayName '{RULE_APP}' -Direction Inbound -Action Allow -Profile Any -Program '{app}'; \
             New-NetFirewallRule -DisplayName '{RULE_RX}' -Direction Inbound -Action Allow -Profile Any -Program '{ux}'; \
             New-NetFirewallRule -DisplayName '{RULE_PORTS}' -Direction Inbound -Action Allow -Profile Any -Protocol TCP -LocalPort {TCP_PORTS}; \
             New-NetFirewallRule -DisplayName '{RULE_PORTS}' -Direction Inbound -Action Allow -Profile Any -Protocol UDP -LocalPort {UDP_PORTS}"),
        "profile" => {
            let idx: Vec<&str> = arg.split(',').filter(|s| s.chars().all(|c| c.is_ascii_digit()) && !s.is_empty()).collect();
            if idx.is_empty() { return false; }
            format!("Set-NetConnectionProfile -InterfaceIndex {} -NetworkCategory Private", idx.join(","))
        }
        _ => return false,
    };
    crate::util::powershell_elevated(&script)
}
