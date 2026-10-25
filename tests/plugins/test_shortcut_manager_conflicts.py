# -*- coding: utf-8 -*-
"""快捷键冲突汇总 / 作用域判定（shortcut-manager 插件纯函数）回归测试

Qt 的 QShortcutMap 对同窗口同键序列判 ambiguous，只发 activatedAmbiguously，
命令一律不触发。列表必须让用户看见冲突，否则现象只有「按了没反应」。
"""

import importlib.util
from pathlib import Path


def _load_cards():
    """按文件路径加载插件模块（插件目录不在包搜索路径内）"""
    path = Path(__file__).resolve().parents[2] / "plugins" / "shortcut-manager" / "ui" / "cards.py"
    spec = importlib.util.spec_from_file_location("sm_cards_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_collect_conflict_names_marks_all_owners():
    cards = _load_cards()
    cmds = [
        {"name": "a", "shortcut": "Ctrl+K"},
        {"name": "b", "shortcut": "ctrl+k"},
        {"name": "c", "shortcut": "Ctrl+L"},
        {"name": "d", "shortcut": ""},
    ]
    assert cards._collect_conflict_names(cmds) == {"a", "b"}


def test_collect_conflict_names_empty_when_unique():
    cards = _load_cards()
    cmds = [{"name": "a", "shortcut": "Ctrl+K"}, {"name": "b", "shortcut": "Ctrl+L"}]
    assert cards._collect_conflict_names(cmds) == set()


def test_collect_conflict_names_ignores_empty_shortcuts():
    cards = _load_cards()
    cmds = [{"name": "a", "shortcut": ""}, {"name": "b", "shortcut": ""}]
    assert cards._collect_conflict_names(cmds) == set()


def test_find_conflicts_is_case_insensitive():
    cards = _load_cards()
    cmds = [
        {"name": "a", "shortcut": "Alt+Q"},
        {"name": "b", "shortcut": "alt+q"},
    ]
    found = cards._find_conflicts("ALT+Q", cmds, exclude_cmd="a")
    assert [c["name"] for c in found] == ["b"]


def test_find_conflicts_excludes_self():
    cards = _load_cards()
    cmds = [{"name": "a", "shortcut": "Ctrl+K"}]
    assert cards._find_conflicts("Ctrl+K", cmds, exclude_cmd="a") == []


def test_is_single_key():
    cards = _load_cards()
    assert cards._is_single_key("Q") is True
    assert cards._is_single_key("Ctrl+Q") is False
    assert cards._is_single_key("F5") is True
    assert cards._is_single_key("") is False


def test_global_scope_commands_matches_tray_hotkey():
    """全局热键命令表必须与 tray_manager 实际读取的命令名一致"""
    cards = _load_cards()
    tray_src = (Path(__file__).resolve().parents[2] / "app" / "tray_manager.py").read_text(encoding="utf-8")
    for name in cards._GLOBAL_SCOPE_COMMANDS:
        assert f'get_command("{name}")' in tray_src, f"tray_manager 未读取 {name} 的快捷键"
