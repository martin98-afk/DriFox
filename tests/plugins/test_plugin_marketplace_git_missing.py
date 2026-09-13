# -*- coding: utf-8 -*-
"""plugin-marketplace git 缺失引导 回归测试

覆盖「git 未安装时报错自动引导安装」链路的核心判定：
- ``subprocess.Popen(["git", ...])`` 抛 ``FileNotFoundError`` → 应转抛
  ``GitNotFoundError``（精确识别环境缺 git，而非误报网络错误）
- ``_format_git_err`` 对该异常返回可读中文文案
- ``_download_and_move`` 全链路失败时 ``last_error`` 落该文案（UI 读取源）
- UI 层 ``_is_git_missing`` 对文案的判定
"""

import sys
from pathlib import Path

import pytest

# 让 pytest 能直接 import 插件源码
ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

_GIT_MISSING_MSG = "未检测到 git 可执行文件（git 未安装或不在 PATH），请先安装 git 后再安装插件"


def _make_installer(tmp_path):
    """构造一个最小可用的 PluginInstaller（仿既有测试的 __new__ 手法）"""
    from ui.installer import PluginInstaller

    plugins_dir = tmp_path / "plugins"
    cache_dir = tmp_path / "cache"
    plugins_dir.mkdir()
    cache_dir.mkdir()
    inst = PluginInstaller.__new__(PluginInstaller)
    inst._plugins_dir = plugins_dir
    inst._cache_dir = cache_dir
    inst.last_error = ""
    inst._suppress_backend_watcher = lambda *a, **k: None
    inst._resume_backend_watcher = lambda *a, **k: None
    inst._purge_plugin_module_cache = lambda *a, **k: None
    inst._manifest_cache = {}
    return inst


def test_popen_file_not_found_translated_to_git_not_found(tmp_path, monkeypatch):
    """git 不在 PATH 时（Popen 抛 FileNotFoundError）必须转抛 GitNotFoundError"""
    from ui.installer import GitNotFoundError, PluginInstaller

    def _popen_raise(*args, **kwargs):
        raise FileNotFoundError(2, "系统找不到指定的文件。")

    monkeypatch.setattr("ui.installer.subprocess.Popen", _popen_raise)
    inst = PluginInstaller.__new__(PluginInstaller)
    with pytest.raises(GitNotFoundError):
        inst._sparse_clone(
            "https://github.com/owner/repo.git",
            ".",
            "main",
            tmp_path / "cache",
        )


def test_format_git_err_returns_friendly_message():
    """GitNotFoundError 应格式化为可读中文文案（供 InfoBar / last_error 展示）"""
    from ui.installer import GitNotFoundError, PluginInstaller

    e = GitNotFoundError(_GIT_MISSING_MSG)
    formatted = PluginInstaller._format_git_err(e)
    assert formatted.startswith("未检测到 git")
    # 不应退化为 "{type}: {e}" 的英文异常名
    assert "GitNotFoundError:" not in formatted


def test_download_and_move_sets_last_error_on_git_missing(tmp_path, monkeypatch):
    """git 缺失时安装失败：返回 False 且 last_error 落 git 缺失文案（UI 读取源）"""
    from ui.installer import GitNotFoundError

    inst = _make_installer(tmp_path)

    def _boom(*args, **kwargs):
        raise GitNotFoundError(_GIT_MISSING_MSG)

    monkeypatch.setattr(inst, "_sparse_clone", _boom)
    ok = inst._download_and_move(
        "fake-plugin",
        "https://github.com/owner/repo.git",
        ".",
        "main",
        inst._plugins_dir / "fake-plugin",
    )
    assert ok is False
    assert "未检测到 git" in inst.last_error


def test_is_git_missing_detects_marker():
    """UI 层判定：含「未检测到 git」→ True；普通网络错误 → False"""
    from ui.cards import MarketplaceCard

    card = MarketplaceCard.__new__(MarketplaceCard)
    assert card._is_git_missing(f"exit=1 stderr={_GIT_MISSING_MSG}") is True
    assert card._is_git_missing("exit=128 stderr=fatal: unable to access") is False
    assert card._is_git_missing("") is False


def test_guide_install_git_hides_card_and_fills_input(monkeypatch):
    """git 缺失引导：隐藏市场卡 + 切回对话 + 输入框填入「本地安装git」并聚焦"""
    from ui.cards import MarketplaceCard

    class FakeCursor:
        End = 1024

        def __init__(self):
            self.moved = None

        def movePosition(self, pos):
            self.moved = pos

    class FakeInputArea:
        def __init__(self):
            self.text = None
            self.focused = False

        def setPlainText(self, t):
            self.text = t

        def textCursor(self):
            return FakeCursor()

        def setTextCursor(self, c):
            self._cursor = c

        def setFocus(self):
            self.focused = True

    class FakeMainWidget:
        input_area = FakeInputArea()

    class FakeWindow:
        @classmethod
        def get_instance(cls):
            return cls()

        def get_current_window(self):
            return FakeMainWidget()

    class FakeUI:
        hidden = []

        @classmethod
        def get_instance(cls):
            return cls()

        def hide_floating_card_globally(self, cid):
            FakeUI.hidden.append(cid)

    # 函数内 import 取模块属性：monkeypatch 模块级符号即可拦截
    from app.plugins.registries import ui_plugin_registry
    from app.widgets import tab_manager_window

    monkeypatch.setattr(ui_plugin_registry, "UIPluginRegistry", FakeUI)
    monkeypatch.setattr(tab_manager_window, "TabManagerWindow", FakeWindow)

    card = MarketplaceCard.__new__(MarketplaceCard)
    card._guide_install_git()

    assert FakeUI.hidden == ["plugin-marketplace"], "必须全局隐藏市场浮动卡"
    assert FakeMainWidget.input_area.text == "本地安装git", "输入框必须填入引导语"
    assert FakeMainWidget.input_area.focused is True, "输入框必须获得焦点"

