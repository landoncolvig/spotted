use serde::Serialize;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, Window};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

#[derive(Serialize, Clone)]
struct SidecarLine {
    kind: &'static str,
    line: String,
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
) -> Result<u16, String> {
    use std::sync::{Arc, Mutex};

    let sidecar = app
        .shell()
        .sidecar("spotted-sidecar")
        .map_err(|e| format!("sidecar lookup: {e}"))?;

    let (mut rx, child) = sidecar
        .args([
            "label-web".to_string(),
            "--port".to_string(),
            port.to_string(),
            "--no-browser".to_string(),
        ])
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

#[tauri::command]
fn app_version() -> String {
    env!("CARGO_PKG_VERSION").into()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
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
                        &MenuItemBuilder::with_id("check_updates", "Check for Updates…")
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

                let menu = MenuBuilder::new(app)
                    .items(&[&app_submenu, &file_submenu, &edit_submenu, &window_submenu])
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
            reveal_in_finder,
            check_for_updates,
            app_version
        ])
        .run(tauri::generate_context!())
        .expect("error while running Spotted");
}
