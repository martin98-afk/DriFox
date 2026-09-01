# app/core/conversation/config.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from typing import Dict as DictType

from loguru import logger


class PermissionStrategy(Enum):
    """权限策略"""
    INTERACTIVE = "interactive"    # UI 弹窗确认
    AUTO_ALLOW = "auto_allow"      # 全部放行（AutoLoop）
    AGENT_CONFIG = "agent_config"  # 按 Agent 配置，ask 视为 deny（Gateway）
    AUTO_DENY = "auto_deny"        # 全部拒绝，只读模式（Cron 保守模式）


class HookPolicy(Enum):
    """Hook 触发策略 — 由对话引擎持有，决定本轮对话参与哪些全局 hook

    背景：插件自建对话执行栈（autoloop/chinese-chess 等）复用主程序
    tool_executor 时，ChatWorker 与 tool_executor 内部会触发全局 hooks
    （PreAssistantMessage/PostAssistantMessage/Stop + PreToolUse/PostToolUse），
    导致插件循环被动执行主对话语义的 hooks（如 Stop 续命 BLOCK 干扰循环控制）。
    引擎在此声明参与级别，hook 是否执行回归引擎决策。

    - ALL：全部触发（UI 主对话 / Gateway 默认）
    - TOOL_EVENTS_ONLY：仅工具级 PreToolUse/PostToolUse（安全审查类 hook 仍生效）
    - NONE：完全不触发（插件后台循环默认，如象棋每步棋）
    """
    ALL = "all"
    TOOL_EVENTS_ONLY = "tool_events_only"
    NONE = "none"


# 非交互策略下必须过滤的工具——这些工具需要人工参与或实时交互，
# 只在 INTERACTIVE 策略下可用
INTERACTIVE_ONLY_TOOLS = frozenset({
    "question",       # 交互式提问，需要用户选择
    "subagent_para",     # 批量分发子智能体任务，需要后续交互查询
    "subagent_status",   # 查询子智能体任务状态，配合 subagent_para 使用
    "todowrite",      # 待办事项管理，纯交互式工具
})


def filter_interactive_tools(
    tools: List[Dict],
    strategy: PermissionStrategy,
) -> List[Dict]:
    """根据权限策略过滤交互类工具

    只有 INTERACTIVE 策略保留交互类工具，其余策略全部过滤。
    """
    if strategy == PermissionStrategy.INTERACTIVE:
        return tools

    before = len(tools)
    filtered = [
        t for t in tools
        if t.get("function", {}).get("name") not in INTERACTIVE_ONLY_TOOLS
        and t.get("name") not in INTERACTIVE_ONLY_TOOLS
    ]
    removed = before - len(filtered)
    if removed > 0:
        removed_names = [
            t.get("function", {}).get("name") or t.get("name", "?")
            for t in tools
            if t.get("function", {}).get("name") in INTERACTIVE_ONLY_TOOLS
            or t.get("name") in INTERACTIVE_ONLY_TOOLS
        ]
        from loguru import logger
        logger.info(f"[ToolFilter] {strategy.value}: filtered {removed} interactive tool(s): {removed_names}")
    return filtered


@dataclass
class ConversationConfig:
    """消费者的对话配置"""
    permission_strategy: PermissionStrategy = PermissionStrategy.AUTO_ALLOW
    agent_permission_config: Dict[str, Any] = field(default_factory=dict)
    # INTERACTIVE 策略下需要外部提供权限检查回调
    interactive_check_callback: Optional[Callable[[str, dict], str]] = None
    # Hook 参与级别（默认 ALL：UI/Gateway 主对话行为不变；插件引擎建议 NONE/TOOL_EVENTS_ONLY）
    hook_policy: "HookPolicy" = HookPolicy.ALL
    # 可选：HookPolicy 插件 id（plugins/system/hook_policies/ 注册）。
    # 优先级高于 hook_policy 枚举：设置后由 HookPolicyRegistry 取对应插件对象，
    # 未设置时按 hook_policy 枚举回落（ALL→"all" / TOOL_EVENTS_ONLY→"tool_only" /
    # NONE→"none"）。允许插件自建引擎（autoloop/象棋）声明更精细的策略。
    hook_policy_id: Optional[str] = None
    # 可选：LoopPolicy 插件 id（plugins/*/loop_policies/ 注册）。
    # 设置后本引擎的 worker 按 id 直接取策略对象，不调用
    # LoopPolicyRegistry.set_active —— 避免污染全局激活槽（主对话仍用 default）。
    # 用途：插件自建引擎声明自己的循环语义，如 assistant_hub 记忆整理
    # 只需要一回合（assistant_hub_single_turn）。未设置时回落全局激活策略。
    loop_policy_id: Optional[str] = None


# ============================================================
# PermissionCache（从 app/core/permission_cache.py 迁移）
# ============================================================
class PermissionCache:
    """工具权限缓存管理器

    设计原则：
    - Round-level: 单轮对话内自动允许，工具执行完自动清理
    - Session-level: 整个会话有效，跨 Worker 持久化
    - 每个 ChatEngine 实例有独立的 PermissionCache，多窗口隔离
    """

    def __init__(self):
        self._round_cache: DictType[str, bool] = {}
        self._session_cache: DictType[str, bool] = {}

    def is_allowed(self, tool_name: str) -> bool:
        if tool_name in self._round_cache:
            return True
        if tool_name in self._session_cache:
            return True
        return False

    def allow_round(self, tool_name: str) -> None:
        self._round_cache[tool_name] = True

    def allow_session(self, tool_name: str) -> None:
        self._session_cache[tool_name] = True
        logger.info(f"[PermissionCache] 设置 session 缓存: tool={tool_name}")

    def deny(self, tool_name: str) -> None:
        if tool_name in self._round_cache:
            del self._round_cache[tool_name]
        if tool_name in self._session_cache:
            del self._session_cache[tool_name]

    def clear_round(self) -> None:
        self._round_cache.clear()

    def clear_session(self) -> None:
        self._session_cache.clear()

    def clear_all(self) -> None:
        self._round_cache.clear()
        self._session_cache.clear()

    def get_session_cache(self) -> DictType[str, bool]:
        return self._session_cache.copy()

    def sync_session_cache(self, cache: DictType[str, bool]) -> None:
        self._session_cache = cache.copy()
        logger.info(f"[PermissionCache] 同步 session 缓存: {len(cache)} 项")

    def get_cache_stats(self) -> DictType[str, int]:
        return {
            "round_count": len(self._round_cache),
            "session_count": len(self._session_cache),
        }
