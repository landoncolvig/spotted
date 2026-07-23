//! TelemetryDeck event reporter — opt-in, anonymous, fire-and-forget.
//!
//! Sends usage events to TelemetryDeck (free tier) so we can see what's
//! actually happening in real installs without giving up the privacy
//! positioning. Three guardrails:
//!
//! 1. **Off by default.** First-launch dialog asks; user picks. Choice is
//!    persisted in `config.json` next to the install ID.
//! 2. **No identifying data.** We hash the install UUID with a static
//!    salt before sending (`client_user_hash`). TelemetryDeck doesn't
//!    accept unhashed IDs by design.
//! 3. **No content.** Event payloads are categorical (counts, version,
//!    OS) — never file paths, names, clip contents, or face data.
//!
//! Configure with the `SPOTTED_TELEMETRY_APP_ID` env var at compile time
//! (set in CI before the release build). If unset, all telemetry is a
//! no-op no matter what the user picked — handy for local builds where
//! you don't want your dev launches polluting prod metrics.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::sync::OnceLock;

const TELEMETRY_ENDPOINT: &str = "https://nom.telemetrydeck.com/v2/";
const SALT: &str = "spotted-v1-2026"; // bump if we ever want to invalidate old hashes

/// Compile-time app ID baked in by CI. None = telemetry is a no-op
/// regardless of user opt-in (local dev builds without the env var set).
fn app_id() -> Option<&'static str> {
    option_env!("SPOTTED_TELEMETRY_APP_ID")
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Config {
    /// UUID generated once on first launch. Used internally; hashed
    /// before any network call.
    pub install_id: Option<String>,
    /// None = not yet asked, Some(true) = opted in, Some(false) = opted out.
    pub telemetry_enabled: Option<bool>,
}

fn config_dir() -> Option<PathBuf> {
    // ~/Library/Application Support/Spotted on macOS. Hand-rolled to
    // avoid pulling the `directories` crate just for one path.
    let home = std::env::var_os("HOME")?;
    let p = PathBuf::from(home).join("Library/Application Support/Spotted");
    fs::create_dir_all(&p).ok()?;
    Some(p)
}

fn config_path() -> Option<PathBuf> {
    Some(config_dir()?.join("config.json"))
}

pub fn load_config() -> Config {
    let Some(path) = config_path() else { return Config::default() };
    fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str::<Config>(&s).ok())
        .unwrap_or_default()
}

pub fn save_config(cfg: &Config) -> std::io::Result<()> {
    let Some(path) = config_path() else {
        return Err(std::io::Error::other("no config dir"));
    };
    let json = serde_json::to_string_pretty(cfg).map_err(std::io::Error::other)?;
    fs::write(path, json)
}

/// Generate-or-load the per-install UUID. Stable across launches; lost
/// only if the user deletes ~/Library/Application Support/Spotted/.
pub fn ensure_install_id() -> String {
    let mut cfg = load_config();
    if let Some(id) = &cfg.install_id {
        return id.clone();
    }
    let id = uuid::Uuid::new_v4().to_string();
    cfg.install_id = Some(id.clone());
    let _ = save_config(&cfg);
    id
}

/// SHA-256(install_id + salt), hex-encoded. Stable per install; opaque
/// to TelemetryDeck. The salt prevents anyone correlating Spotted user
/// IDs with the same UUIDs used by another app.
pub fn client_user_hash(install_id: &str) -> String {
    let mut h = Sha256::new();
    h.update(install_id.as_bytes());
    h.update(SALT.as_bytes());
    hex_encode(&h.finalize())
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

#[derive(Serialize, Clone)]
struct EventPayload {
    #[serde(rename = "appID")]
    app_id: String,
    #[serde(rename = "clientUser")]
    client_user: String,
    #[serde(rename = "sessionID")]
    session_id: String,
    #[serde(rename = "type")]
    type_: String,
    payload: Vec<String>,
}

fn session_id() -> &'static str {
    static SESSION: OnceLock<String> = OnceLock::new();
    SESSION.get_or_init(|| uuid::Uuid::new_v4().to_string())
}

/// Fire-and-forget event send. Spawns a Tokio task; returns immediately.
/// Becomes a no-op if telemetry isn't compiled in, the user hasn't
/// opted in, or there's no install ID yet.
pub fn track(event_name: &str, extra: HashMap<String, String>) {
    let Some(app_id) = app_id() else { return };
    let cfg = load_config();
    if cfg.telemetry_enabled != Some(true) {
        return;
    }
    let Some(install_id) = cfg.install_id else { return };
    let client_user = client_user_hash(&install_id);

    let mut payload: Vec<String> = base_payload()
        .into_iter()
        .map(|(k, v)| format!("{k}:{v}"))
        .collect();
    for (k, v) in extra {
        payload.push(format!("{k}:{v}"));
    }

    let event = EventPayload {
        app_id: app_id.to_string(),
        client_user,
        session_id: session_id().to_string(),
        type_: event_name.to_string(),
        payload,
    };
    tauri::async_runtime::spawn(async move {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build();
        let Ok(client) = client else { return };
        let _ = client
            .post(TELEMETRY_ENDPOINT)
            .json(&[event])
            .send()
            .await;
    });
}

/// Always-on metadata appended to every event: app version + OS so we
/// can slice usage by version (catch a regression that hits only one)
/// and by OS (when we add Windows support, know who's on what).
fn base_payload() -> Vec<(&'static str, String)> {
    vec![
        ("app_version", env!("CARGO_PKG_VERSION").to_string()),
        ("os", os_label()),
    ]
}

fn os_label() -> String {
    if cfg!(target_os = "macos") {
        "macos".into()
    } else if cfg!(target_os = "windows") {
        "windows".into()
    } else if cfg!(target_os = "linux") {
        "linux".into()
    } else {
        "other".into()
    }
}

/// Tauri commands exposed to the frontend so it can manage opt-in state.
pub mod cmds {
    use super::*;

    #[tauri::command]
    pub fn telemetry_state() -> Config {
        load_config()
    }

    #[tauri::command]
    pub fn set_telemetry_enabled(enabled: bool) -> Result<(), String> {
        let mut cfg = load_config();
        cfg.telemetry_enabled = Some(enabled);
        // Ensure we have an install ID — if the user opts in but
        // ensure_install_id hasn't run yet, we'd send no events.
        if cfg.install_id.is_none() {
            cfg.install_id = Some(uuid::Uuid::new_v4().to_string());
        }
        save_config(&cfg).map_err(|e| e.to_string())
    }

    /// One-shot from the frontend: track an event without round-tripping
    /// through a Rust call site. Used for UI-only signals (welcome
    /// dismissed, library opened, etc.).
    #[tauri::command]
    pub fn track_event(name: String, payload: HashMap<String, String>) {
        super::track(&name, payload);
    }

    #[tauri::command]
    pub fn telemetry_active() -> bool {
        // True only when the compile-time app ID is baked in AND the
        // user has opted in. Frontend uses this to gate the "telemetry
        // is enabled" indicator without leaking the app_id.
        app_id().is_some() && load_config().telemetry_enabled == Some(true)
    }
}

/// Idempotent: ensure install ID exists, return current config.
pub fn boot() -> Config {
    let _ = ensure_install_id();
    load_config()
}

/// Sentry DSN baked in by CI. None = error reporting is a no-op (the
/// sentry::init guard simply never gets installed). Same opt-in gate
/// as telemetry events — Sentry only sends if the user has opted in.
fn sentry_dsn() -> Option<&'static str> {
    option_env!("SPOTTED_SENTRY_DSN")
}

/// Initialize Sentry crash reporting. Returns the guard the caller must
/// hold for the lifetime of the app — dropping it flushes pending
/// events and disables the panic hook. Returns None if there's no DSN
/// baked in or the user hasn't opted into telemetry.
///
/// We piggy-back on the same opt-in flag as event telemetry so users
/// only see one consent dialog (privacy positioning stays clean) and
/// can toggle both with one switch.
pub fn init_sentry() -> Option<sentry::ClientInitGuard> {
    let dsn = sentry_dsn()?;
    let cfg = load_config();
    if cfg.telemetry_enabled != Some(true) {
        return None;
    }
    let guard = sentry::init((
        dsn,
        sentry::ClientOptions {
            release: Some(env!("CARGO_PKG_VERSION").into()),
            // Don't ship user data — Sentry has a "send_default_pii"
            // flag that defaults to false but we set it explicitly to
            // make intent unmistakable for anyone auditing.
            send_default_pii: false,
            // send_default_pii does NOT cover server_name, which the SDK
            // otherwise auto-fills from the OS hostname — routinely the user's
            // real name (e.g. "Ellies-MacBook-Pro"). Pin it to a constant so no
            // crash report ships the machine name.
            server_name: Some("anonymous".into()),
            // Anonymize sender by setting the user via the install hash,
            // not the raw IP / hostname Sentry would auto-collect.
            ..Default::default()
        },
    ));
    if let Some(id) = cfg.install_id {
        sentry::configure_scope(|scope| {
            scope.set_user(Some(sentry::User {
                id: Some(client_user_hash(&id)),
                ..Default::default()
            }));
        });
    }
    Some(guard)
}
