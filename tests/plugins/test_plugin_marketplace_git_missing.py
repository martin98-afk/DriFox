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
    """git 缺失引导：隐藏市场卡 + 通过 input.write 能力把「本地安装 git」写入输入框

    能力取用走 ``caps_for``（按 manifest 声明裁剪 + 插件绑定窗口解析 host）。
    早前实现从 ``QApplication.topLevelWidgets()`` 找 ``_build_ui_capabilities``，
    Tab 模式下 MainWidget 已被 reparent 成 TabManagerWindow 子 widget，永远取
    不到池 → 引导静默失效（本测试不再提供该回退，直接断言 caps_for 路径）。
    """
    from ui.cards import MarketplaceCard

    written = {}

    def _fake_writer(text, append=False, focus=True):
        written["text"] = text
        written["focus"] = focus

    class FakeUI:
        hidden = []

        @classmethod
        def get_instance(cls):
            return cls()

        def hide_floating_card_globally(self, cid):
            FakeUI.hidden.append(cid)

    from app.plugins.registries import ui_plugin_registry
    import app.plugins.sdk as sdk

    monkeypatch.setattr(ui_plugin_registry, "UIPluginRegistry", FakeUI)
    monkeypatch.setattr(
        sdk,
        "caps_for",
        lambda cap, plugin="", surface="": _fake_writer if cap == "input.write" else None,
    )

    card = MarketplaceCard.__new__(MarketplaceCard)
    card._guide_install_git("demo-plugin")

    assert FakeUI.hidden == ["plugin-marketplace"], "必须全局隐藏市场浮动卡"
    prompt = written.get("text") or ""
    assert "demo-plugin" in prompt, "引导语必须带上失败插件名，模型才知道要重裝哪个"
    assert "git" in prompt.lower(), "引导语必须指向安装 git"
    # 关键：必须给出本平台可执行的安装命令，而不是泛泛一句「安装 git」
    assert any(
        k in prompt for k in ("winget", "brew", "apt", "dnf", "pacman", "git-scm.com")
    ), f"引导语必须含具体安装命令，实际: {prompt}"
    assert "git --version" in prompt, "引导语必须含验证步骤"
    assert written.get("focus") is True, "输入框必须获得焦点"


def test_guide_install_git_without_capability_does_not_raise(monkeypatch):
    """拿不到 input.write（能力未声明/池未就绪）时只记日志，不抛、不误写"""
    from ui.cards import MarketplaceCard
    import app.plugins.sdk as sdk

    class FakeUI:
        @classmethod
        def get_instance(cls):
            return cls()

        def hide_floating_card_globally(self, cid):
            pass

    from app.plugins.registries import ui_plugin_registry

    monkeypatch.setattr(ui_plugin_registry, "UIPluginRegistry", FakeUI)
    monkeypatch.setattr(sdk, "caps_for", lambda *a, **k: None)

    card = MarketplaceCard.__new__(MarketplaceCard)
    card._guide_install_git("demo-plugin")  # 不抛即通过
