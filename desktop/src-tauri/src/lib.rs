// Tauri 2 应用入口。UI 复刻阶段在此挂 command 与事件桥。
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
