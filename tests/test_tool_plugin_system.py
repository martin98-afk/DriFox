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
from app.tools.plugin_tool_loader import load_plugin_tools, unload_plugin_tools


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
            "no_danger", {"type": "function", "function": {"name": "no_danger"}},
            impl=lambda **kw: "x", source="plugin:x",
        )
        assert not ok

    def test_plugin_cannot_forge_builtin(self):
        reg = ToolRegistry.get_instance()
        ok = reg.register(
            "forge", {"type": "function", "function": {"name": "forge"}},
            danger="safe", source="builtin",
        )
        assert not ok  # 非 trusted 流程拒绝

    def test_trusted_builtin_allowed(self):
        reg = ToolRegistry.get_instance()
        ok = reg.register(
            "trusted_b", {"type": "function", "function": {"name": "trusted_b"}},
            danger="safe", source="builtin", trusted=True,
        )
        assert ok

    def test_unregister_and_version(self):
        reg = ToolRegistry.get_instance()
        reg.register("a", {"type": "function", "function": {"name": "a"}},
                     danger="safe", source="plugin:x")
        v1 = reg.version()
        assert reg.unregister("a")
        assert reg.version() == v1 + 1
        assert not reg.unregister("a")  # 幂等

    def test_group_map_danger_first(self):
        reg = ToolRegistry.get_instance()
        reg.register("safe1", {"type": "function", "function": {"name": "safe1"}},
                     danger="safe", group="G", source="plugin:x")
        reg.register("danger1", {"type": "function", "function": {"name": "danger1"}},
                     danger="dangerous", group="G", source="plugin:x")
        groups = reg.group_map()
        assert [r.name for r in groups["G"]] == ["danger1", "safe1"]

    def test_display_group_fallback(self):
        reg = ToolRegistry.get_instance()
        reg.register("s1", {"type": "function", "function": {"name": "s1"}},
                     danger="safe", source="plugin:x")
        reg.register("d1", {"type": "function", "function": {"name": "d1"}},
                     danger="dangerous", source="plugin:x")
        assert reg.get_group("s1") == "安全操作"
        assert reg.get_group("d1") == "其他"

    def test_on_change_listener(self):
        reg = ToolRegistry.get_instance()
        versions = []
        reg.on_change(lambda v: versions.append(v))  # 立即回调一次
        reg.register("b", {"type": "function", "function": {"name": "b"}},
                     danger="safe", source="plugin:x")
        assert len(versions) == 2  # 初始 + 注册
        assert versions[1] == 1


class TestSystemPluginTools:
    """系统插件工具（plugins/system/tools/）"""

    def test_all_34_tools_registered(self):
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)
        names = set(reg.names())
        expected = {
            "read", "write", "edit", "multi_edit", "grep", "list", "glob",
            "scan_repo", "stage_files", "websearch", "webfetch", "bash",
            "bg_start", "bg_stop", "bg_logs", "bg_list", "todowrite",
            "todoread", "mouse", "keyboard", "screenshot", "get_diagnostics",
            "lsp", "codegraph_explore", "subagent_para", "subagent_status",
            "subagent_dag", "team_send_message", "team_list_members",
            "question", "skill", "list_skills", "mcp_list_servers", "upload_file",
        }
        assert expected <= names, f"缺失: {expected - names}"
        assert len(names) == 34

    def test_danger_classification(self):
        reg = ToolRegistry.get_instance()
        load_plugin_tools(registry=reg)
        assert reg.get_danger("write") == DANGER_DANGEROUS
        assert reg.get_danger("bash") == DANGER_DANGEROUS
        assert reg.get_danger("mouse") == DANGER_DANGEROUS
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

    def _make_temp_plugin(self, tmpdir: Path, tool_name: str, content: str) -> Path:
        tools_dir = tmpdir / "hotplug-test" / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        py = tools_dir / f"{tool_name}.py"
        py.write_text(content, encoding="utf-8")
        return tmpdir

    def test_plugin_register_and_unload(self, tmp_path):
        reg = ToolRegistry.get_instance()
        root = self._make_temp_plugin(
            tmp_path, "hello_plug",
            '''
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
''',
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
            tmp_path, "read",
            '''
def register(registry):
    registry.register(
        "read",
        {"type": "function", "function": {"name": "read"}},
        impl=lambda **kw: "user", danger="safe",
    )
''',
        )
        load_plugin_tools(registry=reg, plugin_roots=[user_root])
        # 系统插件后加载，不应覆盖用户同名（此处无系统，验证 proxy 拒绝重复源）
        assert reg.get("read").source == "plugin:hotplug-test"


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
        assert len(toggles) == 34
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


class TestSelfContained:
    """自包含验证：纯逻辑工具 impl 不依赖主程序工具模块"""

    def test_impl_source_self_contained(self):
        """file/web/automation 插件源码不得 import 主程序工具模块"""
        import glob

        forbidden = [
            "from app.tools.file_tools import",
            "from app.tools.web_tools import",
            "from app.tools.automation import",
            "tool_ctx[\"builtin_tools\"]",
            "tool_ctx.get(\"builtin_tools\")",
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
        from app.tools.plugin_tool_loader import load_plugin_tools
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
        from app.tools.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import _get_tool_icon_html, _get_tool_icon_name

        ToolRegistry.reset_instance()
        load_plugin_tools()
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

        from app.tools.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.utils.theme_manager import theme_manager
        from app.widgets.render_helpers import _get_tool_icon_html, _get_tool_icon_name

        ToolRegistry.reset_instance()
        load_plugin_tools()
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
        from app.tools.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import _render_text_output

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        # codegraph 社区插件注册了 render 闭包
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

        from app.tools.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import render_tool_block

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        assert reg.get_render("subagent_dag") is not None
        echarts_json = json.dumps({"type": "graph", "data": [], "links": []})
        html = render_tool_block("subagent_dag", {"nodes": [{"id": "a"}]},
                                 result="DAG 完成", success=True, echarts=echarts_json)
        assert "echarts-container" in html

    def test_render_mode_and_closures(self):
        """render_mode（inline/none）+ 渲染闭包（edit diff/bash/question）"""
        from app.tools.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry
        from app.widgets.render_helpers import render_tool_block

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        # render_mode：read=inline（紧凑无 body）
        assert reg.get_render_mode("read") == "inline"
        html = render_tool_block("read", {"path": "x"}, result="内容", success=True)
        assert "tool-expanded-content" not in html
        # render_mode：screenshot=expand（禁用折叠框，body 始终展开、无 cm-collapsible）
        assert reg.get_render_mode("screenshot") == "expand"
        html = render_tool_block("screenshot", {"region": [0, 0, 100, 100]}, result="已截图", success=True)
        assert "cm-collapsible" not in html
        assert "tool-block--no-collapse" in html
        assert "tool-expanded-content" in html  # body 直接展开
        # expand 模式：简洁模式偏好下也不折叠
        html = render_tool_block("screenshot", {}, result="已截图", success=True, collapsed=True)
        assert "cm-collapsible" not in html
        assert "tool-block--no-collapse" in html
        # preview 闭包：插件注册的自然语言预览（不写死主程序）
        p = reg.get_preview("read")
        assert p is not None
        assert p({"path": "x.py", "startline": 5}) == '读取 "x.py" (从第 5 行)'
        p = reg.get_preview("mouse")
        assert p is not None
        assert p({"action": "click", "x": 10, "y": 20}) == "鼠标点击 (10, 20)"
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
        from app.tools.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        load_plugin_tools()
        reg = ToolRegistry.get_instance()
        assert reg.get_danger("team_send_message") == "safe"
        assert reg.get_danger("stage_files") == "safe"
        assert reg.is_team_only("team_send_message")

    def test_services_injected(self):
        """平台服务工具：services 缺失优雅降级；todo 自包含（不依赖 services）"""
        from app.tools.plugin_tool_loader import load_plugin_tools
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
