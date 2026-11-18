// 防止 Windows release 构建弹出额外控制台窗口
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    drifox_desktop_lib::run()
}
