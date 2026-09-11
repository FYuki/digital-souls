use std::fs;
use std::path::PathBuf;

#[tauri::command]
fn report_live2d_status(status: String, detail: String) -> Result<(), String> {
    let path = std::env::var("DIGITAL_SOULS_LIVE2D_STATUS_FILE")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir().join("digital-souls-live2d-status.json"));

    let payload = serde_json::json!({
        "status": status,
        "detail": detail,
    });

    fs::write(&path, serde_json::to_vec_pretty(&payload).map_err(|e| e.to_string())?)
        .map_err(|e| format!("{}: {}", path.display(), e))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![report_live2d_status])
        .run(tauri::generate_context!())
        .expect("Tauriアプリケーションの起動に失敗しました");
}
