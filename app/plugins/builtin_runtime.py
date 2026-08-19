# -*- coding: utf-8 -*-
"""运行时内置实现 — ModelAdapter / LoopPolicy / StorageEngine 的默认实现。

行为零变化原则：本模块代码从 chat_worker / store 逐字搬运判定逻辑，
仅做 self.llm_config → llm_config 的机械变换。插件目录可注册高优先级
实现覆盖这些默认值（见 Task 8 加载器）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.provider_profile import detect_provider_family
from app.plugins.contracts.loop_policy import LoopDecision, LoopPolicy, LoopState
from app.plugins.contracts.model_adapter import ProtocolFlags


class OpenAIAdapter:
    """OpenAI 标准协议适配器（含 gemini/reasoning/responses 分支检测，兜底优先级 1）"""

    id = "openai"

    def matches(self, llm_config: Dict[str, Any]) -> int:
        return 1  # 兜底：任何 llm_config 都可走本适配器

    def protocol_flags(self, llm_config: Dict[str, Any]) -> ProtocolFlags:
        return ProtocolFlags(
            is_gemini=self._is_gemini(llm_config),
            requires_reasoning_content=self._requires_reasoning(llm_config),
            use_responses_api=self._use_responses(llm_config),
        )

    def _requires_reasoning(self, llm_config: Dict[str, Any]) -> bool:
        """thinking 模式下，兼容要求 tool-call assistant 保留 reasoning_content 的 provider。

        （逐字搬运 chat_worker._requires_reasoning_content 注释与逻辑）
        deepseek 系模型（含 opencode.ai 等中转平台承载的 deepseek-v4 系列）在
        thinking mode 下要求 tool_calls assistant 消息必须携带 reasoning_content
        字段（可为空串），否则上游 Console 报 400。
        """
        if llm_config.get("思考模式") is not True:
            return False
        family = detect_provider_family(llm_config)
        if family == "deepseek":
            return True
        # opencode 等中转平台承载 deepseek 系模型时（模型名以 deepseek 开头），
        # 上游协议与官方 Console 一致，同样需要 reasoning_content 回传
        model = str(llm_config.get("模型名称", "") or "").lower()
        return model.startswith("deepseek")

    def _is_gemini(self, llm_config: Dict[str, Any]) -> bool:
        """是否为 Gemini 模型（需特殊处理 thought_signature）。（逐字搬运 _is_gemini_model）"""
        try:
            if detect_provider_family(llm_config) == "gemini":
                return True
        except Exception:
            pass
        # 兜底：模型名含 gemini（如 models/gemini-3-flash-preview 的 startswith 判断会漏）
        try:
            model = str((llm_config or {}).get("模型名称", "") or "").lower()
            if "gemini" in model:
                return True
        except Exception:
            pass
        return False

    def _use_responses(self, llm_config: Dict[str, Any]) -> bool:
        """是否走 Responses API（/v1/responses）。（逐字搬运 _use_responses_api）"""
        try:
            override = llm_config.get("使用ResponsesAPI")
            if override is not None:
                return bool(override)
            model = str(llm_config.get("模型名称", "") or "").lower()
            return model.startswith("gpt-5")
        except Exception:
            return False


_adapter_registered = False


def register_builtin_runtime_adapters(registry) -> None:
    """把内置适配器注册进指定 registry（幂等由调用方/registry 覆盖语义保证）"""
    registry.register(OpenAIAdapter(), source="builtin")


# ── LoopPolicy 内置默认实现 ────────────────────────────────
class DefaultLoopPolicy:
    """默认循环策略 — 与现有 chat_worker.run() 行为逐点等价"""

    id = "default"

    def should_continue(self, state: LoopState) -> LoopDecision:
        if state.repetitive_loop_detected:
            return LoopDecision.CONTINUE  # 现状：静默清理后继续
        if state.tool_calls_found:
            return LoopDecision.CONTINUE
        if state.stop_hook_injected:
            return LoopDecision.CONTINUE  # 现状：Stop hook 续命一轮
        return LoopDecision.STOP

    def max_rounds(self, llm_config: Dict[str, Any]) -> Optional[int]:
        """默认不限轮数（现状 while 无上限）；配置键可设上限"""
        try:
            v = llm_config.get("最大循环轮数") if llm_config else None
            return int(v) if v else None
        except (TypeError, ValueError):
            return None


def register_builtin_loop_policies(registry) -> None:
    registry.register(DefaultLoopPolicy(), source="builtin")


def ensure_builtin_runtime() -> None:
    """幂等注册全部内置运行时实现（adapters + loop policies + storages）"""
    global _adapter_registered
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry
    from app.plugins.registries.storage_registry import StorageRegistry

    if _adapter_registered:
        return
    register_builtin_runtime_adapters(ModelAdapterRegistry.get_instance())
    register_builtin_loop_policies(LoopPolicyRegistry.get_instance())
    register_builtin_storages(StorageRegistry.get_instance())
    _adapter_registered = True


# 向后兼容 Task 2/3 已用的入口名
ensure_builtin_adapters = ensure_builtin_runtime


# ── SessionStorageEngine 内置实现 ──────────────────────────
class SqliteStorageEngine:
    """SQLite 存储引擎 — 薄包装现有 SessionRepository（行为零变化）"""

    id = "sqlite"

    def __init__(self, db_dir: str = None):
        from app.core.store.session_store import SessionStore

        store = SessionStore(db_dir=db_dir) if db_dir is not None else SessionStore.get_instance()
        # 复用 SessionStore 内部已初始化的 SessionRepository，避免重复构造连接池；
        # SessionRepository 期望的是 DatabaseManager，而非 SessionStore 本身。
        self._repo = store.session_repo

    def save(self, session: dict) -> bool:
        return self._repo.save(session)

    def get(self, session_id: str):
        return self._repo.get(session_id)

    def get_all(self, limit: int = 100, offset: int = 0):
        return self._repo.get_all(limit=limit, offset=offset)

    def get_by_project(self, project: str, limit: int = 100):
        return self._repo.get_by_project(project, limit=limit)

    def get_projects(self):
        return self._repo.get_projects()

    def delete(self, session_id: str) -> bool:
        return self._repo.delete(session_id)


def register_builtin_storages(registry) -> None:
    """把内置 sqlite 引擎注册进指定 registry（幂等由调用方/registry 覆盖语义保证）"""
    registry.register(SqliteStorageEngine(), source="builtin")
