use serde::Serialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, Window};
use tauri_plugin_shell::process::{Command, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

mod telemetry;

/// Shared state: PIDs of currently-running sidecar processes. Cancel
/// command signals all of them so a single click stops whatever phase
/// the pipeline is in.
#[derive(Default)]
struct ActiveChildren(Arc<Mutex<Vec<u32>>>);

/// PID of the label-web server currently holding the labeler port, if any.
/// A new batch tears this down before starting its own server so it can bind
/// the port and the readiness poll can't get a 200 from the stale (previous
/// batch's) server.
#[derive(Default)]
struct LabelServer(Arc<Mutex<Option<u32>>>);
#[derive(Default)]
struct LabelToken(Arc<Mutex<String>>);

#[derive(Serialize, Clone)]
struct SidecarLine {
    kind: &'static str,
    line: String,
}

#[derive(Serialize, Clone)]
struct UpdaterAvailable {
    version: String,
    current_version: String,
}

#[derive(Serialize, Clone)]
struct UpdaterProgress {
    downloaded: u64,
    total: Option<u64>,
}

#[derive(Serialize, Clone)]
struct UpdaterMessage {
    message: String,
}

/// Give every frozen sidecar process a writable extraction root owned by
/// Spotted. PyInstaller's one-file bootloader unpacks hundreds of megabytes
/// before Python starts. Inheriting macOS's per-login TMPDIR made that startup
/// depend on an external directory that can disappear or become unwritable
/// while Spotted is open.
fn prepare_sidecar_temp_dir(cache_root: &Path) -> Result<PathBuf, String> {
    let temp_dir = cache_root.join("sidecar-tmp");
    std::fs::create_dir_all(&temp_dir).map_err(|e| {
        format!(
            "couldn't prepare Spotted's local working folder {}: {e}",
            temp_dir.display()
        )
    })?;

    // create_dir_all succeeds when the directory already exists, so prove the
    // current user can create and remove files before starting PyInstaller.
    let probe = temp_dir.join(format!(
        ".write-probe-{}",
        uuid::Uuid::new_v4().simple()
    ));
    std::fs::write(&probe, b"spotted").map_err(|e| {
        format!(
            "Spotted's local working folder isn't writable {}: {e}",
            temp_dir.display()
        )
    })?;
    std::fs::remove_file(&probe).map_err(|e| {
        format!(
            "Spotted's local working folder can't remove temporary files {}: {e}",
            temp_dir.display()
        )
    })?;
    Ok(temp_dir)
}

/// Build the one approved sidecar command. All launch sites go through this
/// helper so none can accidentally inherit a broken system temporary path.
fn sidecar_command(app: &AppHandle) -> Result<Command, String> {
    let cache_root = app
        .path()
        .app_cache_dir()
        .map_err(|e| format!("couldn't resolve Spotted's local cache: {e}"))?;
    let temp_dir = prepare_sidecar_temp_dir(&cache_root)?;
    let sidecar = app
        .shell()
        .sidecar("spotted-sidecar")
        .map_err(|e| format!("sidecar lookup: {e}"))?;

    Ok(sidecar
        .env("TMPDIR", &temp_dir)
        .env("TEMP", &temp_dir)
        .env("TMP", &temp_dir))
}

/// Spawn the sidecar, stream its output as `sidecar://line` events on the
/// window, await the child to exit, return its exit code.
async fn run_sidecar(
    app: AppHandle,
    window: Window,
    args: Vec<String>,
) -> Result<i32, String> {
    let sidecar = sidecar_command(&app)?;

    let (mut rx, _child) = sidecar
        .args(args)
        .spawn()
        .map_err(|e| format!("sidecar spawn: {e}"))?;

    // Register the child PID so cancel_work can find it.
    let pid = _child.pid();
    if let Some(state) = app.try_state::<ActiveChildren>() {
        if let Ok(mut v) = state.0.lock() {
            v.push(pid);
        }
    }
    // Defer unregistration to a scope-exit guard so we always clean up
    // even on early return.
    struct PidGuard {
        pid: u32,
        bucket: Arc<Mutex<Vec<u32>>>,
    }
    impl Drop for PidGuard {
        fn drop(&mut self) {
            if let Ok(mut v) = self.bucket.lock() {
                v.retain(|p| *p != self.pid);
            }
        }
    }
    let _guard = app
        .try_state::<ActiveChildren>()
        .map(|s| PidGuard { pid, bucket: s.0.clone() });

    let mut code = -1i32;
    // Tail of BOTH streams. facetag prints error messages via rich.console
    // which goes to stdout, so stderr-only capture missed them in v0.0.7.
    let mut last_lines: Vec<String> = Vec::new();
    let push_line = |buf: &mut Vec<String>, line: String| {
        buf.push(line);
        if buf.len() > 8 {
            buf.remove(0);
        }
    };

    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if !line.is_empty() {
                    // Skip structured events and rich progress-bar lines —
                    // they're noise in user-facing error reports.
                    if !line.starts_with("__SPOTTED__ ") && !line.contains('━') {
                        push_line(&mut last_lines, line.clone());
                    }
                    let _ = window.emit(
                        "sidecar://line",
                        SidecarLine { kind: "stdout", line },
                    );
                }
            }
            CommandEvent::Stderr(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if !line.is_empty() {
                    push_line(&mut last_lines, line.clone());
                    let _ = window.emit(
                        "sidecar://line",
                        SidecarLine { kind: "stderr", line },
                    );
                }
            }
            CommandEvent::Terminated(payload) => {
                code = payload.code.unwrap_or(-1);
                break;
            }
            _ => {}
        }
    }

    if code == 0 {
        Ok(code)
    } else {
        let tail = last_lines.join("\n");
        if tail.is_empty() {
            Err(format!("sidecar exited with code {code}"))
        } else {
            Err(tail)
        }
    }
}

#[tauri::command]
async fn scan_folder(
    app: AppHandle,
    window: Window,
    paths: Vec<String>,
    tags: Vec<String>,
    energy: Option<bool>,
) -> Result<i32, String> {
    if paths.is_empty() {
        return Err("Nothing to scan.".into());
    }
    // Dragging a Finder multi-selection onto Spotted delivers every selected
    // file. Taking only the first is how a 170-clip drop scanned one clip.
    let mut args = vec!["scan".to_string()];
    let had_tags = !tags.is_empty();
    if had_tags {
        args.push("--tags".to_string());
        args.push(tags.join(","));
    }
    // Default on, matching the sidecar. Only the opt-out is ever sent, so an
    // older frontend that omits the field keeps the previous behaviour.
    if energy == Some(false) {
        args.push("--no-energy".to_string());
    }
    // Options first, then "--", so a clip whose name starts with a dash is
    // never read as a flag.
    args.push("--".to_string());
    args.extend(paths.iter().cloned());
    let result = run_sidecar(app, window, args).await;
    let mut payload = HashMap::new();
    payload.insert("had_batch_tags".into(), had_tags.to_string());
    payload.insert("paths".into(), paths.len().to_string());
    payload.insert(
        "status".into(),
        if result.is_ok() { "ok" } else { "error" }.into(),
    );
    telemetry::track("scan-complete", payload);
    result
}

#[tauri::command]
async fn cluster_faces(app: AppHandle, window: Window) -> Result<i32, String> {
    run_sidecar(app, window, vec!["cluster".into()]).await
}

#[tauri::command]
async fn tag_videos(
    app: AppHandle,
    window: Window,
    exclude_tags: Option<Vec<String>>,
    overwrite: Option<bool>,
    scope: Option<String>,
    all_clips: Option<bool>,
    energy: Option<bool>,
    exclude_energy: Option<Vec<String>>,
) -> Result<i32, String> {
    let mut args = vec!["tag-write".to_string()];
    // The bucket lives in the index, so skipping the scoring pass is not
    // enough to keep it off a clip scored on an earlier drop.
    if energy == Some(false) {
        args.push("--no-energy-keywords".to_string());
    }
    // Buckets the user unchecked on the review screen.
    if let Some(buckets) = exclude_energy.filter(|b| !b.is_empty()) {
        args.push("--exclude-energy".to_string());
        args.push(buckets.join(","));
    }
    // Re-tag Library is the one place library-wide is intended.
    if all_clips.unwrap_or(false) {
        args.push("--all".to_string());
    }
    // Confine the write to the folder this batch was about. Without it every
    // run rewrites every clip ever indexed.
    if let Some(path) = scope.filter(|p| !p.is_empty()) {
        args.push("--scope".to_string());
        args.push(path);
    }
    let excluded = exclude_tags.unwrap_or_default();
    if !excluded.is_empty() {
        args.push("--exclude-tags".to_string());
        args.push(excluded.join(","));
    }
    // Overwrite replaces each clip's whole keyword set with Spotted's current
    // one (vs the default merge, which only swaps Spotted's own prior tags).
    // Lets a user re-run a clip and get a clean slate instead of accumulation.
    let overwrite = overwrite.unwrap_or(false);
    if overwrite {
        args.push("--overwrite".to_string());
    }
    let result = run_sidecar(app, window, args).await;
    let mut payload = HashMap::new();
    payload.insert(
        "status".into(),
        if result.is_ok() { "ok" } else { "error" }.into(),
    );
    payload.insert("excluded".into(), excluded.len().to_string());
    payload.insert("overwrite".into(), overwrite.to_string());
    telemetry::track("tag-write-complete", payload);
    result
}

#[tauri::command]
async fn suggest_activities(
    app: AppHandle,
    window: Window,
    scope: Option<String>,
    all_clips: Option<bool>,
) -> Result<String, String> {
    // Capture the sidecar's stdout and RETURN it so the frontend reads the
    // activity-complete payload from the invoke result, not from a racy
    // side-channel event. Still emits sidecar://line for live progress, and
    // still registers the PID so a long match can be cancelled.
    let sidecar = sidecar_command(&app)?;
    let (mut rx, child) = sidecar
        .args({
            let mut a = vec!["activity-suggest".to_string()];
            if all_clips.unwrap_or(false) {
                a.push("--all".to_string());
            }
            if let Some(path) = scope.filter(|p| !p.is_empty()) {
                a.push("--scope".to_string());
                a.push(path);
            }
            a
        })
        .spawn()
        .map_err(|e| format!("sidecar spawn: {e}"))?;

    let pid = child.pid();
    if let Some(state) = app.try_state::<ActiveChildren>() {
        if let Ok(mut v) = state.0.lock() {
            v.push(pid);
        }
    }
    struct PidGuard {
        pid: u32,
        bucket: Arc<Mutex<Vec<u32>>>,
    }
    impl Drop for PidGuard {
        fn drop(&mut self) {
            if let Ok(mut v) = self.bucket.lock() {
                v.retain(|p| *p != self.pid);
            }
        }
    }
    let _guard = app
        .try_state::<ActiveChildren>()
        .map(|s| PidGuard { pid, bucket: s.0.clone() });

    let mut collected: Vec<String> = Vec::new();
    let mut err_tail: Vec<String> = Vec::new();
    let mut code = -1i32;
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if !line.is_empty() {
                    collected.push(line.clone());
                    let _ = window.emit("sidecar://line", SidecarLine { kind: "stdout", line });
                }
            }
            CommandEvent::Stderr(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if !line.is_empty() {
                    err_tail.push(line.clone());
                    if err_tail.len() > 8 {
                        err_tail.remove(0);
                    }
                    let _ = window.emit("sidecar://line", SidecarLine { kind: "stderr", line });
                }
            }
            CommandEvent::Terminated(payload) => {
                code = payload.code.unwrap_or(-1);
                break;
            }
            _ => {}
        }
    }

    let mut payload = HashMap::new();
    payload.insert("status".into(), if code == 0 { "ok" } else { "error" }.into());
    telemetry::track("activity-suggest-complete", payload);

    if code == 0 {
        Ok(collected.join("\n"))
    } else {
        Err(err_tail.join("\n"))
    }
}

/// Wipe the per-user library, but keep an undo path. Renames ~/.facetag/
/// to ~/.facetag.backup-<UTC-timestamp>/ instead of deleting outright, then
/// trims older backups so the user has the last 3 to roll back to. One bad
/// click used to mean lost work; now it's two clicks to recover.
#[tauri::command]
fn reset_library() -> Result<ResetResult, String> {
    use std::fs;
    let home = dirs_home_dir().ok_or_else(|| "couldn't resolve $HOME".to_string())?;
    let root = home.join(".facetag");
    if !root.exists() {
        return Ok(ResetResult { backup_dir: None });
    }
    let stamp = chrono_timestamp();
    let backup = home.join(format!(".facetag.backup-{stamp}"));
    fs::rename(&root, &backup)
        .map_err(|e| format!("mv {} → {}: {e}", root.display(), backup.display()))?;
    let _ = trim_old_backups(&home, 3); // best-effort; not fatal
    Ok(ResetResult { backup_dir: Some(backup.display().to_string()) })
}

#[derive(Serialize, Clone)]
struct ResetResult {
    backup_dir: Option<String>,
}

/// Roll the most recent backup back over the current ~/.facetag/ (which is
/// assumed to be absent or stale after a Reset). If the current dir exists,
/// rename it aside as a safety swap first so this op is itself undoable.
#[tauri::command]
fn restore_last_backup() -> Result<RestoreResult, String> {
    use std::fs;
    let home = dirs_home_dir().ok_or_else(|| "couldn't resolve $HOME".to_string())?;
    let backups = list_backups(&home);
    let latest = backups.first().ok_or_else(|| "no backups found".to_string())?;
    let root = home.join(".facetag");
    if root.exists() {
        let stamp = chrono_timestamp();
        let pre = home.join(format!(".facetag.pre-restore-{stamp}"));
        fs::rename(&root, &pre)
            .map_err(|e| format!("safety-rename of current ~/.facetag: {e}"))?;
    }
    fs::rename(latest, &root)
        .map_err(|e| format!("restore mv {} → {}: {e}", latest.display(), root.display()))?;
    Ok(RestoreResult { restored_from: latest.display().to_string() })
}

#[derive(Serialize, Clone)]
struct RestoreResult {
    restored_from: String,
}

#[tauri::command]
fn list_library_backups() -> Vec<String> {
    let home = match dirs_home_dir() {
        Some(h) => h,
        None => return Vec::new(),
    };
    list_backups(&home)
        .into_iter()
        .map(|p| p.display().to_string())
        .collect()
}

fn list_backups(home: &std::path::Path) -> Vec<std::path::PathBuf> {
    let mut out: Vec<std::path::PathBuf> = match std::fs::read_dir(home) {
        Ok(rd) => rd
            .filter_map(|e| e.ok().map(|d| d.path()))
            .filter(|p| {
                p.file_name()
                    .and_then(|s| s.to_str())
                    .map(|s| s.starts_with(".facetag.backup-"))
                    .unwrap_or(false)
            })
            .collect(),
        Err(_) => Vec::new(),
    };
    // Newest first (timestamp suffix sorts lexicographically because
    // chrono_timestamp emits zero-padded YYYYMMDDHHMMSS).
    out.sort();
    out.reverse();
    out
}

fn trim_old_backups(home: &std::path::Path, keep: usize) -> std::io::Result<()> {
    let backups = list_backups(home);
    for old in backups.into_iter().skip(keep) {
        std::fs::remove_dir_all(&old)?;
    }
    Ok(())
}

fn chrono_timestamp() -> String {
    // Unix seconds. Lexicographically sortable, opaque to the user but
    // they never need to decode it manually because Restore Last Backup
    // finds the newest one automatically. Saves us from pulling chrono.
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("{secs}")
}

fn dirs_home_dir() -> Option<std::path::PathBuf> {
    std::env::var_os("HOME").map(std::path::PathBuf::from)
}

#[tauri::command]
async fn write_markers(
    app: AppHandle,
    window: Window,
    scope: Option<String>,
    all_clips: Option<bool>,
    energy: Option<bool>,
    exclude_energy: Option<Vec<String>>,
) -> Result<i32, String> {
    let mut args = vec!["markers-write".to_string()];
    // Re-tag Library is the one place library-wide is intended.
    if all_clips.unwrap_or(false) {
        args.push("--all".to_string());
    }
    // Peaks persist in the index, so scan --no-energy does not stop a clip
    // scored on an earlier drop from carrying its cues into this timeline.
    if energy == Some(false) {
        args.push("--no-energy-markers".to_string());
    }
    if let Some(buckets) = exclude_energy.filter(|b| !b.is_empty()) {
        args.push("--exclude-energy".to_string());
        args.push(buckets.join(","));
    }
    // Same reason as tag_videos: without a scope the DaVinci timeline is built
    // from every clip ever indexed, including ones long since moved or deleted,
    // which Resolve then reports as missing media.
    if let Some(path) = scope.filter(|p| !p.is_empty()) {
        args.push("--scope".to_string());
        args.push(path);
    }
    run_sidecar(app, window, args).await
}

/// Spawn the Flask label-web in a long-lived background task. Polls the
/// localhost URL until Flask actually responds (or until timeout). The
/// child is kept alive by moving it into the background task; it gets
/// killed when the Tauri process exits.
#[tauri::command]
async fn start_label_server(
    app: AppHandle,
    window: Window,
    port: u16,
    scope_paths: Option<Vec<String>>,
) -> Result<String, String> {
    use std::sync::{Arc, Mutex};

    let sidecar = sidecar_command(&app)?;

    // Tear down a label server left running from a previous batch so it frees
    // the port. Otherwise the new server can't bind, and the readiness poll
    // below would get a 200 from the STALE server — showing the user the prior
    // batch's clusters instead of the folder they just dropped.
    let old_pid = app
        .try_state::<LabelServer>()
        .and_then(|s| s.0.lock().ok().and_then(|g| *g));
    if let Some(pid) = old_pid {
        let _ = std::process::Command::new("kill")
            .arg("-TERM")
            .arg(pid.to_string())
            .status();
        // Wait for the port to actually free (connection refused) before we
        // spawn the replacement, so we never poll the dying server.
        let probe = reqwest::Client::builder()
            .timeout(Duration::from_millis(300))
            .build()
            .map_err(|e| format!("http client: {e}"))?;
        let probe_url = format!("http://127.0.0.1:{port}/");
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while std::time::Instant::now() < deadline {
            if probe.get(&probe_url).send().await.is_err() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(150)).await;
        }
        if let Some(s) = app.try_state::<LabelServer>() {
            if let Ok(mut g) = s.0.lock() {
                *g = None;
            }
        }
    }

    // Build sidecar args. Append --scope-path for each batch path the
    // frontend hands us so the labeler shows only clusters touching the
    // freshly-dropped clip(s). The labeler still exposes a "show all"
    // toggle in its header chip for cross-batch labeling.
    // Per-session token. The labeler serves cropped faces, names and file
    // paths with no login, so without this any local process (and any web page
    // via DNS rebinding) can read and modify all of it.
    let token = uuid::Uuid::new_v4().simple().to_string();
    if let Some(s) = app.try_state::<LabelToken>() {
        if let Ok(mut g) = s.0.lock() {
            *g = token.clone();
        }
    }

    let mut args: Vec<String> = vec![
        "label-web".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--no-browser".to_string(),
        "--token".to_string(),
        token.clone(),
    ];
    if let Some(paths) = scope_paths {
        for p in paths {
            args.push("--scope-path".to_string());
            args.push(p);
        }
    }

    let (mut rx, child) = sidecar
        .args(args)
        .spawn()
        .map_err(|e| format!("sidecar spawn: {e}"))?;

    // Record this server's PID so the next batch can tear it down.
    if let Some(s) = app.try_state::<LabelServer>() {
        if let Ok(mut g) = s.0.lock() {
            *g = Some(child.pid());
        }
    }

    // Capture stderr in a shared buffer so we can surface it if Flask
    // never comes up.
    let last_err: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let last_err_clone = last_err.clone();
    let window_for_task = window.clone();
    tauri::async_runtime::spawn(async move {
        let _keepalive = child;
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) | CommandEvent::Stderr(bytes) => {
                    let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                    if !line.is_empty() {
                        if let Ok(mut buf) = last_err_clone.lock() {
                            buf.push(line.clone());
                            if buf.len() > 12 {
                                buf.remove(0);
                            }
                        }
                        let _ = window_for_task.emit(
                            "sidecar://line",
                            SidecarLine { kind: "stdout", line },
                        );
                    }
                }
                _ => {}
            }
        }
    });

    // Poll until Flask actually responds on the port. Flask's cold start
    // inside the PyInstaller-extracted environment can take 2-5 seconds
    // on a slower Mac, so the old 900ms fixed sleep wasn't enough.
    //
    // Ask /health, not /. The index route summarises every cluster in the
    // library and renders a card per person; past ~200 clips that took longer
    // than the per-request timeout, every probe failed, and a user whose
    // labeler was running fine was told it had failed to start. /health does
    // no database work, so this measures the server, not the library.
    let url = format!("http://127.0.0.1:{port}/?k={token}");
    let health_url = format!("http://127.0.0.1:{port}/health?k={token}");
    let timeout = Duration::from_secs(60);
    let start = std::time::Instant::now();
    let client = reqwest::Client::builder()
        // Generous per request: this bounds one probe, and a probe that is
        // merely slow must not be read as a server that is down.
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|e| format!("http client: {e}"))?;

    while start.elapsed() < timeout {
        match client.get(&health_url).send().await {
            Ok(resp) if resp.status().is_success() => {
                return Ok(url);
            }
            _ => {
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }
    }

    let tail = last_err
        .lock()
        .map(|b| b.join("\n"))
        .unwrap_or_default();
    if tail.is_empty() {
        Err(format!(
            "Labeling server didn't respond on port {port} after {}s. \
             Try quitting and reopening Spotted.",
            timeout.as_secs()
        ))
    } else {
        Err(format!("Labeling server failed to start:\n{tail}"))
    }
}

#[tauri::command]
async fn generate_person_thumbs(app: AppHandle, window: Window) -> Result<i32, String> {
    run_sidecar(app, window, vec!["person-thumbs".into()]).await
}

#[tauri::command]
async fn fetch_library_detail(app: AppHandle, window: Window) -> Result<String, String> {
    let sidecar = sidecar_command(&app)?;
    let (mut rx, _child) = sidecar
        .args(["status".to_string(), "--detail".to_string()])
        .spawn()
        .map_err(|e| format!("sidecar spawn: {e}"))?;

    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if !line.is_empty() {
                    let _ = window.emit(
                        "sidecar://line",
                        SidecarLine { kind: "stdout", line },
                    );
                }
            }
            CommandEvent::Terminated(_) => break,
            _ => {}
        }
    }
    Ok("done".into())
}

#[tauri::command]
async fn rename_person(
    app: AppHandle,
    window: Window,
    old: String,
    new: String,
) -> Result<i32, String> {
    run_sidecar(app, window, vec!["rename-person".into(), old, new]).await
}

#[tauri::command]
async fn delete_person(app: AppHandle, window: Window, name: String) -> Result<i32, String> {
    run_sidecar(app, window, vec!["delete-person".into(), name]).await
}

#[tauri::command]
async fn fetch_status(
    app: AppHandle,
    window: Window,
    batch: Option<bool>,
) -> Result<String, String> {
    // Capture the structured event from `facetag status`. We collect all
    // stdout, then return the raw lines for the frontend to parse.
    let sidecar = sidecar_command(&app)?;

    let mut args = vec!["status".to_string()];
    // --batch reports only the clips the last scan was handed, and comes back
    // as batch-stats. Without it this reports the whole index, which is what
    // the Library view asks for. The two must not be confused: the finish line
    // once told a user her single-clip drop had tagged 169 clips. The batch
    // comes from the database, not from a path the UI is holding, because the
    // updater restarts the app mid-run and that wipes anything held in memory.
    if batch.unwrap_or(false) {
        args.push("--batch".to_string());
    }

    let (mut rx, _child) = sidecar
        .args(args)
        .spawn()
        .map_err(|e| format!("sidecar spawn: {e}"))?;

    let mut collected: Vec<String> = Vec::new();
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if !line.is_empty() {
                    collected.push(line.clone());
                    let _ = window.emit(
                        "sidecar://line",
                        SidecarLine { kind: "stdout", line },
                    );
                }
            }
            CommandEvent::Terminated(_) => break,
            _ => {}
        }
    }
    Ok(collected.join("\n"))
}

#[tauri::command]
async fn reveal_in_finder(path: String) -> Result<(), String> {
    // Use macOS's `open` to reveal the folder (or the parent if file).
    std::process::Command::new("open")
        .arg(&path)
        .spawn()
        .map_err(|e| format!("open failed: {e}"))?;
    Ok(())
}

#[tauri::command]
async fn set_window_title(window: Window, title: String) -> Result<(), String> {
    window.set_title(&title).map_err(|e| e.to_string())
}

fn open_url(url: &str) -> std::io::Result<()> {
    std::process::Command::new("open").arg(url).spawn().map(|_| ())
}

#[tauri::command]
async fn cancel_work(app: AppHandle) -> Result<u32, String> {
    let pids: Vec<u32> = app
        .try_state::<ActiveChildren>()
        .and_then(|s| s.0.lock().ok().map(|v| v.clone()))
        .unwrap_or_default();
    let count = pids.len() as u32;
    for pid in pids {
        // SIGTERM first; sidecar's Python will exit cleanly.
        let _ = std::process::Command::new("kill")
            .arg("-TERM")
            .arg(pid.to_string())
            .status();
    }
    Ok(count)
}

#[tauri::command]
async fn check_for_updates(app: AppHandle) -> Result<Option<String>, String> {
    match app.updater() {
        Ok(updater) => match updater.check().await {
            Ok(Some(update)) => Ok(Some(update.version.clone())),
            Ok(None) => Ok(None),
            Err(e) => Err(format!("update check failed: {e}")),
        },
        Err(e) => Err(format!("updater unavailable: {e}")),
    }
}

/// Download + install the pending update, emitting progress events to the
/// frontend so the user sees a visible toast instead of a silent black box.
/// The frontend calls this after the user accepts the update dialog;
/// `restart_app` is invoked separately to actually swap in the new bundle.
#[tauri::command]
async fn install_update(app: AppHandle) -> Result<(), String> {
    let updater = app.updater().map_err(|e| format!("updater unavailable: {e}"))?;
    let update = updater
        .check()
        .await
        .map_err(|e| format!("update check failed: {e}"))?
        .ok_or_else(|| "no update available".to_string())?;

    let app_clone = app.clone();
    let mut downloaded: u64 = 0;
    update
        .download_and_install(
            move |chunk_len, content_length| {
                downloaded += chunk_len as u64;
                let _ = app_clone.emit(
                    "updater://progress",
                    UpdaterProgress {
                        downloaded,
                        total: content_length,
                    },
                );
            },
            || {},
        )
        .await
        .map_err(|e| format!("install failed: {e}"))?;

    Ok(())
}

/// Triggered by the frontend after a successful install. AppHandle::restart
/// diverges (returns `!`), so the function never returns to the caller.
#[tauri::command]
fn restart_app(app: AppHandle) {
    app.restart();
}

#[tauri::command]
fn app_version() -> String {
    env!("CARGO_PKG_VERSION").into()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Sentry crash reporting — gated by both the compile-time DSN AND
    // the user's telemetry opt-in. The guard MUST live for the duration
    // of the app or the panic hook gets dropped and we miss crashes;
    // we forget it so it stays alive until process exit.
    if let Some(guard) = telemetry::init_sentry() {
        std::mem::forget(guard);
    }

    tauri::Builder::default()
        .manage(ActiveChildren::default())
        .manage(LabelServer::default())
        .manage(LabelToken::default())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            // Boot telemetry early so subsequent track() calls see the
            // install ID. No events fire until the user opts in.
            let _ = telemetry::boot();
            telemetry::track("app-launch", HashMap::new());

            // Re-register the bundle with Spotlight. The updater replaces
            // Spotted.app wholesale, and an ad-hoc signed bundle swapped out
            // from under LaunchServices can drop out of the Spotlight index —
            // the app stops appearing in Cmd+Space and has to be found by
            // hand in Applications. Cheap, backgrounded, and harmless if the
            // index is already fine.
            if let Ok(exe) = std::env::current_exe() {
                // .../Spotted.app/Contents/MacOS/Spotted -> .../Spotted.app
                if let Some(bundle) = exe.ancestors().nth(3) {
                    if bundle.extension().and_then(|e| e.to_str()) == Some("app") {
                        let b = bundle.to_path_buf();
                        std::thread::spawn(move || {
                            let _ = std::process::Command::new("mdimport").arg(&b).status();
                        });
                    }
                }
            }

            // Auto-check for updates on launch. Tauri v2 (unlike v1) ignores
            // the `dialog: true` updater config and does not show any UI on
            // its own — the previous version of this code called
            // `download_and_install` directly with no-op callbacks, which
            // meant updates either silently swapped without the user noticing
            // (and got blamed for "never working") or silently failed with
            // errors only visible in stderr. Now we emit an event that the
            // frontend turns into a native confirm dialog + progress toast.
            let handle_for_update = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                // Give the window a moment to mount before any native
                // dialog could appear over the welcome overlay.
                tokio::time::sleep(Duration::from_secs(3)).await;
                if let Ok(updater) = handle_for_update.updater() {
                    match updater.check().await {
                        Ok(Some(update)) => {
                            let _ = handle_for_update.emit(
                                "updater://available",
                                UpdaterAvailable {
                                    version: update.version.clone(),
                                    current_version: env!("CARGO_PKG_VERSION").into(),
                                },
                            );
                        }
                        Ok(None) => { /* up to date — stay quiet */ }
                        Err(e) => {
                            // Background check failures are usually transient
                            // network issues; surface them but don't toast —
                            // the frontend listener decides whether to show.
                            let _ = handle_for_update.emit(
                                "updater://error",
                                UpdaterMessage {
                                    message: format!("update check failed: {e}"),
                                },
                            );
                        }
                    }
                }
            });

            // Wire macOS menu bar with native shortcuts.
            #[cfg(target_os = "macos")]
            {
                use tauri::menu::{AboutMetadataBuilder, MenuBuilder, MenuItemBuilder, SubmenuBuilder};

                let about_meta = AboutMetadataBuilder::new()
                    .name(Some("Spotted"))
                    .version(Some(env!("CARGO_PKG_VERSION")))
                    .copyright(Some("© 2026 Landon Colvig"))
                    .build();

                let app_submenu = SubmenuBuilder::new(app, "Spotted")
                    .about(Some(about_meta))
                    .separator()
                    .item(
                        &MenuItemBuilder::with_id("open_library", "Library")
                            .accelerator("CmdOrCtrl+L")
                            .build(app)?,
                    )
                    .separator()
                    .item(
                        &MenuItemBuilder::with_id("check_updates", "Check for Updates…")
                            .build(app)?,
                    )
                    .item(
                        &MenuItemBuilder::with_id("telemetry_settings", "Telemetry…")
                            .build(app)?,
                    )
                    .item(
                        &MenuItemBuilder::with_id("retag_library", "Re-tag Library")
                            .build(app)?,
                    )
                    .item(
                        &MenuItemBuilder::with_id("show_welcome", "Show Welcome…")
                            .build(app)?,
                    )
                    .separator()
                    .services()
                    .separator()
                    .hide()
                    .hide_others()
                    .show_all()
                    .separator()
                    .quit()
                    .build()?;

                let file_submenu = SubmenuBuilder::new(app, "File")
                    .item(
                        &MenuItemBuilder::with_id("open_folder", "Open Folder…")
                            .accelerator("CmdOrCtrl+O")
                            .build(app)?,
                    )
                    .separator()
                    .item(
                        &MenuItemBuilder::with_id("reset_library", "Reset Library…")
                            .build(app)?,
                    )
                    .item(
                        &MenuItemBuilder::with_id("restore_backup", "Restore Last Backup…")
                            .build(app)?,
                    )
                    .build()?;

                let edit_submenu = SubmenuBuilder::new(app, "Edit")
                    .undo()
                    .redo()
                    .separator()
                    .cut()
                    .copy()
                    .paste()
                    .select_all()
                    .build()?;

                let window_submenu = SubmenuBuilder::new(app, "Window")
                    .minimize()
                    .maximize()
                    .separator()
                    .close_window()
                    .build()?;

                let help_submenu = SubmenuBuilder::new(app, "Help")
                    .item(
                        &MenuItemBuilder::with_id("show_welcome", "Show Welcome…")
                            .build(app)?,
                    )
                    .separator()
                    .item(
                        &MenuItemBuilder::with_id("open_github", "View on GitHub")
                            .build(app)?,
                    )
                    .item(
                        &MenuItemBuilder::with_id("report_issue", "Report an Issue…")
                            .build(app)?,
                    )
                    .build()?;

                let menu = MenuBuilder::new(app)
                    .items(&[&app_submenu, &file_submenu, &edit_submenu, &window_submenu, &help_submenu])
                    .build()?;

                app.set_menu(menu)?;

                app.on_menu_event(move |handle, event| match event.id().as_ref() {
                    "open_folder" => {
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.emit("menu://open-folder", ());
                        }
                    }
                    "check_updates" => {
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.emit("menu://check-updates", ());
                        }
                    }
                    "retag_library" => {
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.emit("menu://retag-library", ());
                        }
                    }
                    "open_library" => {
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.emit("menu://open-library", ());
                        }
                    }
                    "show_welcome" => {
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.emit("menu://show-welcome", ());
                        }
                    }
                    "open_github" => {
                        let _ = open_url("https://github.com/landoncolvig/spotted");
                    }
                    "report_issue" => {
                        let _ = open_url("https://github.com/landoncolvig/spotted/issues/new");
                    }
                    "reset_library" => {
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.emit("menu://reset-library", ());
                        }
                    }
                    "restore_backup" => {
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.emit("menu://restore-backup", ());
                        }
                    }
                    "telemetry_settings" => {
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.emit("menu://telemetry-settings", ());
                        }
                    }
                    _ => {}
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            scan_folder,
            cluster_faces,
            tag_videos,
            write_markers,
            start_label_server,
            fetch_status,
            fetch_library_detail,
            generate_person_thumbs,
            rename_person,
            delete_person,
            reveal_in_finder,
            set_window_title,
            cancel_work,
            check_for_updates,
            install_update,
            restart_app,
            suggest_activities,
            reset_library,
            restore_last_backup,
            list_library_backups,
            telemetry::cmds::telemetry_state,
            telemetry::cmds::set_telemetry_enabled,
            telemetry::cmds::track_event,
            telemetry::cmds::telemetry_active,
            app_version
        ])
        .build(tauri::generate_context!())
        .expect("error while running Spotted")
        .run(|app, event| {
            // Tauri only auto-kills children spawned from JS; these are spawned
            // from Rust, so without this the label server (and any running
            // scan) is reparented to launchd and keeps going after quit.
            if let tauri::RunEvent::Exit = event {
                if let Some(s) = app.try_state::<LabelServer>() {
                    if let Ok(mut g) = s.0.lock() {
                        if let Some(pid) = g.take() {
                            let _ = std::process::Command::new("kill")
                                .arg("-TERM")
                                .arg(pid.to_string())
                                .status();
                        }
                    }
                }
                if let Some(s) = app.try_state::<ActiveChildren>() {
                    if let Ok(mut g) = s.0.lock() {
                        for pid in g.drain(..) {
                            let _ = std::process::Command::new("kill")
                                .arg("-TERM")
                                .arg(pid.to_string())
                                .status();
                        }
                    }
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::prepare_sidecar_temp_dir;
    use std::fs;

    fn scratch_root() -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "spotted-sidecar-temp-test-{}",
            uuid::Uuid::new_v4().simple()
        ))
    }

    #[test]
    fn prepares_a_writable_app_owned_sidecar_temp_directory() {
        let root = scratch_root();
        let prepared = prepare_sidecar_temp_dir(&root).expect("temp directory should be usable");

        assert_eq!(prepared, root.join("sidecar-tmp"));
        assert!(prepared.is_dir());
        assert!(
            fs::read_dir(&prepared)
                .expect("prepared directory should be readable")
                .next()
                .is_none(),
            "write probe must be removed before the sidecar starts"
        );

        fs::remove_dir_all(&root).expect("scratch directory should be removable");
    }

    #[cfg(unix)]
    #[test]
    fn rejects_an_unwritable_app_cache_before_starting_the_sidecar() {
        use std::os::unix::fs::PermissionsExt;

        let root = scratch_root();
        fs::create_dir_all(&root).expect("scratch root should be created");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o500))
            .expect("scratch root should become read-only");

        let result = prepare_sidecar_temp_dir(&root);

        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
            .expect("scratch root permissions should be restored");
        fs::remove_dir_all(&root).expect("scratch directory should be removable");

        assert!(result.is_err(), "unwritable cache must fail before spawn");
        assert!(
            result
                .expect_err("unwritable cache should return an explanation")
                .contains("local working folder"),
            "the error should identify Spotted's working folder"
        );
    }
}
