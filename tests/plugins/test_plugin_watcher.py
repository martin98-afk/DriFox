# -*- coding: utf-8 -*-
"""
PluginToolWatcher 插件工具热重载测试（插件化新行为，此前无覆盖）

覆盖：
1. scan_now 全量重扫注册（临时插件目录注入，不碰真实 .drifox/plugins）
2. scan_now 幂等：重复重扫不重复注册
3. 文件内容变更 → 重扫 → 注册更新（impl 更新）
4. 删除插件文件 → 重扫 → 工具注销（残留清理——旧 diff 实现 bug 回归点）
5. 跨根同名：工作树根优先（_root_tracker 判定）
6. _signature 变更检测（mtime_ns/size）
7. 下划线前缀模块跳过
8. P0-1：Settings 禁用插件 → scan_now 跳过其工具

运行: python -m pytest tests/test_plugin_watcher.py -v
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.plugins.loaders.plugin_tool_loader import PluginToolWatcher
from app.tools.registry import ToolRegistry


# 临时插件名：需在 enabled_plugins 白名单，否则被 P0-1 过滤
_TEST_PLUGIN = "watcher-test"


@pytest.fixture(autouse=True)
def fresh_registry():
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


@pytest.fixture(autouse=True)
def _enable_test_plugin(plugin_enabled):
    """把测试插件名临时加入 Settings.enabled_plugins（P0-1 加载过滤适配）。"""
    restore = plugin_enabled(_TEST_PLUGIN)
    yield
    restore()


def _make_plugin(root: Path, tool_name: str, content: str) -> Path:
    """构造 tools/ 目录结构插件：<root>/<plugin>/tools/<tool>.py，返回 py 路径。"""
    tools_dir = root / _TEST_PLUGIN / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    py = tools_dir / f"{tool_name}.py"
    py.write_text(content, encoding="utf-8")
    return py


def _register_snippet(tool_name: str, ret: str = "ok") -> str:
    return (
        "from app.tools.registry import DANGER_SAFE\n"
        f"def register(registry):\n"
        f"    registry.register(\n"
        f"        {tool_name!r},\n"
        f"        {{\"type\": \"function\", \"function\": {{\"name\": {tool_name!r}}}}},\n"
        f"        impl=lambda **kw: {ret!r},\n"
        f"        danger=DANGER_SAFE,\n"
        f"    )\n"
    )


def _make_watcher(root: Path, registry=None):
    return PluginToolWatcher(registry=registry or ToolRegistry.get_instance(), roots=[root])


class TestScanRegister:
    """scan_now 全量重扫注册"""

    def test_scan_registers_tools(self, tmp_path):
        _make_plugin(tmp_path, "w_read", _register_snippet("w_read"))
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        assert reg.get("w_read") is not None
        assert reg.get("w_read").source == f"plugin:{_TEST_PLUGIN}"
        assert reg.get("w_read").danger == "safe"

    def test_scan_idempotent_no_duplicate(self, tmp_path):
        _make_plugin(tmp_path, "w_read", _register_snippet("w_read"))
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        watcher.scan_now()
        watcher.scan_now()
        assert len(reg.names()) == 1, f"重复重扫不应重复注册，实际: {reg.names()}"

    def test_scan_loads_multiple_tools(self, tmp_path):
        _make_plugin(tmp_path, "w_a", _register_snippet("w_a"))
        _make_plugin(tmp_path, "w_b", _register_snippet("w_b"))
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        assert set(reg.names()) == {"w_a", "w_b"}

    def test_underscore_module_skipped(self, tmp_path):
        _make_plugin(tmp_path, "_private", _register_snippet("w_hidden"))
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        assert reg.names() == [], f"下划线前缀模块应跳过，实际: {reg.names()}"

    def test_no_register_fn_module_skipped(self, tmp_path):
        _make_plugin(tmp_path, "w_nofn", "x = 1\n")
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        assert reg.names() == []

    def test_broken_module_does_not_kill_others(self, tmp_path):
        _make_plugin(tmp_path, "w_bad", "raise RuntimeError('boom')\n")
        _make_plugin(tmp_path, "w_good", _register_snippet("w_good"))
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        assert "w_good" in reg.names(), f"坏模块不应影响其他插件加载，实际: {reg.names()}"


class TestScanHotUpdate:
    """文件变更 → 重扫 → 注册更新 / 删除 → 注销"""

    def _bump_mtime(self, py: Path):
        """强制修改文件时间戳（mtime_ns 变化，避免时钟粒度问题）"""
        old = py.stat().st_mtime_ns
        os.utime(py, ns=(old + 2_000_000_000, old + 2_000_000_000))

    def test_modify_file_updates_impl(self, tmp_path):
        py = _make_plugin(tmp_path, "w_read", _register_snippet("w_read", ret="v1"))
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        assert reg.get("w_read").impl(**{}) == "v1"

        # 修改文件内容 + 强制刷新时间戳 → 重扫 → impl 更新
        py.write_text(_register_snippet("w_read", ret="v2"), encoding="utf-8")
        self._bump_mtime(py)
        watcher.scan_now()
        assert reg.get("w_read").impl(**{}) == "v2", "文件内容变更后重扫应更新 impl"

    def test_reload_plugin_updates_impl(self, tmp_path):
        """D2 回归：对齐快路径命中后再变更，热更新 scan_now 仍更新 impl。

        曾因 scan_now 对齐快路径（_align_needed=False 即跳过）吞掉热更新：
        注册集未变（同名工具同签名）但 impl 语义已变，重复 scan 永不更新。
        force=True 默认语义下任意次 scan 后内容变更必须生效；force=False
        仅限启动对齐场景（无差异跳过）。
        """
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        py = _make_plugin(tmp_path, "w_read", _register_snippet("w_read", ret="v1"))
        watcher.scan_now()
        assert reg.get("w_read").impl(**{}) == "v1"

        # 模拟 deferred 对齐重扫：注册集无差异 → 快路径跳过（幂等）
        watcher.scan_now(force=False)
        assert reg.get("w_read").impl(**{}) == "v1"

        # 内容变更 → 热更新 scan_now（默认 force=True）→ impl 更新
        py.write_text(_register_snippet("w_read", ret="v2"), encoding="utf-8")
        self._bump_mtime(py)
        watcher.scan_now()
        assert reg.get("w_read").impl(**{}) == "v2", "对齐快路径不得吞掉热更新内容变更"
        # 再次对齐重扫：新 impl 稳定保持
        watcher.scan_now(force=False)
        assert reg.get("w_read").impl(**{}) == "v2"

    def test_delete_file_unregisters_tool(self, tmp_path):
        """旧 diff 实现 bug 回归：删除文件后必须注销残留工具"""
        py = _make_plugin(tmp_path, "w_read", _register_snippet("w_read"))
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        assert "w_read" in reg.names()

        py.unlink()
        watcher.scan_now()
        assert "w_read" not in reg.names(), "删除插件文件后重扫应注销残留工具"
        assert reg.get("w_read") is None

    def test_delete_one_of_many(self, tmp_path):
        _make_plugin(tmp_path, "w_a", _register_snippet("w_a"))
        py_b = _make_plugin(tmp_path, "w_b", _register_snippet("w_b"))
        reg = ToolRegistry.get_instance()
        watcher = _make_watcher(tmp_path, reg)
        watcher.scan_now()
        py_b.unlink()
        watcher.scan_now()
        assert "w_a" in reg.names()
        assert "w_b" not in reg.names()


class TestCrossRootPriority:
    """跨根同名：先扫描的根优先（工作树 plugins/ 先于用户目录）"""

    def test_first_root_wins(self, tmp_path):
        root1 = tmp_path / "root1"  # 工作树（先扫）
        root2 = tmp_path / "root2"  # 用户目录（后扫）
        root1.mkdir()
        root2.mkdir()
        _make_plugin(root1, "same_tool", _register_snippet("same_tool", ret="from-root1"))
        _make_plugin(root2, "same_tool", _register_snippet("same_tool", ret="from-root2"))

        reg = ToolRegistry.get_instance()
        watcher = PluginToolWatcher(registry=reg, roots=[root1, root2])
        watcher.scan_now()
        assert reg.get("same_tool") is not None
        assert reg.get("same_tool").impl(**{}) == "from-root1", "先扫描的根应优先（先注册者优先）"
        assert reg.get("same_tool").source == f"plugin:{_TEST_PLUGIN}"

    def test_second_root_tools_still_load(self, tmp_path):
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        _make_plugin(root1, "tool_a", _register_snippet("tool_a"))
        _make_plugin(root2, "tool_b", _register_snippet("tool_b"))
        reg = ToolRegistry.get_instance()
        watcher = PluginToolWatcher(registry=reg, roots=[root1, root2])
        watcher.scan_now()
        assert set(reg.names()) == {"tool_a", "tool_b"}


class TestSignature:
    """_signature 变更检测"""

    def test_signature_empty_for_missing_root(self, tmp_path):
        watcher = _make_watcher(tmp_path / "missing")
        assert watcher._signature() == ()

    def test_signature_changes_on_modify(self, tmp_path):
        py = _make_plugin(tmp_path, "w_read", _register_snippet("w_read"))
        watcher = _make_watcher(tmp_path)
        sig1 = watcher._signature()
        py.write_text(_register_snippet("w_read", ret="v2"), encoding="utf-8")
        old = py.stat().st_mtime_ns
        os.utime(py, ns=(old + 2_000_000_000, old + 2_000_000_000))
        sig2 = watcher._signature()
        assert sig1 != sig2, "文件修改后签名应变化"
        # 未修改 → 签名稳定
        assert watcher._signature() == sig2

    def test_signature_tracks_size(self, tmp_path):
        py = _make_plugin(tmp_path, "w_read", _register_snippet("w_read"))
        watcher = _make_watcher(tmp_path)
        sig1 = watcher._signature()
        # 同 mtime 不同 size（先固定 mtime 再改内容）
        st = py.stat()
        os.utime(py, ns=(st.st_mtime_ns, st.st_mtime_ns))
        py.write_text(_register_snippet("w_read", ret="longer-content-xxx"), encoding="utf-8")
        os.utime(py, ns=(st.st_mtime_ns, st.st_mtime_ns))
        sig2 = watcher._signature()
        assert sig1 != sig2, "文件 size 变化即使 mtime 相同签名也应变化"


class TestEnabledFilter:
    """P0-1：Settings 禁用插件 → scan_now 跳过其工具"""

    def test_disabled_plugin_tools_not_registered(self, tmp_path):
        from app.utils.config import Settings

        _make_plugin(tmp_path, "w_read", _register_snippet("w_read"))
        cfg = Settings.get_instance()
        saved = list(cfg.enabled_plugins.value or [])
        saved_disabled = list(cfg.disabled_plugins.value or [])
        try:
            # D8 双写禁用：enabled 移除 + disabled 加入
            cfg.enabled_plugins.value = [p for p in saved if p != _TEST_PLUGIN]
            cfg.disabled_plugins.value = saved_disabled + [_TEST_PLUGIN]
            reg = ToolRegistry.get_instance()
            watcher = _make_watcher(tmp_path, reg)
            watcher.scan_now()
            assert reg.names() == [], f"禁用插件的工具不应注册，实际: {reg.names()}"
        finally:
            cfg.enabled_plugins.value = saved
            cfg.disabled_plugins.value = saved_disabled


class TestCrossRootRestoreGating:
    """跨根恢复门控：仅当目标插件确实覆盖过低等级根工具时才重扫低等级根

    回归点：reload/unload 原为无条件 `_restore_lower_roots`，用户根插件
    （rank=1）每次启停 tools 都会把 system 根（rank=0）全部插件工具重扫
    重注册一遍——日志刷屏、多花 ~0.6s。改为按覆盖记账门控。
    """

    _USER_PLUGIN = "user-overrider"

    @pytest.fixture(autouse=True)
    def _clear_overrides(self):
        from app.plugins.loaders.plugin_tool_loader import _CROSS_ROOT_OVERRIDES

        _CROSS_ROOT_OVERRIDES.clear()
        yield
        _CROSS_ROOT_OVERRIDES.clear()

    @pytest.fixture()
    def user_root(self, tmp_path, monkeypatch):
        """把 app_data 目录指向 tmp，使 <tmp>/appdata/plugins 被 _root_kind 识别为 user 根"""
        import app.utils.utils as utils_mod

        app_data = tmp_path / "appdata"
        (app_data / "plugins").mkdir(parents=True)
        monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: app_data)
        return app_data / "plugins"

    def _make_user_plugin(self, user_root: Path, tool_name: str, ret: str) -> None:
        tools_dir = user_root / self._USER_PLUGIN / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        (tools_dir / "user_tool.py").write_text(_register_snippet(tool_name, ret=ret), encoding="utf-8")

    def test_unload_without_override_skips_lower_root_rescan(self, tmp_path, user_root, plugin_enabled, monkeypatch):
        sys_root = tmp_path / "sys_root"
        sys_root.mkdir()
        _make_plugin(sys_root, "sys_only_tool", _register_snippet("sys_only_tool", ret="from-sys"))
        self._make_user_plugin(user_root, "user_only_tool", ret="from-user")
        restore_enabled = plugin_enabled(self._USER_PLUGIN)
        try:
            reg = ToolRegistry.get_instance()
            watcher = PluginToolWatcher(registry=reg, roots=[sys_root, user_root])
            watcher.scan_now()
            assert reg.get("user_only_tool") is not None

            calls: list = []
            monkeypatch.setattr(watcher, "_restore_lower_roots", lambda rank: calls.append(rank))
            watcher.unload_plugin(self._USER_PLUGIN)
            assert calls == [], "未覆盖同级低等级根工具时不应重扫"
            assert reg.get("sys_only_tool") is not None, "无关的系统根工具应保持注册"
            assert reg.get("user_only_tool") is None
        finally:
            restore_enabled()

    def test_unload_with_override_restores_lower_root_tool(self, tmp_path, user_root, plugin_enabled):
        sys_root = tmp_path / "sys_root"
        sys_root.mkdir()
        _make_plugin(sys_root, "shared_tool", _register_snippet("shared_tool", ret="from-sys"))
        self._make_user_plugin(user_root, "shared_tool", ret="from-user")
        restore_enabled = plugin_enabled(self._USER_PLUGIN)
        try:
            reg = ToolRegistry.get_instance()
            watcher = PluginToolWatcher(registry=reg, roots=[sys_root, user_root])
            watcher.scan_now()
            assert reg.get("shared_tool").impl(**{}) == "from-user", "用户根应覆盖系统根同名工具"

            watcher.unload_plugin(self._USER_PLUGIN)
            restored = reg.get("shared_tool")
            assert restored is not None, "被覆盖的系统根工具必须恢复"
            assert restored.impl(**{}) == "from-sys"
        finally:
            restore_enabled()

    def test_reload_without_override_skips_lower_root_rescan(self, tmp_path, user_root, plugin_enabled, monkeypatch):
        sys_root = tmp_path / "sys_root"
        sys_root.mkdir()
        _make_plugin(sys_root, "sys_only_tool", _register_snippet("sys_only_tool", ret="from-sys"))
        self._make_user_plugin(user_root, "user_only_tool", ret="from-user")
        restore_enabled = plugin_enabled(self._USER_PLUGIN)
        try:
            reg = ToolRegistry.get_instance()
            watcher = PluginToolWatcher(registry=reg, roots=[sys_root, user_root])
            watcher.scan_now()

            calls: list = []
            monkeypatch.setattr(watcher, "_restore_lower_roots", lambda rank: calls.append(rank))
            watcher.reload_plugin(self._USER_PLUGIN)
            assert calls == [], "未覆盖低等级根工具时 reload 不应重扫"
            assert reg.get("user_only_tool") is not None, "目标插件自身工具应重新注册"
        finally:
            restore_enabled()
