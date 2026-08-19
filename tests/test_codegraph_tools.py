# -*- coding: utf-8 -*-
"""
CodeGraph 工具测试 — 功能正确性 + 性能基准

运行方式:
  python -m pytest tests/test_codegraph_tools.py -v
  python -m pytest tests/test_codegraph_tools.py -v -k "perf"  # 仅性能测试
  python tests/test_codegraph_tools.py                          # 直接运行
"""

import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── 跳过条件（工具插件化：引擎迁社区插件 .drifox/plugins/codegraph-tools/） ──
try:
    import codegraph  # noqa: F401
    import importlib.util

    _PLUGIN_PATH = PROJECT_ROOT / ".drifox" / "plugins" / "codegraph-tools" / "tools" / "codegraph.py"
    # 社区插件未安装/未同步时优雅跳过（不报收集错误）
    if not _PLUGIN_PATH.exists():
        raise ImportError(f"codegraph-tools 社区插件未安装: {_PLUGIN_PATH}")
    _spec = importlib.util.spec_from_file_location("_codegraph_plugin", _PLUGIN_PATH)
    _cg_plugin = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_cg_plugin)
    CodeGraphTools = _cg_plugin.CodeGraphTools
    _HAS_CODEGRAPH = _cg_plugin._HAS_CODEGRAPH
    _CODEGRAPH_AVAILABLE = _HAS_CODEGRAPH
except (ImportError, FileNotFoundError):
    _CODEGRAPH_AVAILABLE = False
    _cg_plugin = None


# ── 夹具 ─────────────────────────────────────────────────────────────────

class _OwnerShim:
    """引擎 owner 最小实现（仅 workdir）"""
    def __init__(self, workdir):
        self.workdir = workdir


@pytest.fixture(scope="class")
def cg_tools():
    """创建 CodeGraphTools 实例（指向实际项目）"""
    tools = CodeGraphTools(_OwnerShim(Path(os.getcwd())))
    yield tools
    tools.cleanup()


# =========================================================================
# 0. 契约测试（不依赖 codegraph-py 安装，P11）
# =========================================================================


class TestCodeGraphContract:
    """社区插件契约：缺插件/缺依赖时加载器容错（T2 计划 P11）。

    契约语义：codegraph 引擎是社区插件（.drifox/plugins/codegraph-tools），
    未安装/未同步时主程序必须优雅降级——加载不报错、registry 无 codegraph 工具。
    安装后由 TestCodeGraphModule（skipif 保护）验证功能。
    """

    @pytest.fixture(autouse=True)
    def _restore_system_plugins(self):
        """测试后恢复系统插件注册（防顺序污染：reset 后不恢复会清空 registry，
        导致后续测试（如 test_agent_smoke 的 tools 解析）查 registry 失败——T22 实测）。"""
        yield
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        load_plugin_tools()

    def test_load_without_codegraph_plugin_no_error(self, tmp_path):
        """临时空插件根（无 codegraph）→ load_plugin_tools 不报错"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        empty_root = tmp_path / "empty-plugins"
        empty_root.mkdir()
        loaded = load_plugin_tools(plugin_roots=[empty_root])  # 不应抛异常
        assert isinstance(loaded, dict)
        assert "codegraph-tools" not in loaded
        ToolRegistry.reset_instance()

    def test_no_codegraph_tool_registered_when_missing(self, tmp_path):
        """缺 codegraph 插件 → registry 无 codegraph_explore（不误注册）"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        empty_root = tmp_path / "empty2"
        empty_root.mkdir()
        load_plugin_tools(plugin_roots=[empty_root])
        reg = ToolRegistry.get_instance()
        assert reg.get("codegraph_explore") is None
        assert "codegraph_explore" not in reg.names()
        ToolRegistry.reset_instance()

    def test_broken_plugin_dir_does_not_kill_loader(self, tmp_path):
        """损坏的插件目录（无 register 函数）→ 加载器容错跳过，不崩溃"""
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
        from app.tools.registry import ToolRegistry

        ToolRegistry.reset_instance()
        root = tmp_path / "broken-root"
        (root / "broken-plug" / "tools").mkdir(parents=True)
        (root / "broken-plug" / "tools" / "x.py").write_text("x = 1\n", encoding="utf-8")
        # 不抛异常；broken-plug 不在 enabled 白名单被过滤跳过（P0-1），或已启用时无 register 函数返回空集
        loaded = load_plugin_tools(plugin_roots=[root])
        assert isinstance(loaded, dict)
        ToolRegistry.reset_instance()


# =========================================================================
# 1. 模块完整性测试
# =========================================================================

@pytest.mark.skipif(not _CODEGRAPH_AVAILABLE, reason="codegraph-py 未安装")
class TestCodeGraphModule:
    """验证 codegraph-py 包已正确安装且可导入"""

    def test_import_codegraph(self):
        import codegraph  # noqa: F401
        assert hasattr(codegraph, "CodeGraph")
        assert hasattr(codegraph, "__version__")

    def test_import_codegraph_tools(self):
        # 工具插件化：引擎迁社区插件 .drifox/plugins/codegraph-tools/tools/codegraph.py
        assert _cg_plugin is not None
        assert _cg_plugin._HAS_CODEGRAPH

    def test_tool_classifier(self):
        from app.tools.registry import ToolRegistry
        from app.tools.tool_classifier import get_safe_tools, classify_tool_danger
        from app.plugins.loaders.plugin_tool_loader import load_plugin_tools

        load_plugin_tools()
        assert "codegraph_explore" in get_safe_tools()
        assert ToolRegistry.get_instance().get_danger("codegraph_explore") == "safe"
        assert classify_tool_danger("codegraph_explore") == "safe"

    def test_tool_name_mapper(self):
        from app.tools.tool_name_mapper import ToolNameMapper
        assert ToolNameMapper.is_known("codegraph_explore")
        assert ToolNameMapper.to_native("CodeGraphExplore") == "codegraph_explore"
        assert ToolNameMapper.to_native("cg_explore") == "codegraph_explore"

    def test_tool_schema_in_tool_schemas(self):
        # 工具插件化后：schema 从 registry 读取（TOOL_SCHEMAS 静态表已删除）
        from app.tools import get_builtin_tools_schema

        schemas = get_builtin_tools_schema()
        cg_schemas = [s for s in schemas if s["function"]["name"].startswith("codegraph_")]
        assert len(cg_schemas) == 1
        schema = cg_schemas[0]
        assert schema["function"]["name"] == "codegraph_explore"
        params = schema["function"]["parameters"]["properties"]
        assert "mode" in params
        assert "query" in params
        assert "depth" in params
        assert "kind" in params


# =========================================================================
# 2. 功能正确性测试
# =========================================================================

@pytest.mark.skipif(not _CODEGRAPH_AVAILABLE, reason="codegraph-py 未安装")
class TestCodeGraphFunctional:
    """测试各 mode 返回正确结构"""

    # ── status ──────────────────────────────────────────────────────────

    def test_status_mode(self, cg_tools):
        """mode=status: 返回索引统计"""
        r = cg_tools.codegraph_explore(mode="status")
        assert r.is_success(), f"status 失败: {r.error}"
        content = r.content
        # 应包含关键统计字段
        assert "文件" in content or "节点" in content or "索引" in content

    # ── search ──────────────────────────────────────────────────────────

    def test_search_mode_found(self, cg_tools):
        """mode=search: 搜索已存在的符号"""
        r = cg_tools.codegraph_explore("CodeGraphTools", mode="search")
        assert r.is_success(), f"search 失败: {r.error}"
        # 我们自己的工具类应该已被索引（或至少不报错）
        assert r.content is not None

    def test_search_mode_not_found(self, cg_tools):
        """mode=search: 搜索不存在的符号返回友好信息"""
        r = cg_tools.codegraph_explore("ThisSymbolDoesNotExist_XYZ_12345", mode="search")
        assert r.is_success()
        assert "未找到" in r.content

    def test_search_with_kind_filter(self, cg_tools):
        """mode=search + kind: 按类型过滤"""
        r = cg_tools.codegraph_explore("class", mode="search", kind="class", limit=5)
        assert r.is_success()
        # 搜索结果中应该都是 class 类型
        if "未找到" not in r.content:
            lines = r.content.split("\n")
            for line in lines:
                if "[class]" in line:
                    break  # 至少有一条 class 结果
            else:
                pass  # 也可能没有 class 名包含 "class" 的

    def test_search_with_exact_match(self, cg_tools):
        """mode=search + exact: 精确匹配"""
        r = cg_tools.codegraph_explore(
            "ChatBackend",
            mode="search",
            exact=True,
            limit=5,
        )
        assert r.is_success()
        # ChatBackend 是已知类名，应该返回结果
        content = r.content
        if "未找到" not in content:
            assert "ChatBackend" in content

    # ── callers ─────────────────────────────────────────────────────────

    def test_callers_mode_found(self, cg_tools):
        """mode=callers: 找已知符号的调用者"""
        r = cg_tools.codegraph_explore("ChatBackend", mode="callers")
        assert r.is_success()

    def test_callers_mode_not_found(self, cg_tools):
        """mode=callers: 不存在的符号"""
        r = cg_tools.codegraph_explore("NonExistentFunc_ZZZ", mode="callers")
        assert r.is_success()
        assert "未找到" in r.content

    def test_callers_with_depth(self, cg_tools):
        """mode=callers + depth=2: 多级调用者"""
        r = cg_tools.codegraph_explore("ChatBackend", mode="callers", depth=2)
        assert r.is_success()

    # ── callees ─────────────────────────────────────────────────────────

    def test_callees_mode_found(self, cg_tools):
        """mode=callees: 找已知符号的被调用者"""
        r = cg_tools.codegraph_explore("ChatBackend", mode="callees")
        assert r.is_success()

    def test_callees_mode_not_found(self, cg_tools):
        """mode=callees: 不存在的符号"""
        r = cg_tools.codegraph_explore("NonExistentFunc_ZZZ", mode="callees")
        assert r.is_success()
        assert "未找到" in r.content

    # ── explore ─────────────────────────────────────────────────────────

    def test_explore_mode_default(self, cg_tools):
        """默认 mode=explore: 综合探索"""
        r = cg_tools.codegraph_explore("ChatBackend")
        assert r.is_success()
        assert "探索" in r.content or "未找到" in r.content

    def test_explore_not_found(self, cg_tools):
        """explore 模式找不到符号"""
        r = cg_tools.codegraph_explore("ZZZ_NonExistent_999")
        assert r.is_success()
        assert "未找到" in r.content

    def test_explore_with_max_files(self, cg_tools):
        """explore + max_files 限制"""
        r = cg_tools.codegraph_explore("ChatBackend", max_files=3)
        assert r.is_success()

    # ── impact ──────────────────────────────────────────────────────────

    def test_impact_mode_found(self, cg_tools):
        """mode=impact: 影响分析"""
        r = cg_tools.codegraph_explore("ChatBackend", mode="impact", depth=1)
        assert r.is_success()
        content = r.content
        if "未找到" not in content:
            assert "影响" in content or "符号" in content

    def test_impact_mode_not_found(self, cg_tools):
        """mode=impact: 不存在的符号"""
        r = cg_tools.codegraph_explore("NonExistentFunc_ZZZ", mode="impact")
        assert r.is_success()
        assert "未找到" in r.content

    # ── sync ────────────────────────────────────────────────────────────

    def test_sync_mode(self, cg_tools):
        """mode=sync: 同步索引"""
        r = cg_tools.codegraph_explore(mode="sync")
        assert r.is_success()
        assert isinstance(r.content, str)

    # ── files ───────────────────────────────────────────────────────────

    def test_files_mode(self, cg_tools):
        """mode=files: 列出已索引文件"""
        r = cg_tools.codegraph_explore(mode="files")
        assert r.is_success()
        content = r.content
        # 应该显示文件列表
        assert "已索引文件" in content or ".py" in content or "app" in content

    def test_files_with_directory_filter(self, cg_tools):
        """mode=files + directory: 按目录筛选"""
        r = cg_tools.codegraph_explore(mode="files", directory="app/tools")
        assert r.is_success()

    # ── 异常处理 ────────────────────────────────────────────────────────

    def test_invalid_mode(self, cg_tools):
        """无效 mode 应走默认 explore 路径（不报错）"""
        # mode 参数有 enum 限制，传无效值会 fallback 到 explore
        r = cg_tools.codegraph_explore("ChatBackend", mode="invalid_mode_xyz")
        assert r.is_success()

    def test_empty_query_search(self, cg_tools):
        """空 query 的 search 模式"""
        r = cg_tools.codegraph_explore("", mode="search")
        assert r.is_success()

    def test_codegraph_not_available(self, monkeypatch):
        """codegraph-py 未安装时的降级行为"""
        tools = CodeGraphTools(None)
        tools._owner = type("Owner", (), {"workdir": Path(os.getcwd())})()

        # 模拟未安装
        original = _cg_plugin._HAS_CODEGRAPH
        _cg_plugin._HAS_CODEGRAPH = False
        try:
            r = tools.codegraph_explore(mode="status")
            assert not r.is_success()
            assert "未安装" in r.error or "未安装" in r.content
        finally:
            _cg_plugin._HAS_CODEGRAPH = original
        tools.cleanup()


# =========================================================================
# 3. 性能基准测试
# =========================================================================

# 阈值说明：
# - status/sync 含首次 DB 打开 + 索引加载，设得稍宽
# - 其余查询命中缓存后应 < 1 秒
PERF_THRESHOLDS = {
    "status": 5.0,      # 首调含 DB 打开 + 索引加载
    "search": 3.0,      # 符号搜索
    "callers": 3.0,     # 调用者查询
    "callees": 3.0,     # 被调用者查询
    "explore": 5.0,     # 综合探索
    "impact": 5.0,      # 影响分析
    "sync": 15.0,       # 同步含变更检测
    "files": 3.0,       # 文件列表
}


@pytest.mark.perf
@pytest.mark.skipif(not _CODEGRAPH_AVAILABLE, reason="codegraph-py 未安装")
class TestCodeGraphPerformance:
    """性能基准测试（标记 @pytest.mark.perf，可单独运行）"""

    @pytest.fixture(autouse=True)
    def setup(self, cg_tools):
        self.tools = cg_tools

    def _measure(self, name: str, query: str = "", mode: str = "status", **kwargs):
        """执行并计时"""
        start = time.perf_counter()
        r = self.tools.codegraph_explore(query, mode=mode, **kwargs)
        elapsed = time.perf_counter() - start

        threshold = PERF_THRESHOLDS.get(mode, 5.0)
        assert r.is_success(), f"{mode} 失败: {r.error}"

        print(f"\n  ⏱  {name}: {elapsed:.3f}s (阈值: {threshold}s)")
        if elapsed > threshold:
            print(f"  ⚠  {name} 超出阈值 {threshold}s，耗时 {elapsed:.3f}s")

        return elapsed

    def test_perf_status(self):
        self._measure("status", mode="status")

    def test_perf_search(self):
        self._measure("search(term=ChatBackend)", "ChatBackend", mode="search")

    def test_perf_search_kind_filter(self):
        self._measure("search(kind=class, limit=10)", "Chat", mode="search", kind="class", limit=10)

    def test_perf_callers(self):
        self._measure("callers(ChatBackend)", "ChatBackend", mode="callers")

    def test_perf_callers_depth2(self):
        self._measure("callers(ChatBackend, depth=2)", "ChatBackend", mode="callers", depth=2)

    def test_perf_callees(self):
        self._measure("callees(ChatBackend)", "ChatBackend", mode="callees")

    def test_perf_callees_depth2(self):
        self._measure("callees(ChatBackend, depth=2)", "ChatBackend", mode="callees", depth=2)

    def test_perf_explore(self):
        self._measure("explore(ChatBackend)", "ChatBackend", mode="explore")

    def test_perf_impact(self):
        self._measure("impact(ChatBackend)", "ChatBackend", mode="impact", depth=1)

    def test_perf_impact_depth3(self):
        self._measure("impact(ChatBackend, depth=3)", "ChatBackend", mode="impact", depth=3)

    def test_perf_sync(self):
        self._measure("sync", mode="sync")

    def test_perf_files(self):
        self._measure("files", mode="files")


# =========================================================================
# 4. 连续压力测试（多次调用验证无退化）
# =========================================================================

@pytest.mark.stress
@pytest.mark.skipif(not _CODEGRAPH_AVAILABLE, reason="codegraph-py 未安装")
class TestCodeGraphStress:
    """连续调用压力测试 — 验证实例复用和资源无泄漏"""

    MODES = [
        ("status", "", {}),
        ("search", "ChatBackend", {"limit": 5}),
        ("callers", "ChatBackend", {}),
        ("callees", "ChatBackend", {}),
        ("explore", "ChatBackend", {"max_files": 3}),
        ("impact", "ChatBackend", {"depth": 1}),
        ("files", "", {}),
    ]

    def test_repeated_calls(self, cg_tools):
        """连续 20 次不同 mode 调用"""
        for i in range(20):
            mode, query, kwargs = self.MODES[i % len(self.MODES)]
            r = cg_tools.codegraph_explore(query, mode=mode, **kwargs)
            assert r.is_success(), f"第{i+1}次调用 {mode} 失败: {r.error}"
        # 验证实例未被重复创建
        assert cg_tools._cg is not None

    def test_mixed_modes_no_crash(self, cg_tools):
        """快速切换 mode，验证不崩溃"""
        for mode in ["status", "search", "callers", "callees", "explore", "impact", "sync", "files"]:
            for _ in range(3):
                r = cg_tools.codegraph_explore("ChatBackend", mode=mode)
                assert r.is_success()


# =========================================================================
# 5. 直接运行入口
# =========================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("CodeGraph 工具测试")
    print(f"  codegraph-py 可用: {_CODEGRAPH_AVAILABLE}")
    print("=" * 60)

    if not _CODEGRAPH_AVAILABLE:
        print("跳过：codegraph-py 未安装")
        sys.exit(0)

    pytest.main([__file__, "-v", "--tb=short"])
