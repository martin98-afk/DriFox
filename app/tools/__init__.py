# -*- coding: utf-8 -*-
"""
内置工具集 - 深度重构：自动聚合工具模块，消除手动委托

This module provides a dynamic tool registry that automatically discovers
and aggregates tools from separate tool modules, eliminating the need
for manual method forwarding in a shallow facade.
"""

import copy
import platform
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QObject

from app.core.lsp.lsp_manager import get_lsp_manager
from app.core.lsp.lsp_tools import LspToolsIntegration

# Import all tool modules（工具插件化：file/web/automation/codegraph/terminal/diagnostics 工具实现已迁插件）
from app.tools.mcp_tools import MCPClientManager
from app.tools.result import ToolResult


class BuiltinTools(QObject):
    """
    Builtin tools registry - automatically aggregates methods from tool modules.

    This is a deep module: it handles dynamic dispatch to registered tools
    and manages session state, without requiring manual method forwarding
    for every tool method.
    """

    def __init__(self, homepage=None, workdir: str = None):
        super().__init__(homepage)
        self.homepage = homepage

        # 团队上下文（不依赖 homepage，由 backend 设置）
        self._team_window_id: str = ""
        self._team_agent_name: str = ""

        if workdir:
            self.workdir = Path(workdir)
        else:
            try:
                from app.utils.utils import resource_path

                self.workdir = Path(resource_path("/"))
            except Exception:
                self.workdir = Path.cwd()

        # Initialize all tool instances
        self._tools: Dict[str, Any] = {}
        self._register_tools()

        # MCP 客户端管理器（全局单例，多窗口共享连接）
        self._mcp_manager = MCPClientManager.get_instance()
        self._mcp_manager.acquire()

        # Dependencies injected later
        self._sub_agent_manager = None
        self._agent_manager = None
        self._set_stage_callback = None
        self._memory_manager = None
        self._get_llm_config = None
        self._get_session_messages = None
        self._current_project = "默认项目"  # 当前项目（由 set_current_project() 设置）

        logger.info(f"[BuiltinTools] Workdir: {self.workdir}, loaded {len(self._tools)} tool modules")

    def _register_tools(self):
        """Register all tool modules - add new tools here"""
        # 工具插件化：工具实现（含 task/team 服务）已全部迁插件，
        # 此处仅保留主程序平台服务：LSP 集成（lsp 工具 impl 经 services 调用）。
        # LSP 工具集成
        self._lsp_tools = LspToolsIntegration(get_lsp_manager(), owner=self)
        self._tools["lsp"] = self._lsp_tools

    @property
    def mcp_manager(self):
        return self._mcp_manager

    def __getattr__(self, name: str):
        """
        Dynamic dispatch: look for method on tool modules.

        This eliminates the need for manual method forwarding.
        If a method isn't found on this class, it searches all
        registered tool modules and dispatches to the first match.
        """
        # Search all tool modules for the method
        for tool in self._tools.values():
            if hasattr(tool, name):
                method = getattr(tool, name)
                return method

        # If not found, raise AttributeError (Python default)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    # The following methods have special handling (additional logic)
    # so they are kept here instead of dynamic dispatch

    def set_team_context(self, window_id: str, agent_name: str):
        """设置团队上下文（由 ChatBackend 在初始化/切换 agent 时调用）"""
        self._team_window_id = window_id
        self._team_agent_name = agent_name

    def cleanup(self):
        """
        彻底清理 BuiltinTools 的所有缓存，防止内存泄漏。
        应该在对话结束后或切换会话时调用。
        """
        # 清理子智能体管理器（工具插件待办/技能状态随插件生命周期，主程序不持有）
        self._sub_agent_manager = None

        # 释放 MCP 引用（引用计数归零时才真正断开）
        self._mcp_manager.release()

    def gitee_upload(self, local_path: str) -> ToolResult:
        """将本地文件上传至 Gitee 仓库，返回公开下载链接（平台能力，供 upload_file 工具服务调用）"""
        from app.gateway.utils.gitee_uploader import GiteeUploader
        from app.utils.utils import resolve_path

        full_path = resolve_path(self.workdir, local_path)
        uploader = GiteeUploader.get_instance()
        if not uploader.is_configured():
            return ToolResult(False, error="Gitee 未配置。请在设置中填写 Gitee Token、Owner、Repo。")
        url, err = uploader.upload_file(str(full_path))
        if err:
            return ToolResult(False, error=f"Gitee 上传失败: {err}")
        return ToolResult(
            True,
            content={
                "url": url,
                "filename": full_path.name,
                "local_path": str(full_path),
            },
        )

    def set_memory_manager(self, memory_manager):
        self._memory_manager = memory_manager

    def set_llm_config_getter(self, getter):
        self._get_llm_config = getter

    def set_session_messages_getter(self, getter):
        self._get_session_messages = getter

    def set_agent_manager(self, agent_manager):
        """设置 AgentManager 实例，用于动态生成工具 schema"""
        self._agent_manager = agent_manager

    def set_current_project(self, project: str):
        """设置当前项目（供更新项目笔记时使用）"""
        self._current_project = project

    def set_workdir(self, workdir: str):
        """动态更新工作目录（用于 AutoLoop 自定义项目路径）

        各工具模块通过 workdir 属性动态获取最新值，无需逐个传播。
        """
        from pathlib import Path

        self.workdir = Path(workdir)
        logger.info(f"[BuiltinTools] Workdir updated to: {self.workdir}")


def create_builtin_tools(homepage=None, workdir: str = None) -> BuiltinTools:
    """创建内置工具实例"""
    return BuiltinTools(homepage, workdir)


# Tool schema definitions - keep separate from class
# Each tool module can provide its own schema in the future

# ============================================================
# 工具插件化：系统插件工具加载 + schema 聚合（registry 驱动）
# ============================================================
# 系统工具插件位于 plugins/system/tools/*.py，通过 register(registry) 注册
# schema / impl / icon / cn_name / danger / group / description / aliases。
# 模块导入时加载一次（幂等），热重载由 PluginToolWatcher 后台轮询驱动。

_CACHE_RESULT: Optional[List[Dict]] = None
_CACHE_TIMESTAMP: float = 0.0
_CACHE_VERSION: int = -1  # 缓存对应的 registry 版本（版本变化即失效）
_CACHE_AGENT_REF: Optional[object] = None  # 缓存对应的 agent_manager 引用（is 比对，换 agent 实例即失效）
_CACHE_TTL = 5.0  # 秒

_plugin_tools_loaded = False


def _ensure_plugin_tools_loaded() -> None:
    """确保系统插件工具已加载（模块导入时 + 幂等，进程级一次）"""
    global _plugin_tools_loaded
    if _plugin_tools_loaded:
        return
    _plugin_tools_loaded = True
    try:
        from app.plugins.loaders.plugin_tool_loader import ensure_plugin_tool_watcher, load_plugin_tools

        load_plugin_tools()
        ensure_plugin_tool_watcher()
    except Exception as e:
        logger.warning(f"[BuiltinTools] 插件工具加载失败: {e}")


def _invalidate_schema_cache(version: int) -> None:
    """ToolRegistry 变更监听：registry 版本变化时失效 get_builtin_tools_schema 缓存"""
    global _CACHE_RESULT
    _CACHE_RESULT = None


# 注册缓存失效钩子（模块加载时；立即回调一次用于初始化）
try:
    from app.tools.registry import ToolRegistry

    ToolRegistry.get_instance().on_change(_invalidate_schema_cache)
except Exception:
    pass

# 模块导入即加载系统插件工具（早于任何 schema/权限/渲染读取）
_ensure_plugin_tools_loaded()


def get_builtin_tools_schema(agent_manager=None, builtin_tools=None) -> List[Dict]:
    """获取工具的 schema 定义（用于给 LLM 调用，registry 驱动）

    系统插件工具（plugins/system/tools/*.py）与第三方插件工具经
    ToolRegistry 注册后自动进入 schema 流；MCP 工具在此动态注入。

    Args:
        agent_manager: AgentManager 实例，用于动态注入可用子智能体列表
        builtin_tools: BuiltinTools 实例，用于动态注入 MCP 工具 schema
    """
    global _CACHE_RESULT, _CACHE_TIMESTAMP, _CACHE_VERSION, _CACHE_AGENT_REF

    _ensure_plugin_tools_loaded()

    # 版本号驱动缓存失效（双保险：监听器 + 版本比对）
    try:
        current_version = ToolRegistry.get_instance().version()
    except Exception:
        current_version = -1

    now = time.monotonic()
    if (
        _CACHE_RESULT is not None
        and now - _CACHE_TIMESTAMP < _CACHE_TTL
        and _CACHE_VERSION == current_version
        and _CACHE_AGENT_REF is agent_manager  # 引用比对：agent 实例更换即失效（多窗口隔离）
    ):
        return copy.deepcopy(_CACHE_RESULT)

    # 动态获取子智能体名称列表
    subagent_names = []
    if agent_manager and hasattr(agent_manager, "list_subagent_names"):
        try:
            subagent_names = agent_manager.list_subagent_names(include_hidden=True)
        except Exception:
            pass

    # 从 registry 读取全部 schema（深拷贝，避免 description 改写污染注册数据）
    try:
        schemas = ToolRegistry.get_instance().schemas()
    except Exception as e:
        logger.warning(f"[BuiltinTools] 读取 registry schema 失败: {e}")
        schemas = []

    # 动态生成 subagent_para 工具描述
    subagent_para_desc = "批量分发子智能体任务(并行执行)。调完后不可等——继续调其他工具或结束本轮。完成后系统发[后台任务状态]，届时用subagent_status查。"
    if subagent_names:
        subagent_para_desc += "\n\n可用子智能体见系统提示 ## Available Subagents。"

    for schema in schemas:
        name = schema.get("function", {}).get("name", "")
        if name == "subagent_para":
            schema["function"]["description"] = subagent_para_desc
        elif name == "subagent_dag":
            if subagent_names:
                schema["function"]["description"] += "\n\n可用子智能体见系统提示 ## Available Subagents。"
        elif name == "bash":
            schema["function"]["description"] += f"\n\n当前平台: {platform.system()}。"

    # 动态注入 MCP 工具 schema
    if builtin_tools and hasattr(builtin_tools, "_mcp_manager"):
        try:
            mcp_schemas = builtin_tools._mcp_manager.get_tool_schemas()
            if mcp_schemas:
                schemas.extend(mcp_schemas)
                logger.debug(f"[BuiltinTools] 注入 {len(mcp_schemas)} 个 MCP 工具 schema")
        except Exception:
            pass

    # 动态注入 LSP 服务器状态到 lsp 工具描述
    try:
        from app.core.lsp.lsp_manager import get_lsp_manager

        lsp_mgr = get_lsp_manager()
        clients = lsp_mgr._clients
        if clients:
            running = [n for n, c in clients.items() if c.is_running]
            status_text = f"已启动LSP: {', '.join(running) if running else '(无)'}。"
            for schema in schemas:
                if schema.get("function", {}).get("name", "") == "lsp":
                    schema["function"]["description"] += f"\n\n{status_text}"
                    break
    except Exception:
        pass

    # 写入缓存
    _CACHE_RESULT = schemas
    _CACHE_TIMESTAMP = now
    _CACHE_VERSION = current_version
    _CACHE_AGENT_REF = agent_manager

    return copy.deepcopy(schemas)
