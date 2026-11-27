"""Windows 真窗多会话采样；独立运行，不经 main.py 的全局单实例锁。

.venv/Scripts/python.exe -u tests/perf/probe_windows_sessions.py memory
每行 [probe] 后为 JSON。只枚举当前解释器后代，private 为字节，不是 RSS。
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def emit(**row):
    print('[probe] ' + json.dumps(row, ensure_ascii=True), flush=True)


def tree_sample():
    import psutil

    root = psutil.Process()
    rows = []
    for proc in [root, *root.children(recursive=True)]:
        try:
            rows.append(dict(pid=proc.pid, name=proc.name(), private=proc.memory_info().private))
        except psutil.NoSuchProcess:
            continue
    assert any('qtwebengine' in p['name'].lower() for p in rows)
    return rows


def memory(tm, first, drain):
    def sample(phase, cycle):
        drain(2000)
        for _ in range(3):
            emit(phase=phase, cycle=cycle, tabs=len(tm._windows), processes=tree_sample())
            drain(250)

    sample('base', 0)
    for cycle in range(1, 4):
        for i in range(3):
            window = tm.spawn_tab(first, new_session=True)
            assert window is not None
            drain(1200)
            assert window._current_session_id and window.isVisible()
            card = window._append_assistant_message()
            card.ensure_rendered()
            card.append_text(f'合成会话 {cycle}.{i}。' * 60)
            card.finish_streaming()
            drain(800)
            assert card.isVisible() and card.viewer._is_js_ready
            del card, window
        sample('open', cycle)
        for i in range(3):
            tm._on_tab_close_requested(len(tm._windows) - 1)
            drain(350)
        assert tm._windows == [first]
        sample('closed', cycle)


def run(mode, directory):
    os.chdir(directory)  # 同时隔离仍使用相对 .drifox 的开发态路径
    os.environ.update(DRIFOX_DATA_DIR=str(Path(directory) / '.drifox'), QT_OPENGL='angle', QT_QPA_PLATFORM='windows')
    os.environ.pop('DRIFOX_QT_RENDERER', None)
    from app.utils.render_env import apply_render_env, default_config_path

    apply_render_env(default_config_path())
    from test_new_tab_baseline import QApplication, FakePage, _drain
    from app.core.infra.webengine_profile import init_shared_web_profile

    app = QApplication([])
    app.setStyle('Fusion')
    app.setQuitOnLastWindowClosed(False)
    profile = init_shared_web_profile(parent=app)
    assert Path(profile.persistentStoragePath()).resolve().is_relative_to(Path(directory))
    from app.main_widget import OpenAIChatToolWindow
    from app.widgets.tab_manager_window import TabManagerWindow

    # 独立进程内关闭与 UI 无关的联网预热；不改主程序或用户配置。
    OpenAIChatToolWindow._start_models_dev_sync = lambda self: None
    OpenAIChatToolWindow._check_gitee_sync_reminder = lambda self: None
    tm = TabManagerWindow.create_instance()
    tm.show()
    tm.show_boot_placeholder()
    from app.core.services.plugin_host_service import PluginHostService
    from app.utils.preheat import preheat_process_level

    PluginHostService.get_instance().ensure_started()
    tm.complete_construction()
    preheat_process_level()
    page = FakePage()
    first = OpenAIChatToolWindow(page)
    tm.add_window(first)
    tm.remove_boot_placeholder()
    tm._mark_first_window_ready()
    _drain(3000)
    assert first._current_session_id and first.isVisible()
    emit(mode=mode, pid=os.getpid(), platform=app.platformName(), opengl=os.environ.get('QT_OPENGL'))
    if mode == 'memory':
        memory(tm, first, _drain)
    else:
        from probe_finish_layers import finish
        finish(tm, first, _drain, emit)
    emit(complete=mode)
    # 避开诊断进程解释器退出时 Qt profile/页面的不定析构顺序；临时目录由父进程回收。
    os._exit(0)


if __name__ == '__main__':
    import subprocess

    if sys.platform != 'win32' or sys.argv[1:2] not in (['memory'], ['finish']):
        raise SystemExit('用法：Windows 真桌面，probe_windows_sessions.py memory 或 finish')
    if len(sys.argv) == 2:
        with tempfile.TemporaryDirectory(prefix='drifox-perf-') as tmp:
            result = subprocess.run([sys.executable, '-u', __file__, sys.argv[1], tmp])
        raise SystemExit(result.returncode)
    run(sys.argv[1], sys.argv[2])
