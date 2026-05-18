use serde::Serialize;

#[derive(Serialize)]
struct ScanResult {
    path: String,
    status: String,
}

#[tauri::command]
fn start_scan(path: String) -> ScanResult {
    ScanResult {
        path,
        status: "queued".into(),
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
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![start_scan, app_version])
        .run(tauri::generate_context!())
        .expect("error while running Spotted");
}
