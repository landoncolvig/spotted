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
    let mut last_stderr: Vec<String> = Vec::new();

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
            CommandEvent::Stderr(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if !line.is_empty() {
                    // Keep last few stderr lines for error reporting.
                    last_stderr.push(line.clone());
                    if last_stderr.len() > 6 {
                        last_stderr.remove(0);
                    }
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
        let tail = last_stderr.join("\n");
        if tail.is_empty() {
            Err(format!("sidecar exited with code {code}"))
        } else {
            Err(format!("sidecar exited with code {code}:\n{tail}"))
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

/// Spawn the Flask label-web in a long-lived background task. Returns the
/// port once Flask has had a moment to bind. The child is kept alive by
/// moving it into the background task; it gets killed when the Tauri
/// process exits.
#[tauri::command]
async fn start_label_server(
    app: AppHandle,
    window: Window,
    port: u16,
) -> Result<u16, String> {
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

    // Background task forwards output and keeps the child alive.
    let window_for_task = window.clone();
    tauri::async_runtime::spawn(async move {
        let _keepalive = child; // dropped at task end, killing the sidecar
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) | CommandEvent::Stderr(bytes) => {
                    let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                    if !line.is_empty() {
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

    // Give Flask a moment to bind before the webview iframes it.
    tokio::time::sleep(Duration::from_millis(900)).await;
    Ok(port)
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
