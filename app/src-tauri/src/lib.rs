use serde::Serialize;
use tauri::{AppHandle, Emitter, Window};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

#[derive(Serialize, Clone)]
struct SidecarEvent {
    kind: &'static str,
    line: String,
}

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
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if !line.is_empty() {
                    let _ = window.emit(
                        "sidecar://line",
                        SidecarEvent { kind: "stdout", line },
                    );
                }
            }
            CommandEvent::Stderr(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if !line.is_empty() {
                    let _ = window.emit(
                        "sidecar://line",
                        SidecarEvent { kind: "stderr", line },
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
        Err(format!("sidecar exited with code {code}"))
    }
}

#[tauri::command]
async fn scan_folder(app: AppHandle, window: Window, path: String) -> Result<i32, String> {
    run_sidecar(app, window, vec!["scan".into(), path]).await
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
async fn start_label_server(
    app: AppHandle,
    window: Window,
    port: u16,
) -> Result<u16, String> {
    // Fires off the label-web Flask server in the background. The sidecar's
    // own stdout will tell us when it's listening; we just return the port
    // we passed in (Flask binds to it).
    let _ = run_sidecar(
        app,
        window,
        vec![
            "label-web".into(),
            "--port".into(),
            port.to_string(),
            "--no-browser".into(),
        ],
    );
    Ok(port)
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
        .invoke_handler(tauri::generate_handler![
            scan_folder,
            cluster_faces,
            tag_videos,
            start_label_server,
            app_version
        ])
        .run(tauri::generate_context!())
        .expect("error while running Spotted");
}
