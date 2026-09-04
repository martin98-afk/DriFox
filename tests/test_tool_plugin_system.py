# -*- coding: utf-8 -*-
"""
工具插件化系统测试 — registry / loader / 渲染联动 / 权限联动 / 热插拔

运行: python -m pytest tests/test_tool_plugin_system.py -v
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from app.tools.registry import DANGER_DANGEROUS, DANGER_SAFE, ToolRegistry
from app.plugins.loaders.plugin_tool_loader import load_plugin_tools, unload_plugin_tools


@pytest.fixture(autouse=True)
def fresh_registry():
    """每个测试前重置 registry（测试用）"""
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


@pytest.fixture(scope="module")
def qt_app():
    """Qt 应用（仅需要创建 QWidget 的测试使用，惰性创建）"""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app
    app.processEvents()


class TestRegistry:
    """注册表核心"""

    def test_register_metadata(self):
        reg = ToolRegistry.get_instance()
        ok = reg.register(
            "test_tool",
            {"type": "function", "function": {"name": "test_tool"}},
            impl=lambda **kw: "ok",
            danger="safe",
            icon="read",
            cn_name="测试工具",
            group="测试组",
            description="测试描述",
            aliases=["TestTool"],
            source="plugin:test",
        )
        assert ok
        r = reg.get("test_tool")
        assert r.danger == "safe"
        assert r.icon == "read"
        assert r.cn_name == "测试工具"
        assert r.group == "测试组"
        assert r.display_group == "测试组"
        assert r.description == "测试描述"
        assert r.aliases == ["TestTool"]
        assert r.is_plugin
        assert reg.version() == 1

    def test_plugin_without_danger_rejected(self):
        reg = ToolRegistry.get_instance()
        ok = reg.register(
            "no_danger",
            {"type": "function", "function": {"name": "no_danger"}},
            impl=lambda **kw: "x",
            source="plugin:x",
        )
        assert not ok

    def test_plugin_cannot_forge_builtin(self):
        reg = ToolRegistry.get_instance()
        ok = reg.register(
            "forge",
            {"type": "function", "function": {"name": "forge"}},
            danger="safe",
            source="builtin",
        )
        assert not ok  # 非 trusted 流程拒绝

    def test_trusted_builtin_allowed(self):
        reg = ToolRegistry.get_instance()
        ok = reg.register(
            "trusted_b",
            {"type": "function", "function": {"name": "trusted_b"}},
            danger="safe",
            source="builtin",
            trusted=True,
        )
        assert ok

    def test_unregister_and_version(self):
        reg = ToolRegistry.get_instance()
        reg.register("a", {"type": "function", "function": {"name": "a"}}, danger="safe", source="plugin:x")
        v1 = reg.version()
        assert reg.unregister("a")
        assert reg.version() == v1 + 1
        assert not reg.unregister("a")  # 幂等

    def test_group_map_danger_first(self):
        reg = ToolRegistry.get_instance()
        reg.register(
            "safe1", {"type": "function", "function": {"name": "safe1"}}, danger="safe", group="G", source="plugin:x"
        )
        reg.register(
            "danger1",
            {"type": "function", "function": {"name": "danger1"}},
            danger="dangerous",
            group="G",
            source="plugin:x",
        )
        groups = reg.group_map()
        assert [r.name for r in groups["G"]] == ["danger1", "safe1"]

    def test_display_group_fallback(self):
        reg = ToolRegistry.get_instance()
        reg.register("s1", {"type": "function", "function": {"name": "s1"}}, danger="safe", source="plugin:x")
        reg.register("d1", {"type": "function", "function": {"name": "d1"}}, danger="dangerous", source="plugin:x")
        assert reg.get_group("s1") == "安全操作"
        assert reg.get_group("d1") == "其他"

    def test_on_change_listener(self):
        reg = ToolRegistry.get_instance()
        versions = []
        reg.on_change(lambda v: versions.append(v))  # 立即回调一次
        reg.register("b", {"type": "function", "function": {"name": "b"}}, danger="safe", source="plugin:x")
        assert len(versions) == 2  # 初始 + 注册
        assert versions[1] == 1


class TestSystemPluginTools:
    """系统插件工具（plugins/system/tools/）"""

    def test_all_30_tools_registered(self):
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)
        names = set(reg.names())
        # 系统插件固定 30 个工具；codegraph_explore 来自社区插件 codegraph-tools
        # （引擎插件化后迁出系统插件，未安装时不注册），单独按可用性断言。
        expected = {
            "read",
            "write",
            "edit",
            "multi_edit",
            "grep",
            "list",
            "glob",
            "scan_repo",
            "stage_files",
            "websearch",
            "webfetch",
            "bash",
            "bg_start",
            "bg_stop",
            "bg_logs",
            "bg_list",
            "todowrite",
            "todoread",
            "get_diagnostics",
            "lsp",
            "subagent_para",
            "subagent_status",
            "subagent_dag",
            "team_send_message",
            "team_list_members",
            "question",
            "skill",
            "manage_skill",
            "mcp_list_servers",
            "upload_file",
        }
        assert expected <= names, f"缺失: {expected - names}"
        # codegraph_explore：依赖 .drifox/plugins/codegraph-tools/ 社区插件（可能未安装）
        if "codegraph_explore" in names:
            assert reg.get_danger("codegraph_explore") == DANGER_SAFE

    def test_danger_classification(self):
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)
        assert reg.get_danger("write") == DANGER_DANGEROUS
        assert reg.get_danger("bash") == DANGER_DANGEROUS
        assert reg.get_danger("read") == DANGER_SAFE
        assert reg.get_danger("websearch") == DANGER_SAFE
        assert reg.get_danger("subagent_para") == DANGER_DANGEROUS
        assert reg.get_danger("subagent_status") == DANGER_SAFE

    def test_icon_cn_name_group(self):
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)
        assert reg.get_icon("read") == "read"
        assert reg.get_icon("write") == "编辑"
        assert reg.get_cn_name("read") == "读取"
        assert reg.get_cn_name("bash") == "执行命令"
        assert reg.get_group("read") == "文件读取"
        assert reg.get_group("bash") == "终端与后台"

    def test_aliases_registered(self):
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)
        assert "Read" in reg.get_aliases("read")
        assert "Edit" in reg.get_aliases("edit")
        assert "WebSearch" in reg.get_aliases("websearch")


class TestHotPlug:
    """热插拔：临时插件工具注册→注销"""

    @pytest.fixture(autouse=True)
    def _enable_hotplug_plugin(self):
        """P0-1 适配：临时插件 hotplug-test 加入 enabled_plugins。

        加载过滤以 Settings.enabled_plugins 为准；测试环境不隔离 Settings，
        须将临时插件名临时注入启用列表，否则会被「已禁用插件」过滤跳过。
        """
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        try:
            saved = list(cfg.enabled_plugins.value or [])
        except Exception:
            saved = []
        cfg.enabled_plugins.value = saved + ["hotplug-test"]
        yield
        cfg.enabled_plugins.value = saved

    def _make_temp_plugin(self, tmpdir: Path, tool_name: str, content: str) -> Path:
        tools_dir = tmpdir / "hotplug-test" / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        py = tools_dir / f"{tool_name}.py"
        py.write_text(content, encoding="utf-8")
        return tmpdir

    def test_plugin_register_and_unload(self, tmp_path):
        reg = ToolRegistry.get_instance()
        root = self._make_temp_plugin(
            tmp_path,
            "hello_plug",
            """
from app.tools.result import ToolResult

def _impl(**kwargs):
    return ToolResult(True, content="hotplug ok")

def register(registry):
    registry.register(
        "hello_plug",
        {"type": "function", "function": {"name": "hello_plug", "description": "hotplug demo"}},
        impl=_impl, danger="safe", icon="工具", cn_name="热插拔",
        group="测试", description="热插拔测试",
    )
""",
        )
        loaded = load_plugin_tools(registry=reg, plugin_roots=[root])
        assert "hello_plug" in reg.names()
        assert reg.get_cn_name("hello_plug") == "热插拔"
        assert reg.get("hello_plug").source == "plugin:hotplug-test"

        # 注销
        unload_plugin_tools("hotplug-test", loaded.get("hotplug-test", set()), reg)
        assert "hello_plug" not in reg.names()

    def test_worktree_priority(self, tmp_path):
        """同名工具先注册者优先（工作树 plugins/ 优先于用户目录）"""
        reg = ToolRegistry.get_instance()
        user_root = self._make_temp_plugin(
            tmp_path,
            "read",
            """
def register(registry):
    registry.register(
        "read",
        {"type": "function", "function": {"name": "read"}},
        impl=lambda **kw: "user", danger="safe",
    )
""",
        )
        load_plugin_tools(registry=reg, plugin_roots=[user_root])
        # 系统插件后加载，不应覆盖用户同名（此处无系统，验证 proxy 拒绝重复源）
        assert reg.get("read").source == "plugin:hotplug-test"


class TestUserOverrideSystem:
    """跨根优先级：user 插件可覆盖 system 插件的同名工具。

    设计：
    - root_kind 优先级 system(0) < user(1)
    - 高等级根可覆盖低等级根的同名工具；同根/反向/同级按先注册者优先
    - root_tracker 在 watcher 中持久，跨 scan_now 保留覆盖关系
    """

    @pytest.fixture(autouse=True)
    def _enable_override_plugin(self):
        """启用临时 user override 插件"""
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = list(cfg.enabled_plugins.value or [])
        try:
            cfg.enabled_plugins.value = list(set(saved + ["user-override-plug"]))
            yield
        finally:
            cfg.enabled_plugins.value = saved

    @staticmethod
    def _make_user_override_plugin(user_root: Path) -> Path:
        """在 user_root/user-override-plug/tools/override.py 写一个 read 覆盖

        注意：user_root 必须等于 get_app_data_dir()/plugins，否则 _root_kind
        无法识别为 user 等级。
        """
        plug = user_root / "user-override-plug"
        plug.mkdir(parents=True, exist_ok=True)
        tools = plug / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "override.py").write_text(
            """
def register(registry):
    registry.register(
        "read",
        {"type": "function", "function": {"name": "read"}},
        impl=lambda **kw: "USER-VERSION",
        danger="safe",
        cn_name="读取（用户覆盖）",
        description="由用户插件覆盖",
    )
""",
            encoding="utf-8",
        )
        return user_root

    def _setup_user_root(self, tmp_path, monkeypatch):
        """把 user_root 设为 tmp_path（_root_kind 必须能识别）

        返回 (system_root, user_root)。
        注意：_PLUGIN_ROOTS 是模块级缓存，测试必须显式传 plugin_roots 参数，
        否则 monkeypatch 改 get_app_data_dir 不会影响已缓存的 _PLUGIN_ROOTS。
        """
        monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: tmp_path)
        user_root = tmp_path / "plugins"
        self._make_user_override_plugin(user_root)
        system_root = Path(__file__).parent.parent / "plugins"
        return system_root, user_root

    def test_root_kind_priority_constants(self):
        """_ROOT_KIND_PRIORITY 必须为 system < user"""
        from app.plugins.loaders.plugin_tool_loader import (
            _ROOT_KIND_PRIORITY,
            _ROOT_KIND_SYSTEM,
            _ROOT_KIND_USER,
        )

        assert _ROOT_KIND_PRIORITY[_ROOT_KIND_SYSTEM] < _ROOT_KIND_PRIORITY[_ROOT_KIND_USER]

    def test_root_kind_recognizes_user_root(self, tmp_path, monkeypatch):
        """_root_kind 必须把 <app_data>/plugins 识别为 user（前置条件）"""
        from app.plugins.loaders.plugin_tool_loader import _root_kind, _ROOT_KIND_USER

        monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: tmp_path)
        assert _root_kind(tmp_path / "plugins") == _ROOT_KIND_USER

    def test_user_plugin_overrides_system(self, tmp_path, monkeypatch):
        """场景 1：完整加载（system + user）→ user 覆盖 system 同名"""
        system_root, user_root = self._setup_user_root(tmp_path, monkeypatch)
        reg = ToolRegistry.get_instance()

        # 显式传 plugin_roots（_PLUGIN_ROOTS 是模块级缓存，monkeypatch 不会刷新）
        tracker = {}
        load_plugin_tools(registry=reg, plugin_roots=[system_root, user_root], root_tracker=tracker)
        # user 覆盖 system：read.source 应为 user 插件名
        assert reg.get("read").source == "plugin:user-override-plug"
        # cn_name 是 user 版本
        assert reg.get("read").cn_name == "读取（用户覆盖）"
        # root_tracker 记录 read 来自 user 根
        assert tracker.get("read") == user_root
        # metadata._plugin_root_kind 为 user
        assert reg.get("read").metadata.get("_plugin_root_kind") == "user"

    def test_system_reload_preserves_user_override(self, tmp_path, monkeypatch):
        """场景 2：仅重扫 system 根，user 覆盖应保留"""
        system_root, user_root = self._setup_user_root(tmp_path, monkeypatch)
        reg = ToolRegistry.get_instance()

        tracker = {}
        load_plugin_tools(registry=reg, plugin_roots=[system_root, user_root], root_tracker=tracker)
        assert reg.get("read").source == "plugin:user-override-plug"

        # 仅重扫 system 根：复用同一 tracker
        load_plugin_tools(registry=reg, plugin_roots=[system_root], root_tracker=tracker)
        # user 覆盖仍然存在（system 不能再覆盖回）
        assert reg.get("read").source == "plugin:user-override-plug"
        assert reg.get("read").cn_name == "读取（用户覆盖）"

    def test_watcher_scan_preserves_user_override(self, tmp_path, monkeypatch):
        """场景 3：watcher 重扫后 user 覆盖关系保持

        回归：旧实现 root_tracker 每次 load_plugin_tools 都新建，
        跨根保护失效，导致 user 覆盖在 scan_now 后被还原为 system。
        修复：watcher.scan_now 传入自身 _root_tracker。
        """
        system_root, user_root = self._setup_user_root(tmp_path, monkeypatch)
        reg = ToolRegistry.get_instance()

        from app.plugins.loaders.plugin_tool_loader import PluginToolWatcher

        watcher = PluginToolWatcher(registry=reg, roots=[system_root, user_root])
        watcher.scan_now()  # 触发首次扫描 + user 覆盖
        assert reg.get("read").source == "plugin:user-override-plug"

        # 再触发一次重扫：模拟 watcher 周期检测到变更
        watcher.scan_now()
        assert reg.get("read").source == "plugin:user-override-plug"
        assert reg.get("read").cn_name == "读取（用户覆盖）"

    def test_user_vs_user_first_wins(self, tmp_path, monkeypatch):
        """场景 4：两个用户插件同名 → 同根内先注册者优先（避免互相覆盖打架）"""
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        old = list(cfg.enabled_plugins.value or [])
        try:
            cfg.enabled_plugins.value = list(set(old + ["user-override-plug", "user-override-plug-2"]))

            system_root, user_root = self._setup_user_root(tmp_path, monkeypatch)
            # 第二个用户插件
            plug2 = user_root / "user-override-plug-2"
            plug2.mkdir(parents=True, exist_ok=True)
            (plug2 / "tools").mkdir(parents=True, exist_ok=True)
            (plug2 / "tools" / "override.py").write_text(
                """
def register(registry):
    registry.register(
        "read",
        {"type": "function", "function": {"name": "read"}},
        impl=lambda **kw: "USER2",
        danger="safe",
        cn_name="读取（用户覆盖 2）",
    )
""",
                encoding="utf-8",
            )

            reg = ToolRegistry.get_instance()
            tracker = {}
            load_plugin_tools(registry=reg, plugin_roots=[user_root], root_tracker=tracker)
            # user-override-plug 先注册（_iter_tool_modules 按 sorted 顺序遍历）
            # user-override-plug-2 同根内后注册，应被拒绝
            assert reg.get("read").source == "plugin:user-override-plug"
        finally:
            cfg.enabled_plugins.value = old

    def test_metadata_root_kind_injected_for_all_plugins(self):
        """场景 5：所有插件工具的 metadata._plugin_root_kind 都被正确注入"""
        reg = ToolRegistry.get_instance()
        # 显式只加载 system_root，隔离用户插件（如 hashline-edit 覆盖 read）干扰
        system_root = Path(__file__).parent.parent / "plugins"
        load_plugin_tools(registry=reg, plugin_roots=[system_root])
        # 系统插件工具：kind=system
        for name in ("read", "write", "bash"):
            reg_obj = reg.get(name)
            assert reg_obj.metadata.get("_plugin_root_kind") == "system", f"{name} root_kind 应为 system"

    def test_get_meta_includes_source(self):
        """get_meta 暴露 source 字段，权限卡片已可用"""
        reg = ToolRegistry.get_instance()
        system_root = Path(__file__).parent.parent / "plugins"
        load_plugin_tools(registry=reg, plugin_roots=[system_root])
        meta = reg.get_meta("read")
        assert "source" in meta
        assert meta["source"].startswith("plugin:")

    def test_removing_user_plugin_restores_system(self, tmp_path, monkeypatch):
        """场景 6：覆盖 system 的用户插件被删除 → 系统插件恢复

        回归：旧实现 _run_register 用 after-before diff 记录 _loaded，
        覆盖场景 diff 为空 → watcher 漏记被覆盖工具 → 用户插件删除后
        system 无法恢复（幽灵残留）。修复：proxy.registered_names 显式记录
        + unload 时清理 root_tracker 残留。
        """
        system_root, user_root = self._setup_user_root(tmp_path, monkeypatch)
        from app.plugins.loaders.plugin_tool_loader import PluginToolWatcher

        reg = ToolRegistry.get_instance()
        watcher = PluginToolWatcher(registry=reg, roots=[system_root, user_root])

        # 1) 用户插件覆盖 system
        watcher.scan_now()
        assert reg.get("read").source == "plugin:user-override-plug"
        assert "read" in watcher._loaded.get("user-override-plug", set()), (
            "watcher 必须记录被覆盖的工具（否则删除后无法注销）"
        )

        # 2) 删除用户插件目录 → 重扫 → system 恢复
        import shutil

        shutil.rmtree(user_root / "user-override-plug", ignore_errors=True)
        watcher.scan_now()
        r = reg.get("read")
        assert r is not None, "read 不应丢失"
        assert r.source == "plugin:system", f"system 应恢复，实际: {r.source}"
        assert r.cn_name != "读取（用户覆盖）", "cn_name 应为 system 原始值"

    def test_unload_plugin_precise_no_other_plugin_touched(self, tmp_path, monkeypatch):
        """场景 6b：unload_plugin 精准卸载单插件，不波及他插件

        回归：此前 _reload_tools 删除路径调 scan_now 全量重扫，
        会先注销全部插件工具再重注册——删一个插件却卸载并重载全部工具。
        修复后 unload_plugin 只注销目标插件名下工具，且跨根覆盖由
        低等级根重扫恢复（不注销他插件）。
        """
        import shutil

        system_root, user_root = self._setup_user_root(tmp_path, monkeypatch)
        from app.plugins.loaders.plugin_tool_loader import PluginToolWatcher

        reg = ToolRegistry.get_instance()
        watcher = PluginToolWatcher(registry=reg, roots=[system_root, user_root])
        watcher.scan_now()
        # user 覆盖 read 生效
        assert reg.get("read").source == "plugin:user-override-plug"

        # 捕获删除前 user 插件工具集
        pre_loaded = set(watcher._loaded.get("user-override-plug", set()))
        assert pre_loaded, "watcher 应记录 user 插件工具"

        # spy：记录所有被 unregister 的工具名
        unregistered: list[str] = []
        orig_unregister = reg.unregister

        def _spy(name):
            unregistered.append(name)
            return orig_unregister(name)

        monkeypatch.setattr(reg, "unregister", _spy)

        # 删除 user 插件目录 → 精准卸载
        shutil.rmtree(user_root / "user-override-plug", ignore_errors=True)
        watcher.unload_plugin("user-override-plug")

        # 1) 只注销了 user 插件名下工具（精准）
        assert set(unregistered) == pre_loaded, f"应只注销 user 插件工具 {pre_loaded}，实际注销 {set(unregistered)}"
        # 2) 他插件（system）工具未被注销——write 是独立 system 工具
        assert "write" not in unregistered, "system 工具 write 不应被 unregister"
        # 3) 跨根覆盖恢复：read 恢复为 system
        r = reg.get("read")
        assert r is not None and r.source == "plugin:system", f"system 应恢复，实际: {r.source if r else None}"
        # 4) watcher._loaded 已移除该插件记录
        assert "user-override-plug" not in watcher._loaded

    def test_reload_plugin_precise_no_other_plugin_touched(self, tmp_path, monkeypatch):
        """场景 6c：reload_plugin 精准重载单插件（更新/安装路径），不波及他插件

        回归：此前更新路径调 scan_now 全量重扫——装/改一个插件同样
        卸载并重载全部工具。reload_plugin 只注销目标插件旧工具 →
        恢复被覆盖的 system 同名工具 → 重注册目标插件当前模块。
        """
        system_root, user_root = self._setup_user_root(tmp_path, monkeypatch)
        from app.plugins.loaders.plugin_tool_loader import PluginToolWatcher

        reg = ToolRegistry.get_instance()
        watcher = PluginToolWatcher(registry=reg, roots=[system_root, user_root])
        watcher.scan_now()
        assert reg.get("read").source == "plugin:user-override-plug"

        pre_loaded = set(watcher._loaded.get("user-override-plug", set()))
        assert pre_loaded, "watcher 应记录 user 插件工具"

        # spy：记录所有被 unregister 的工具名
        unregistered: list[str] = []
        orig_unregister = reg.unregister

        def _spy(name):
            unregistered.append(name)
            return orig_unregister(name)

        monkeypatch.setattr(reg, "unregister", _spy)

        # 模拟热更新：read 改 cn_name + 新增 readme 工具
        (user_root / "user-override-plug" / "tools" / "override.py").write_text(
            """
def register(registry):
    registry.register(
        "read",
        {"type": "function", "function": {"name": "read"}},
        impl=lambda **kw: "USER-VERSION-2",
        danger="safe",
        cn_name="读取（用户覆盖 2）",
        description="由用户插件覆盖",
    )
    registry.register(
        "readme",
        {"type": "function", "function": {"name": "readme"}},
        impl=lambda **kw: "README",
        danger="safe",
        cn_name="说明（用户）",
    )
""",
            encoding="utf-8",
        )
        watcher.reload_plugin("user-override-plug")

        # 1) 只注销了 user 插件名下旧工具（精准）
        assert set(unregistered) == pre_loaded, f"应只注销 user 插件旧工具 {pre_loaded}，实际注销 {set(unregistered)}"
        # 2) 他插件（system）工具未被注销——write 是独立 system 工具
        assert "write" not in unregistered, "system 工具 write 不应被 unregister"
        # 3) read 仍由 user 覆盖且内容更新（跨根覆盖保持 + 新代码生效）
        r = reg.get("read")
        assert r is not None and r.source == "plugin:user-override-plug", (
            f"read 应仍由 user 覆盖，实际: {r.source if r else None}"
        )
        assert r.cn_name == "读取（用户覆盖 2）", "热更新后的 cn_name 应生效"
        # 4) 新增工具已注册
        r2 = reg.get("readme")
        assert r2 is not None and r2.source == "plugin:user-override-plug", "新工具 readme 应注册"
        # 5) watcher._loaded 更新为新工具集
        assert watcher._loaded.get("user-override-plug") == {"read", "readme"}, (
            f"_loaded 应更新为新工具集，实际: {watcher._loaded.get('user-override-plug')}"
        )
        # 6) 再触发一次全量重扫：状态应与 reload_plugin 结果一致（无漂移）
        watcher.scan_now()
        assert reg.get("read").source == "plugin:user-override-plug"
        assert reg.get("readme").source == "plugin:user-override-plug"

    def test_disabling_user_plugin_restores_system(self, tmp_path, monkeypatch):
        """场景 7：覆盖 system 的用户插件被禁用 → 系统插件恢复

        与删除等价：enabled_plugins 移除插件名后，watcher 重扫跳过其工具。
        """
        from app.utils.config import Settings

        system_root, user_root = self._setup_user_root(tmp_path, monkeypatch)
        from app.plugins.loaders.plugin_tool_loader import PluginToolWatcher

        cfg = Settings.get_instance()
        old = list(cfg.enabled_plugins.value or [])
        reg = ToolRegistry.get_instance()
        watcher = PluginToolWatcher(registry=reg, roots=[system_root, user_root])

        # 覆盖生效
        watcher.scan_now()
        assert reg.get("read").source == "plugin:user-override-plug"

        # 禁用用户插件（从 enabled_plugins 移除）
        cfg.enabled_plugins.value = [p for p in old if p != "user-override-plug"]
        watcher.scan_now()
        r = reg.get("read")
        assert r is not None and r.source == "plugin:system", f"禁用后 system 应恢复，实际: {r.source if r else None}"
        cfg.enabled_plugins.value = old


class TestSourceLabelRender:
    """工具权限卡片来源标签渲染（参考 hook 配置卡片的 sourceLabel 色块风格）"""

    def test_format_source_label_builtin(self):
        from app.widgets.cards.settings.tool_control_card import _format_source_label

        color, text = _format_source_label("builtin", "")
        assert color == "#888"
        assert text == "内置"

    def test_format_source_label_system_plugin(self):
        from app.widgets.cards.settings.tool_control_card import _format_source_label

        color, text = _format_source_label("plugin:system", "system")
        assert color == "#e74c3c"
        assert text == "system"  # 直接显示插件名，无前缀

    def test_format_source_label_user_plugin(self):
        from app.widgets.cards.settings.tool_control_card import _format_source_label

        color, text = _format_source_label("plugin:my-plug", "user")
        assert color == "#2ecc71"
        assert text == "my-plug"  # 直接显示插件名，无前缀

    def test_format_source_label_long_plugin_truncated(self):
        from app.widgets.cards.settings.tool_control_card import _format_source_label

        # 长插件名截断（> 8 字符加 …）
        # 长插件名截断（> 8 字符加 …）
        long_name = "plugin:verylongpluginname"
        _, text = _format_source_label(long_name, "user")
        assert text == "verylong…"
        _, text = _format_source_label(long_name, "user")
        assert "…" in text or len(text.split("·", 1)[1].rstrip("…")) <= 8

    def test_card_row_has_source_label(self, qt_app):
        """工具行构建后存在来源 QLabel（与 hook 卡片 sourceLabel 同位置/风格）"""
        from PyQt5.QtWidgets import QLabel
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController
        from app.widgets.cards.settings.tool_control_card import ToolControlCardContent

        # 显式只加载 system_root，隔离用户插件（hashline-edit 覆盖 read）干扰
        system_root = Path(__file__).parent.parent / "plugins"
        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg, plugin_roots=[system_root])

        pc = ToolPermissionController()
        card = ToolControlCardContent(controller=pc)
        card.show_content()

        # 收集来源标签文本（内置 / 纯插件名）
        all_labels = card.findChildren(QLabel)
        source_texts = {lbl.text() for lbl in all_labels}
        # 至少存在系统插件来源标签（插件名 system）或内置标签
        assert "system" in source_texts or "内置" in source_texts, f"缺来源标签: {source_texts}"

        card.deleteLater()
        pc.deleteLater()
        qt_app.processEvents()


class TestRenderLinkage:
    """渲染联动：图标 / 中文名从 registry 读"""

    def test_render_helpers(self):
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import _get_tool_cn_name, _get_tool_icon_name

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        assert _get_tool_icon_name("read") == "read"
        assert _get_tool_icon_name("bash") == "shell"
        assert _get_tool_icon_name("unknown_xyz") == "工具"  # 未注册回退
        assert _get_tool_cn_name("read") == "读取"
        assert _get_tool_cn_name("bash") == "执行命令"
        assert _get_tool_cn_name("unknown_xyz") == "unknown_xyz"
        # MCP 特殊处理
        assert _get_tool_cn_name("mcp__server__fetch") == "fetch"

    def test_tool_classifier(self):
        from app.tools.registry import ToolRegistry
        from app.tools.tool_classifier import classify_tool_danger, get_tool_counts

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        assert classify_tool_danger("write") == "dangerous"
        assert classify_tool_danger("read") == "safe"
        counts = get_tool_counts({"write": True, "read": True, "websearch": False})
        assert counts == (1, 1)

    def test_tool_name_mapper(self):
        from app.tools.registry import ToolRegistry
        from app.tools.tool_name_mapper import ToolNameMapper

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        assert ToolNameMapper.to_native("Read") == "read"
        assert ToolNameMapper.to_native("Edit") == "edit"
        assert ToolNameMapper.to_claude_style("edit") == "Edit"
        assert ToolNameMapper.is_known("Read")
        assert not ToolNameMapper.is_known("nonsense_tool")
        # 动态 ALIAS_MAP 兼容
        assert "read" in ToolNameMapper.ALIAS_MAP


class TestPermissionLinkage:
    """权限联动：权限控制器 / 卡片从 registry 驱动"""

    def test_permission_controller(self, qt_app):
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        pc = ToolPermissionController()
        toggles = pc.get_toggles()
        # 系统插件工具基线 30；workbuddy 插件新增 wb_plan/present_files/wb_read_me/wb_tool_search
        # 共 4 个工具 → 基线 34。codegraph_explore 来自社区插件 codegraph-tools，
        # 未安装时不注册。用动态下界兼容未来新增：>= 30；精确 34 仅在无 codegraph 时成立。
        assert len(toggles) >= 30
        assert (
            len(toggles) == 34
            or (len(toggles) == 35 and "codegraph_explore" in toggles)
            or (len(toggles) == 31 and "codegraph_explore" in toggles)  # 仅 codegraph，无 workbuddy
            or len(toggles) == 30  # 极简环境（workbuddy/codegraph 均未加载）
        ), f"工具数异常: {len(toggles)} ({sorted(toggles.keys())})"
        assert toggles["read"] is True
        pc.deleteLater()
        qt_app.processEvents()

    def test_control_card_groups(self, qt_app):
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController
        from app.widgets.cards.settings.tool_control_card import ToolControlCardContent

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        pc = ToolPermissionController()
        card = ToolControlCardContent(controller=pc)
        groups = dict(card._get_groups())
        assert "文件读取" in groups
        assert "终端与后台" in groups
        assert "read" in groups["文件读取"]
        assert "bash" in groups["终端与后台"]
        card.deleteLater()
        pc.deleteLater()
        qt_app.processEvents()

    def test_registry_burst_change_debounced(self, qt_app):
        """回归：watcher 重扫会逐个注销+重注册全部工具（几十次 change 事件），
        卡片必须合并为一次全量重建，不能逐个排队（曾导致 ~180ms/次 × 35 次刷屏 6s）"""
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController
        from app.widgets.cards.settings.tool_control_card import ToolControlCardContent

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        pc = ToolPermissionController()
        card = ToolControlCardContent(controller=pc)
        card.show_content()  # 首次构建
        card.show()  # 模拟卡片可见（隐藏时变更延迟到下次显示，重建只在可见时发生）
        assert card._built
        # 清掉 _bind_registry() 注册时立即回调的伪变更（_queued_built=False 不重建，
        # 但会残留 pending 排队，影响下方"35 次变更合并为 1 次"的计数断言）
        qt_app.processEvents()
        qt_app.processEvents()
        assert not card._rebuild_pending

        rebuild_calls = []
        card._rebuild = lambda: rebuild_calls.append(1)

        # 模拟重扫：35 次 change 事件连续到达（version 同步真实递增，
        # 与 ToolControlCardContent._do_rebuild 的 version 稳定性合并对齐）
        base = reg.version()
        for i in range(35):
            card._on_registry_changed(base + i + 1)
            reg._version += 1  # 同步真实 version（watcher 重扫每次 change 都真实递增）
        assert card._rebuild_pending  # 同批变更已合并，仅排队一次

        qt_app.processEvents()
        qt_app.processEvents()
        assert card._rebuild_pending is False
        assert len(rebuild_calls) == 1  # 35 次变更只重建 1 次

        card.deleteLater()
        pc.deleteLater()
        qt_app.processEvents()

    def test_registry_change_from_bg_thread_rebuilds(self, qt_app):
        """回归：热重载 watcher 在后台线程触发 registry 变更（unregister/register 同步
        notify listener），卡片必须经信号桥接在主线程重建，且重建后发射
        togglesChanged（main_widget 连它刷新工具栏按钮的「危险 X 安全 Y」计数）。

        旧实现在后台线程直接 QTimer.singleShot(0, ...) 跨线程操作 Qt 定时器，
        且重建完成后不发射 togglesChanged → 工具栏按钮计数保持旧值，
        只有新建窗口（重新初始化按钮）才显示正确数量。
        """
        import threading

        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController
        from app.widgets.cards.settings.tool_control_card import ToolControlCardContent

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        pc = ToolPermissionController()
        card = ToolControlCardContent(controller=pc)
        card.show_content()  # 首次构建
        card.show()  # 模拟卡片可见（隐藏时变更延迟到下次显示，重建只在可见时发生）
        before = len(card._toggle_widgets)
        assert before > 0

        # 模拟 main_widget 连接：togglesChanged -> 刷新工具栏计数
        refresh_payloads = []
        card.togglesChanged.connect(refresh_payloads.append)

        new_tool = "_bg_reload_test_tool"
        assert new_tool not in reg.names()
        errors = []

        def bg():
            try:
                reg.unregister("read")
                reg.register(
                    new_tool,
                    schema={"type": "object"},
                    impl=lambda **kw: "ok",
                    danger="safe",
                    source="plugin:test",
                    cn_name="后台热重载测试",
                )
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=bg)
        t.start()
        t.join()
        assert not errors, errors
        assert new_tool in reg.names()

        # 处理 QueuedConnection 信号 + 去抖定时器
        for _ in range(5):
            qt_app.processEvents()

        assert not card._rebuild_pending
        assert "read" not in card._toggle_widgets  # 被注销工具消失
        assert new_tool in card._toggle_widgets  # 新工具出现
        assert len(card._toggle_widgets) == before  # 数量同步（-1 +1）

        # 重建后必须通知上层刷新计数（工具栏按钮动态读 registry）
        assert refresh_payloads, "热重载后未发射 togglesChanged（工具栏按钮计数不会刷新）"
        latest = refresh_payloads[-1]
        assert new_tool in latest and "read" not in latest  # 载荷同步到最新工具集

        card.deleteLater()
        pc.deleteLater()
        qt_app.processEvents()


class TestPerToolPolicy:
    """per-tool 关闭策略(T3):设置/持久化/回退/UI 联动/生效层一致性"""

    @staticmethod
    def _snapshot_settings():
        from app.utils.config import Settings

        s = Settings.get_instance()
        return (s.tool_toggles.value, s.tool_off_behavior.value, s.tool_permission_policy.value)

    @staticmethod
    def _restore_settings(snap):
        from app.utils.config import Settings

        s = Settings.get_instance()
        s.tool_toggles.value, s.tool_off_behavior.value, s.tool_permission_policy.value = snap
        s.save()

    def test_tool_policy_set_persist_fallback(self, qt_app):
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            pc = ToolPermissionController()
            # 初始:无 per-tool 策略 → 回退全局 behavior(默认 deny)
            assert pc.get_tool_policy("read") == "deny"
            # 设置 per-tool 策略
            pc.set_user_tool_policy("read", "ask")
            assert pc.get_tool_policy("read") == "ask"
            # 持久化到 Settings
            from app.utils.config import Settings

            assert Settings.get_instance().tool_permission_policy.value.get("read") == "ask"
            # 其他工具不受影响(仍回退全局)
            assert pc.get_tool_policy("bash") == "deny"
            # 非法值被忽略
            pc.set_user_tool_policy("bash", "banana")
            assert pc.get_tool_policy("bash") == "deny"
            pc.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_apply_agent_generates_policies_and_copy(self, qt_app):
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            pc = ToolPermissionController()
            pc.set_user_tool_policy("read", "ask")
            # apply_agent:active 侧生成 agent 权限策略(ask→ask/deny→deny),user 偏好不变
            pc.apply_agent(
                agent_name="test_agent",
                agent_tools={},
                agent_permission={"read": "ask", "bash": "deny", "*": "allow"},
            )
            assert pc.get_tool_policy("read") == "ask"
            assert pc.get_tool_policy("bash") == "deny"
            # user 偏好层不受 agent 激活影响
            assert pc.get_user_tool_policy("read") == "ask"
            assert pc.get_user_tool_policy("bash") == "deny"
            # 复制状态(copy_state_from 带新字段)
            pc2 = ToolPermissionController()
            pc2.copy_state_from(pc)
            assert pc2.get_tool_policy("read") == "ask"
            assert pc2.get_tool_policy("bash") == "deny"
            # 恢复用户偏好
            pc.restore_user()
            assert pc.get_tool_policy("read") == "ask"
            assert pc.get_tool_policy("bash") == "deny"
            pc.deleteLater()
            pc2.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_tool_policy_combo_visibility(self, qt_app):
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController
        from app.widgets.cards.settings.tool_control_card import ToolControlCardContent

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            pc = ToolPermissionController()
            card = ToolControlCardContent(controller=pc)
            card.show_content()
            # 默认全开 → 策略下拉懒创建：开启行无 combo 或隐藏（行为语义，
            # 不锁对象存在性）
            combo = card._policy_combos.get("read")
            assert combo is None or combo.isHidden()
            # 关闭 read → 下拉创建并显示
            pc.set_user_toggle("read", False)
            qt_app.processEvents()
            combo = card._policy_combos.get("read")
            assert combo is not None
            assert not combo.isHidden()
            # 重新开启 → 隐藏（懒创建对象保留，仅切可见性）
            pc.set_user_toggle("read", True)
            qt_app.processEvents()
            combo = card._policy_combos.get("read")
            assert combo is not None
            assert combo.isHidden()
            card.deleteLater()
            pc.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_behavior_combo_mixed_and_force(self, qt_app):
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController
        from app.widgets.cards.settings.tool_control_card import (
            MIXED_OPTION,
            ToolControlCardFrame,
        )

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            # 环境隔离：清空残留 per-tool 策略，保证初始状态一致（回退全局 deny）
            from app.utils.config import Settings

            s = Settings.get_instance()
            s.tool_permission_policy.value = {}
            s.save()
            pc = ToolPermissionController()
            frame = ToolControlCardFrame(controller=pc)
            # 初始:无 per-tool 策略 → 全部回退全局 deny → 一致 → 显示 deny
            assert frame._behavior_combo.currentData() == "deny"
            # 设置一个工具 ask → 不一致 → "未统一"占位
            pc.set_user_tool_policy("read", "ask")
            qt_app.processEvents()
            assert frame._behavior_combo.currentData() == MIXED_OPTION[0]
            # 强制统一:右上角选择 ask → 所有工具策略变 ask
            frame._on_behavior_changed(frame._behavior_combo.findData("ask"))
            qt_app.processEvents()
            assert pc.get_tool_policy("read") == "ask"
            assert pc.get_tool_policy("bash") == "ask"
            assert frame._behavior_combo.currentData() == "ask"
            frame.deleteLater()
            pc.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_engine_off_policy_resolution(self, qt_app):
        """engine/subagent_worker 共用 resolve_tool_off_policy:关闭分支查 per-tool 策略"""
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import (
            ToolPermissionController,
            resolve_tool_off_policy,
        )

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            pc = ToolPermissionController()
            # controller 存在:per-tool 优先,缺失回退全局 behavior
            pc.set_user_tool_policy("read", "ask")
            assert resolve_tool_off_policy("read", pc, {}, "deny") == "ask"
            assert resolve_tool_off_policy("bash", pc, {}, "deny") == "deny"
            # controller 不存在(API 模式):查 Settings policies 字典兜底
            assert resolve_tool_off_policy("read", None, {"read": "ask"}, "deny") == "ask"
            assert resolve_tool_off_policy("bash", None, {"read": "ask"}, "deny") == "deny"
            # 非法值回退全局 behavior
            assert resolve_tool_off_policy("read", None, {"read": "banana"}, "deny") == "deny"
            pc.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_policy_change_does_not_bypass_template_deny(self, qt_app):
        """MAJOR-1 锚点:改策略不污染 _user_modified → 不绕过 agent 模板 deny"""
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import (
            ToolPermissionController,
            resolve_tool_off_policy,
        )

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            pc = ToolPermissionController()
            # 激活 agent:bash 模板 deny(安全策略)
            pc.apply_agent(
                agent_name="t",
                agent_tools={},
                agent_permission={"bash": "deny", "*": "allow"},
            )
            # 用户只改 bash 关闭策略为 ask(开关仍 off)
            pc.set_user_tool_policy("bash", "ask")
            assert pc.get_tool_policy("bash") == "ask"
            # 未进入 _user_modified → 模板 deny 不被绕过
            assert not pc.is_user_modified("bash")
            # 关闭分支按 per-tool 返回 ask(而非被 is_user_modified 放行)
            assert resolve_tool_off_policy("bash", pc, {}, "deny") == "ask"
            # 对照:显式拨开关才进 _user_modified(T28 语义保留)
            pc.set_user_toggle("read", True)
            assert pc.is_user_modified("read")
            pc.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_set_policy_agent_mode_active_only(self, qt_app):
        """MINOR-2①:agent 激活时 set_user_tool_policy 只改 active,user 偏好不变"""
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            pc = ToolPermissionController()
            # 用户偏好:bash 关闭策略 = ask
            pc.set_user_tool_policy("bash", "ask")
            # 激活 agent(bash 模板 deny → active 侧策略 deny)
            pc.apply_agent(
                agent_name="t",
                agent_tools={},
                agent_permission={"bash": "deny", "*": "allow"},
            )
            assert pc.get_tool_policy("bash") == "deny"  # active 反映模板
            # agent 模式下改 bash 策略为 ask → 只改 active
            pc.set_user_tool_policy("bash", "ask")
            assert pc.get_tool_policy("bash") == "ask"  # active 生效
            assert pc.get_user_tool_policy("bash") == "ask"  # user 偏好仍是用户原值
            # 恢复用户偏好 → 回到用户设置
            pc.restore_user()
            assert pc.get_tool_policy("bash") == "ask"
            pc.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_settings_external_policy_change(self, qt_app):
        """MINOR-2②:Settings 外部变更(ConfigSync 场景)自动刷新 + 回环防护"""
        from app.tools.registry import ToolRegistry
        from app.utils.config import Settings
        from app.core.tool_permission_controller import ToolPermissionController

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            s = Settings.get_instance()
            pc = ToolPermissionController()
            # 模拟外部变更(ConfigSync 下载新配置):写 Settings 后由
            # ConfigSyncService.settingsRestored 驱动刷新。控制器不再监听
            # Settings.valueChanged,避免兄弟 tab 本地编辑互相广播刷新。
            from app.core.config_sync import ConfigSyncService

            s.tool_permission_policy.value = {"read": "ask", "stale_tool": "ask"}
            ConfigSyncService.get_instance().settingsRestored.emit()
            qt_app.processEvents()
            # 已存在 controller 自动刷新(双相等性检查通过后应用)
            assert pc.get_user_tool_policy("read") == "ask"
            # 新 controller 从 Settings 读同一配置
            pc2 = ToolPermissionController()
            assert pc2.get_user_tool_policy("read") == "ask"
            # 残留工具(stale_tool 未注册)被清理,不进全量补全结果
            assert "stale_tool" not in pc2.get_user_tool_policies()
            # 自写回环不重复应用:set_user_tool_policy 后状态不被外部监听清掉
            pc2.set_user_tool_policy("read", "deny")
            assert pc2.get_user_tool_policy("read") == "deny"
            pc.deleteLater()
            pc2.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_subagent_worker_ui_permission_policy(self, qt_app):
        """MINOR-2③:subagent_worker._check_ui_tool_permission 关闭分支查 per-tool 策略"""
        from unittest.mock import MagicMock
        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController
        from app.core.workers.subagent_worker import SubAgentExecutor

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            pc = ToolPermissionController()
            pc.set_user_toggle("read", False)  # 关闭 read
            pc.set_user_tool_policy("read", "ask")
            backend = MagicMock()
            backend.tool_permission_controller = pc
            executor = MagicMock()
            executor._backend = backend
            # 绕过 __init__ 构造最小 worker(仅测 _check_ui_tool_permission)
            worker = SubAgentExecutor.__new__(SubAgentExecutor)
            worker.tool_executor = executor
            worker.agent_manager = None
            worker.agent_name = ""
            # 关闭 + per-tool ask → 返回 ask
            assert worker._check_ui_tool_permission("read", {}) == "ask"
            # 关闭 + per-tool deny → 返回 deny
            pc.set_user_tool_policy("read", "deny")
            assert worker._check_ui_tool_permission("read", {}) == "deny"
            # 缺失 per-tool → 回退全局 behavior
            pc.set_user_toggle("bash", False)
            assert worker._check_ui_tool_permission("bash", {}) == "deny"
            pc.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_restore_user_skips_agent_template_check(self, qt_app):
        """方案 B：restore_user()(取消 agent 激活)后，执行层不再用
        _current_agent(团队角色/UI 切换角色)的 permission 模板拦截，
        权限完全由用户开关(user toggles)控制。

        团队场景根因：apply_agent 注入后模板 deny 生效；用户点「↺ 恢复」
        只复位了 controller 开关状态，但 engine 模板检查仍用团队角色
        permission → 恢复前后工具权限行为一致，用户感觉「权限没生效」。
        """
        from unittest.mock import MagicMock

        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController
        from app.core.engines.ui.engine import UIEngine

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            pc = ToolPermissionController()
            # 团队角色激活：模板 deny write（用户偏好未关闭 write）
            pc.apply_agent(
                agent_name="build",
                agent_tools={},
                agent_permission={"write": "deny", "*": "allow"},
            )
            # 构造最小 engine（绕过 __init__，仅测 _check_tool_permission）
            engine = UIEngine.__new__(UIEngine)
            engine._current_agent = "build"
            backend = MagicMock()
            backend.tool_permission_controller = pc
            engine._backend = backend
            agent_manager = MagicMock()
            agent = MagicMock()
            agent.permission = {"write": "deny", "*": "allow"}
            agent.tools = {}
            agent_manager.get_agent.return_value = agent
            engine._get_agent_manager = lambda: agent_manager

            # 1. agent 激活期间：模板 deny → 拦截
            assert pc.is_agent_active()
            assert engine._check_tool_permission("write", {}) == "deny"
            # 2. 用户点「↺ 恢复」→ 取消激活
            pc.restore_user()
            assert not pc.is_agent_active()
            # 3. 恢复后：跳过模板检查 → 用户开关(write 开)放行
            #    （修复前此处仍返回 deny → 权限控制不生效）
            assert engine._check_tool_permission("write", {}) == "allow"
            # 4. 用户显式关闭 write → 恢复后仍受用户开关控制
            pc.set_user_toggle("write", False)
            assert engine._check_tool_permission("write", {}) == "deny"
            pc.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)

    def test_agent_active_template_still_blocks(self, qt_app):
        """防回归：agent 命令激活期间模板 deny 仍然拦截（方案 B 只放开
        非激活态，不破坏 agent 权限注入语义）。"""
        from unittest.mock import MagicMock

        from app.tools.registry import ToolRegistry
        from app.core.tool_permission_controller import ToolPermissionController
        from app.core.engines.ui.engine import UIEngine

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)

        snap = self._snapshot_settings()
        try:
            pc = ToolPermissionController()
            pc.apply_agent(
                agent_name="build",
                agent_tools={},
                agent_permission={"bash": "deny", "*": "allow"},
            )
            engine = UIEngine.__new__(UIEngine)
            engine._current_agent = "build"
            backend = MagicMock()
            backend.tool_permission_controller = pc
            engine._backend = backend
            agent_manager = MagicMock()
            agent = MagicMock()
            agent.permission = {"bash": "deny", "*": "allow"}
            agent.tools = {}
            agent_manager.get_agent.return_value = agent
            engine._get_agent_manager = lambda: agent_manager

            # 激活中：bash 模板 deny → 拦截
            assert pc.is_agent_active()
            assert engine._check_tool_permission("bash", {}) == "deny"
            # 模板 allow 的工具放行
            assert engine._check_tool_permission("read", {}) == "allow"
            pc.deleteLater()
            qt_app.processEvents()
        finally:
            self._restore_settings(snap)


class TestWebToolsEnvKey:
    """T8:web_tools._api_key 仅读环境变量(主程序不再注入 token)"""

    @staticmethod
    def _load_web_tools():
        """按插件加载器同款方式动态加载 web_tools 模块"""
        import importlib.util

        path = Path(__file__).parent.parent / "plugins" / "system" / "tools" / "web_tools.py"
        spec = importlib.util.spec_from_file_location("_test_web_tools", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _schema_defaults():
        """E1：默认 key 由 plugin.json config_schema 声明（迁移后单一来源）"""
        import json

        manifest = json.loads(
            (Path(__file__).parent.parent / "plugins" / "system" / ".drifox-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        return {f["key"]: f["default"] for f in manifest["config_schema"]["fields"]}

    def test_api_key_reads_env_only(self, monkeypatch):
        mod = self._load_web_tools()
        defaults = self._schema_defaults()
        # 插件内置默认 key 非空(用户配置值由 schema 声明)
        assert defaults["tavily_api_key"]
        assert defaults["tinyfish_api_key"]
        # E1 契约：_api_key 调用前需注册 schema（模块级常量已迁出）
        from app.plugins.contracts.plugin_config import parse_config_schema
        from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

        reg = PluginConfigRegistry.get_instance()
        reg.register(
            parse_config_schema(
                "system",
                {
                    "title": "T",
                    "fields": [
                        {
                            "key": "tavily_api_key",
                            "label": "T",
                            "type": "password",
                            "default": defaults["tavily_api_key"],
                            "env": "TAVILY_API_KEY",
                        },
                        {
                            "key": "tinyfish_api_key",
                            "label": "T",
                            "type": "password",
                            "default": defaults["tinyfish_api_key"],
                            "env": "TINYFISH_API_KEY",
                        },
                    ],
                },
            )
        )
        try:
            monkeypatch.setenv("TAVILY_API_KEY", "env-test-key")
            # 无 tool_ctx / 无 env.api_keys → 仍能读到(os.environ 优先)
            assert mod._api_key({}, "TAVILY_API_KEY") == "env-test-key"
            assert mod._api_key(None, "TAVILY_API_KEY") == "env-test-key"
            # 未设置环境变量 → 回退 schema default(非空)
            monkeypatch.delenv("TAVILY_API_KEY")
            assert mod._api_key({}, "TAVILY_API_KEY") == defaults["tavily_api_key"]
            # TINYFISH 同样:环境变量优先
            monkeypatch.setenv("TINYFISH_API_KEY", "tiny-test-key")
            assert mod._api_key({}, "TINYFISH_API_KEY") == "tiny-test-key"
            # TINYFISH 未设置 → 回退 schema default
            monkeypatch.delenv("TINYFISH_API_KEY")
            assert mod._api_key({}, "TINYFISH_API_KEY") == defaults["tinyfish_api_key"]
        finally:
            reg.unregister_plugin("system")


class TestSelfContained:
    """自包含验证：纯逻辑工具 impl 不依赖主程序工具模块"""

    def test_impl_source_self_contained(self):
        """file/web/automation 插件源码不得 import 主程序工具模块"""
        import glob

        forbidden = [
            "from app.tools.file_tools import",
            "from app.tools.web_tools import",
            "from app.tools.automation import",
            'tool_ctx["builtin_tools"]',
            'tool_ctx.get("builtin_tools")',
        ]
        # 只检查 import 行 + builtin_tools 访问（docstring 说明文字不受限）
        for py in glob.glob("plugins/system/tools/*.py"):
            src = open(py, encoding="utf-8").read()
            import_lines = [l for l in src.splitlines() if l.strip().startswith(("import ", "from "))]
            for kw in forbidden:
                assert not any(kw in l for l in import_lines), f"{py} import 依赖主程序: {kw}"
                assert kw not in src or kw.startswith("from "), f"{py} 直接访问 builtin_tools"

    def test_file_impl_executes_without_bt(self):
        """自包含 impl 执行：只注入 workdir 也能跑（不依赖 BuiltinTools 实例）"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        import tempfile
        from pathlib import Path

        wd = Path(tempfile.mkdtemp(prefix="selfcontained_test_"))
        # 构造最小 tool_ctx（无 services、无 builtin_tools）
        ctx = {"workdir": str(wd)}
        result = reg.get("write").impl(tool_ctx=ctx, path="x.txt", content="独立执行")
        assert result.success
        assert (wd / "x.txt").read_text(encoding="utf-8") == "独立执行"
        result = reg.get("read").impl(tool_ctx=ctx, path="x.txt")
        assert result.success and "独立执行" in str(result.content)
        # write 必须带 diff（行内 diff 框渲染依赖）
        result = reg.get("write").impl(tool_ctx=ctx, path="x.txt", content="覆盖内容")
        assert result.success and result.diff, "write 应生成 unified diff"
        assert "-独立执行" in result.diff and "+覆盖内容" in result.diff

    def test_icon_dir_injected(self):
        """插件自带图标目录注入 + data URI 渲染"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import _get_tool_icon_html, _get_tool_icon_name

        ToolRegistry.reset_instance()
        # 显式只加载 system_root，隔离用户插件覆盖（hashline-edit 无 read.svg 图标）
        load_plugin_tools(registry=ToolRegistry.get_instance(), plugin_roots=[Path(__file__).parent.parent / "plugins"])
        reg = ToolRegistry.get_instance()
        assert reg.get_icon_dir("read")
        assert reg.get_icon_dir_light("read")
        assert (Path(reg.get_icon_dir("read")) / "read.svg").exists()
        assert (Path(reg.get_icon_dir_light("read")) / "read.svg").exists()
        html = _get_tool_icon_html(_get_tool_icon_name("read"), tool_name="read")
        assert html.startswith('<img src="data:image/svg+xml;base64,')

    def test_icon_theme_aware(self):
        """深浅主题加载不同图标（浅色 → icons_light/，深色 → icons/）"""
        import base64
        from unittest.mock import patch

        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.utils.theme_manager import theme_manager
        from app.widgets.render_helpers import _get_tool_icon_html, _get_tool_icon_name

        ToolRegistry.reset_instance()
        # 显式只加载 system_root，隔离用户插件覆盖
        load_plugin_tools(registry=ToolRegistry.get_instance(), plugin_roots=[Path(__file__).parent.parent / "plugins"])
        icon_name = _get_tool_icon_name("read")

        def svg_of(html):
            b64 = html.split("base64,")[1].split('"')[0]
            return base64.b64decode(b64).decode("utf-8")

        with patch.object(theme_manager, "is_light_theme", return_value=False):
            dark_svg = svg_of(_get_tool_icon_html(icon_name, tool_name="read"))
        with patch.object(theme_manager, "is_light_theme", return_value=True):
            light_svg = svg_of(_get_tool_icon_html(icon_name, tool_name="read"))
        assert dark_svg != light_svg  # 深浅图标内容不同

    def test_render_closure(self):
        """工具完成框渲染闭包：插件注册 render → 渲染层优先调用；无注册回退默认"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import _render_text_output

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        # codegraph 社区插件注册了 render 闭包（未安装 codegraph-tools 时跳过该子断言）
        if reg.get("codegraph_explore") is not None:
            assert reg.get_render("codegraph_explore") is not None
            html = _render_text_output("### 标题\n📄 文件.py", "codegraph_explore", {})
            assert "58a6ff" in html  # 插件闭包标题蓝
        # 未注册 render 的工具走默认渲染
        assert reg.get_render("read") is None
        html2 = _render_text_output("普通输出", "read", {"path": "x"})
        assert html2

    def test_dag_echarts_render_closure(self):
        """subagent_dag 的 echarts 渲染走插件 render 闭包"""
        import json

        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import render_tool_block

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        assert reg.get_render("subagent_dag") is not None
        echarts_json = json.dumps({"type": "graph", "data": [], "links": []})
        html = render_tool_block(
            "subagent_dag", {"nodes": [{"id": "a"}]}, result="DAG 完成", success=True, echarts=echarts_json
        )
        assert "echarts-container" in html

    def test_render_mode_and_closures(self):
        """render_mode（inline/none）+ 渲染闭包（edit diff/bash/question）"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import render_tool_block

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        # render_mode：read=inline（紧凑无 body）
        assert reg.get_render_mode("read") == "inline"
        html = render_tool_block("read", {"path": "x"}, result="内容", success=True)
        assert "tool-expanded-content" not in html
        # render_mode：expand 由插件声明（截图类工具迁移后按需注册；此处验证渲染分支）
        reg.register(
            "_test_expand",
            {
                "type": "function",
                "function": {"name": "_test_expand", "parameters": {"type": "object", "properties": {}}},
                "required": [],
            },
            impl=lambda tool_ctx, **kw: None,
            danger="safe",
            render_mode="expand",
            source="plugin:test",
        )
        try:
            assert reg.get_render_mode("_test_expand") == "expand"
            html = render_tool_block("_test_expand", {}, result="已截图", success=True)
            assert "cm-collapsible" not in html
            assert "tool-block--no-collapse" in html
            assert "tool-expanded-content" in html  # body 直接展开
            # expand 模式：简洁模式偏好下也不折叠
            html = render_tool_block("_test_expand", {}, result="已截图", success=True, collapsed=True)
            assert "cm-collapsible" not in html
            assert "tool-block--no-collapse" in html
        finally:
            reg.unregister("_test_expand")
        # preview 闭包：插件注册的自然语言预览（不写死主程序）
        p = reg.get_preview("read")
        assert p is not None
        assert p({"path": "x.py", "startline": 5}) == '读取 "x.py" (从第 5 行)'
        p = reg.get_preview("websearch")
        assert p is not None
        assert p({"query": "python"}) == '搜索 "python"'
        p = reg.get_preview("question")
        assert p is not None
        assert p({"questions": [{"question": "继续？"}]}) == "继续？"
        # inline 渲染走 preview 闭包文案（去重：自然语言预览去掉与中文名重复的前缀）
        html = render_tool_block("todoread", {"limit": 5}, result="todo", success=True)
        assert "前 5 行" in html
        # 文本输出无白名单：任意工具成功结果均渲染（闭包路由或通用 pre）
        html = render_tool_block("websearch", {"query": "x"}, result="搜索结果", success=True)
        assert "<pre" in html
        # 渲染闭包已注册
        assert reg.get_render("edit") is not None
        assert reg.get_render("bash") is not None
        assert reg.get_render("question") is not None
        assert reg.get_render("multi_edit") is not None

    def test_team_tools_danger(self):
        """团队工具/标记文件为安全操作（非危险）"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        assert reg.get_danger("team_send_message") == "safe"
        assert reg.get_danger("stage_files") == "safe"
        assert reg.is_team_only("team_send_message")

    def test_services_injected(self):
        """平台服务工具：services 缺失优雅降级；todo 自包含（不依赖 services）"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        ctx = {"workdir": None, "session_id": "s", "services": {}}
        # todo 自包含：无 services 也正常工作（模块级状态）
        result = reg.get("todowrite").impl(tool_ctx=ctx, todos=[{"content": "t1", "status": "pending"}])
        assert result.success
        assert result.todos and result.todos[0]["content"] == "t1"
        result = reg.get("todoread").impl(tool_ctx=ctx)
        assert result.success
        # 团队工具：无窗口上下文 → 优雅失败（不崩溃）
        result = reg.get("team_list_members").impl(tool_ctx=ctx)
        assert not result.success


# ══════════════════════════════════════════════════════════
# 补充（quality-engineer 盘点后新增）：渲染模式 / diff 降级 / 元数据 / 加载容错
# ══════════════════════════════════════════════════════════


class TestRenderModeNone:
    """render_mode="none"：不渲染工具完成框"""

    def test_none_mode_returns_empty_html(self):
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        reg.register(
            "none_tool",
            {"type": "function", "function": {"name": "none_tool"}},
            impl=lambda **kw: "x",
            danger="safe",
            source="plugin:t",
            render_mode="none",
        )
        from app.widgets.render_helpers import render_tool_block

        html = render_tool_block("none_tool", {}, result="内容", success=True)
        assert html == "", f"render_mode=none 应不渲染完成框，实际: {html[:80]}"

    def test_unregistered_tool_default_render(self):
        """未注册工具回退默认折叠卡（render_mode 默认空）"""
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import render_tool_block

        ToolRegistry.reset_instance()
        html = render_tool_block("unknown_tool", {}, result="内容", success=True)
        assert html != ""
        assert "cm-collapsible" in html


class TestDiffRenderDegradation:
    """diff 渲染插件化降级（e293b056 删除主程序 fallback 后的行为锁定）"""

    def test_diff_without_render_closure_no_fallback(self):
        """带 diff 但工具无 render 闭包 → 无 diff 渲染（主程序无兜底）"""
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import render_tool_block

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        reg.register(
            "diff_tool",
            {"type": "function", "function": {"name": "diff_tool"}},
            impl=lambda **kw: "x",
            danger="safe",
            source="plugin:t",
        )
        diff_text = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-old\n+new"
        html = render_tool_block("diff_tool", {}, result="已修改", success=True, diff=diff_text)
        assert "tool-diff-inline" not in html, "无 render 闭包的工具不应输出插件 diff 渲染"
        assert "tool-diff-inline__header" not in html, "主程序 fallback 已删除，不应再输出 diff header"

    def test_render_closure_exception_falls_back_to_text(self):
        """render 闭包抛异常 → 回退通用文本渲染（不崩溃）"""
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import render_tool_block

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()

        def _boom_render(result, tool_name, tool_args, success):
            raise RuntimeError("render boom")

        reg.register(
            "boom_tool",
            {"type": "function", "function": {"name": "boom_tool"}},
            impl=lambda **kw: "x",
            danger="safe",
            source="plugin:t",
            render=_boom_render,
        )
        html = render_tool_block("boom_tool", {}, result="结果文本", success=True, diff="+a\n-b")
        assert "tool-diff-inline" not in html, "闭包异常应放弃 diff 渲染"
        assert "结果文本" in html, "结果文本应仍渲染（回退通用渲染）"


class TestRegistryMetadata:
    """registry 元数据消费点（permission_resolve_args / summarize / protect / interactive / ui_managed）"""

    @staticmethod
    def _reg_with_meta():
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()

        def _mk(name, metadata):
            reg.register(
                name,
                {"type": "function", "function": {"name": name}},
                impl=lambda **kw: "x",
                danger="safe",
                source="plugin:t",
                metadata=metadata,
            )

        _mk("mt_protect", {"protect": True})
        _mk("mt_interactive", {"interactive": True})
        _mk("mt_ui", {"ui_managed": True})
        _mk("mt_task", {"permission_task": True})
        _mk("mt_arg", {"permission_arg": "path"})
        _mk("mt_plain", {})
        return reg

    def test_permission_resolve_args_task(self):
        reg = self._reg_with_meta()
        assert reg.permission_resolve_args("mt_task", {"tasks": [{"agent": "build"}]}) == ("task", "build")
        assert reg.permission_resolve_args("mt_task", {"tasks": []}) == ("task", "")

    def test_permission_resolve_args_plain(self):
        reg = self._reg_with_meta()
        assert reg.permission_resolve_args("mt_arg", {"path": "/tmp/x"}) == ("plain", "/tmp/x")
        assert reg.permission_resolve_args("mt_plain", {"path": "/tmp/x"}) == ("plain", "")

    def test_make_summarize_from_preview(self):
        from app.tools.registry import make_summarize_from_preview

        fn = make_summarize_from_preview(lambda args: f"预览 {args.get('n', '')}")
        assert fn("tool", {"n": 5}, "content") == "[tool] 预览 5 (7 chars)"
        assert fn("tool", None, "") == "[tool] 预览  (0 chars)"

    def test_protected_interactive_ui_managed(self):
        reg = self._reg_with_meta()
        assert reg.is_protected("mt_protect") is True
        assert reg.is_protected("mt_plain") is False
        assert reg.is_interactive("mt_interactive") is True
        assert reg.is_interactive("mt_protect") is False
        assert reg.is_ui_managed("mt_ui") is True
        assert reg.is_ui_managed("mt_plain") is False

    def test_subagent_task_keep_in_content(self):
        """keep_in_content=True 显式声明的工具常驻正文（keep_in_content_tools 纯参数派生）"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        kept = reg.keep_in_content_tools()
        assert "subagent_para" in kept
        assert "subagent_dag" in kept
        assert "write" in kept, "文件写入工具应常驻正文"
        assert "question" in kept, "提问工具应常驻正文"
        # 纯 metadata 语义键（interactive/subagent_task）不再隐式驱动留正文
        assert "skill" not in kept

    def test_keep_in_content_param_only(self):
        """keep_in_content 纯参数派生：group/语义 metadata 不再隐式生效"""
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        reg = ToolRegistry.get_instance()
        reg.register("_t_keep", {"type": "object"}, danger="safe", keep_in_content=True, source="plugin:test")
        reg.register(
            "_t_no_keep", {"type": "object"},
            danger="safe", group="文件写入", source="plugin:test",
            metadata={"interactive": True, "subagent_task": True},
        )
        kept = reg.keep_in_content_tools()
        assert "_t_keep" in kept
        assert "_t_no_keep" not in kept, "语义借用不再隐式触发留正文"
        ToolRegistry.reset_instance()


class TestPluginLoadFaultTolerance:
    """插件 register 抛异常 → load_plugin_tools 跳过该插件继续加载其他插件"""

    def test_broken_plugin_skipped_others_loaded(self, tmp_path):
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import DANGER_SAFE, ToolRegistry

        ToolRegistry.reset_instance()
        # 坏插件：register 抛异常
        bad_dir = tmp_path / "bad-plugin" / "tools"
        bad_dir.mkdir(parents=True)
        (bad_dir / "bad_tool.py").write_text(
            "def register(registry):\n    raise RuntimeError('boom')\n", encoding="utf-8"
        )
        # 好插件：正常注册
        good_dir = tmp_path / "good-plugin" / "tools"
        good_dir.mkdir(parents=True)
        (good_dir / "good_tool.py").write_text(
            "def register(registry):\n"
            "    registry.register('good_tool', {'type': 'function', 'function': {'name': 'good_tool'}},\n"
            "                impl=lambda **kw: 'ok', danger='safe')\n",
            encoding="utf-8",
        )
        # 两插件名加入 enabled_plugins 避免 P0-1 过滤
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = list(cfg.enabled_plugins.value or [])
        cfg.enabled_plugins.value = saved + ["bad-plugin", "good-plugin"]
        try:
            reg = ToolRegistry.get_instance()
            loaded = load_plugin_tools(registry=reg, plugin_roots=[tmp_path])
            assert "good_tool" in reg.names(), f"好插件应加载，实际: {reg.names()}"
            assert "bad_tool" not in reg.names()
            assert "good-plugin" in loaded
            assert "bad-plugin" not in loaded, "register 失败的插件不应计入 loaded"
        finally:
            cfg.enabled_plugins.value = saved

    def test_partial_register_rolls_back(self, tmp_path):
        """插件 register 注册 2 个后第 3 个抛异常 → 前 2 个回滚（无半套工具残留）"""
        from app.plugins.loaders.plugin_tool_loader import PluginToolWatcher, load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        plugin_dir = tmp_path / "partial-plugin" / "tools"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "partial_tool.py").write_text(
            "def register(registry):\n"
            "    registry.register('t_a', {'type': 'function', 'function': {'name': 't_a'}},\n"
            "                impl=lambda **kw: 'a', danger='safe')\n"
            "    registry.register('t_b', {'type': 'function', 'function': {'name': 't_b'}},\n"
            "                impl=lambda **kw: 'b', danger='safe')\n"
            "    raise RuntimeError('third step boom')\n",
            encoding="utf-8",
        )
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = list(cfg.enabled_plugins.value or [])
        cfg.enabled_plugins.value = saved + ["partial-plugin"]
        try:
            reg = ToolRegistry.get_instance()
            loaded = load_plugin_tools(registry=reg, plugin_roots=[tmp_path])
            # 前 2 个已注册工具必须回滚（失败插件不留半套工具）
            assert reg.get("t_a") is None, "t_a 应被回滚注销"
            assert reg.get("t_b") is None, "t_b 应被回滚注销"
            assert "partial-plugin" not in loaded
            # watcher scan_now 重扫后仍无残留（回滚彻底，无幽灵工具）
            watcher = PluginToolWatcher(registry=reg, roots=[tmp_path])
            watcher.scan_now()
            assert reg.get("t_a") is None
            assert reg.get("t_b") is None
        finally:
            cfg.enabled_plugins.value = saved


class TestToolCardRebuildAndListener:
    """P1-1 回归 + P2-3 弱引用：show_content 吸收排队不丢变更 / listener 不泄漏"""

    def test_show_content_absorbs_queued_change_rebuilds(self, qt_app):
        """P1-1：隐藏期间 registry 变更排队 → 立即显示必须重建（新工具不丢失）"""
        from app.tools.registry import ToolRegistry
        from app.widgets.cards.settings.tool_control_card import ToolControlCardContent

        reg = ToolRegistry.get_instance()
        card = ToolControlCardContent()
        # 首次构建（吸收创建时的立即回调排队）
        card.show_content()
        assert card._built
        assert not card._rebuild_pending
        assert "p11_probe_tool" not in card._toggle_widgets

        calls = []
        _orig_rebuild = ToolControlCardContent._rebuild

        def _counting(self):
            calls.append(1)
            return _orig_rebuild(self)

        ToolControlCardContent._rebuild = _counting
        try:
            # 模拟隐藏期间热重载：注册新工具 → registry 变更 → 排队
            reg.register(
                "p11_probe_tool",
                {"type": "function", "function": {"name": "p11_probe_tool"}},
                impl=lambda **kw: "ok",
                danger="safe",
                icon="read",
                cn_name="探针工具",
                group="测试组",
                description="P1-1 回归探针",
                source="plugin:test",
            )
            assert card._rebuild_pending, "registry 变更应排队"
            assert card._rebuild_timer.isActive(), "排队 timer 应激活"
            # 立即打开卡片（隐藏 → 显示）：吸收排队并重建
            card.show_content()
            assert len(calls) == 1, f"吸收排队后应重建 1 次, 实际 {len(calls)}"
            assert "p11_probe_tool" in card._toggle_widgets, "新工具应出现在卡片（吸收的变更不丢失）"
            assert not card._rebuild_pending
            assert not card._rebuild_timer.isActive()
        finally:
            ToolControlCardContent._rebuild = _orig_rebuild
            card.deleteLater()
            qt_app.processEvents()

    def test_registry_listener_weakref_released(self):
        """P2-3：bound method 监听者对象销毁后 registry 不再回调（弱引用 + 死引用清理）"""
        import gc
        import weakref

        from app.tools.registry import ToolRegistry

        reg = ToolRegistry.get_instance()
        assert len(reg._listeners) == 0  # fresh_registry 重置后无残留

        class Probe:
            def __init__(self):
                self.calls = []

            def on_change(self, version):
                self.calls.append(version)

        p = Probe()
        ref = weakref.ref(p)
        reg.on_change(p.on_change)
        assert len(p.calls) == 1  # 注册即回调一次
        reg._notify_change()
        assert len(p.calls) == 2  # 对象存活时正常回调

        del p
        gc.collect()
        assert ref() is None, "listener 对象应被回收（弱引用不保活）"
        # 通知不再触发已销毁对象（无异常），并顺带清理死引用
        before = len(reg._listeners)
        reg._notify_change()
        after = len(reg._listeners)
        assert before == 1 and after == 0, f"死引用应被清理: {before} -> {after}"
