use serde::Serialize;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, Window};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

/// Shared state: PIDs of currently-running sidecar processes. Cancel
/// command signals all of them so a single click stops whatever phase
/// the pipeline is in.
#[derive(Default)]
struct ActiveChildren(Arc<Mutex<Vec<u32>>>);

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

/// Spawn the sidecar, stream its output as `sidecar://line` events on the
/// window, await the child to exit, return its exit code.
async fn run_sidecar(
    app: AppHandle,
    window: Window,
    args: Vec<String>,
) -> Result<i32, String> {
    let sidecar = app
        .shell()
        .sidecar("spotted-sidecar")
        .map_err(|e| format!("sidecar lookup: {e}"))?;

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
    path: String,
    tags: Vec<String>,
) -> Result<i32, String> {
    let mut args = vec!["scan".to_string(), path];
    if !tags.is_empty() {
        args.push("--tags".to_string());
        args.push(tags.join(","));
    }
    run_sidecar(app, window, args).await
}

#[tauri::command]
async fn cluster_faces(app: AppHandle, window: Window) -> Result<i32, String> {
    run_sidecar(app, window, vec!["cluster".into()]).await
}

#[tauri::command]
async fn tag_videos(app: AppHandle, window: Window) -> Result<i32, String> {
    run_sidecar(app, window, vec!["tag-write".into()]).await
}

#[tauri::command]
async fn suggest_activities(app: AppHandle, window: Window) -> Result<i32, String> {
    run_sidecar(app, window, vec!["activity-suggest".into()]).await
}

/// Wipe the per-user library — face index, embeddings, generated thumbs,
/// person crops. Equivalent to deleting ~/.facetag/ and starting over. The
/// frontend confirms with a native dialog before invoking this; from
/// here on it's destructive and silent.
#[tauri::command]
fn reset_library() -> Result<(), String> {
    use std::fs;
    let home = dirs_home_dir().ok_or_else(|| "couldn't resolve $HOME".to_string())?;
    let root = home.join(".facetag");
    if root.exists() {
        fs::remove_dir_all(&root).map_err(|e| format!("rm -rf {}: {e}", root.display()))?;
    }
    Ok(())
}

fn dirs_home_dir() -> Option<std::path::PathBuf> {
    std::env::var_os("HOME").map(std::path::PathBuf::from)
}

#[tauri::command]
async fn write_markers(app: AppHandle, window: Window) -> Result<i32, String> {
    run_sidecar(app, window, vec!["markers-write".into()]).await
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
) -> Result<u16, String> {
    use std::sync::{Arc, Mutex};

    let sidecar = app
        .shell()
        .sidecar("spotted-sidecar")
        .map_err(|e| format!("sidecar lookup: {e}"))?;

    // Build sidecar args. Append --scope-path for each batch path the
    // frontend hands us so the labeler shows only clusters touching the
    // freshly-dropped clip(s). The labeler still exposes a "show all"
    // toggle in its header chip for cross-batch labeling.
    let mut args: Vec<String> = vec![
        "label-web".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--no-browser".to_string(),
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
    let url = format!("http://127.0.0.1:{port}/");
    let timeout = Duration::from_secs(20);
    let start = std::time::Instant::now();
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(500))
        .build()
        .map_err(|e| format!("http client: {e}"))?;

    while start.elapsed() < timeout {
        match client.get(&url).send().await {
            Ok(resp) if resp.status().is_success() => {
                return Ok(port);
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
    let sidecar = app
        .shell()
        .sidecar("spotted-sidecar")
        .map_err(|e| format!("sidecar lookup: {e}"))?;
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
async fn fetch_status(app: AppHandle, window: Window) -> Result<String, String> {
    // Capture the structured event from `facetag status`. We collect all
    // stdout, then return the raw lines for the frontend to parse.
    let sidecar = app
        .shell()
        .sidecar("spotted-sidecar")
        .map_err(|e| format!("sidecar lookup: {e}"))?;

    let (mut rx, _child) = sidecar
        .args(["status".to_string()])
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
    tauri::Builder::default()
        .manage(ActiveChildren::default())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
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
            app_version
        ])
        .run(tauri::generate_context!())
        .expect("error while running Spotted");
}
