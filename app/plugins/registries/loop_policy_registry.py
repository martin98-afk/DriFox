# -*- coding: utf-8 -*-
"""循环策略注册表 — 按 scope 分域激活插件策略（主智能体/子智能体各自独立激活槽）。

- scope="main"     主智能体（chat_worker），默认激活 "default"
- scope="subagent" 子智能体（subagent_worker），默认激活 "subagent"
策略未声明 scope 时按 "main" 兜底（向后兼容旧插件）。

零硬编码兜底：某 scope 的激活 id 不在 _policies 时回落该 scope 的默认 id，
仍不在则回落 "default"，再不在抛 RuntimeError（引导启用 system 插件）。
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

from app.plugins.contracts.loop_policy import SCOPE_MAIN, SCOPE_SUBAGENT, LoopPolicy

# 各 scope 的默认策略 id（由系统插件 plugins/system/loop_policies/*.py 注册）
_SCOPE_DEFAULTS: Dict[str, str] = {
    SCOPE_MAIN: "default",
    SCOPE_SUBAGENT: "subagent",
}


class LoopPolicyRegistry:
    def __init__(self) -> None:
        # policy_id -> (policy, source, scope)
        self._policies: Dict[str, Tuple[LoopPolicy, str, str]] = {}
        # scope -> 激活的 policy_id
        self._active: Dict[str, str] = dict(_SCOPE_DEFAULTS)
        self._lock = threading.Lock()

    def register(self, policy: LoopPolicy, source: str = "") -> None:
        scope = getattr(policy, "scope", SCOPE_MAIN) or SCOPE_MAIN
        with self._lock:
            self._policies[policy.id] = (policy, source, scope)

    def unregister_source(self, source: str) -> None:
        with self._lock:
            dead = [k for k, (_, s, _) in self._policies.items() if s == source]
            for k in dead:
                del self._policies[k]
            # 各 scope 激活槽失效则回落该 scope 默认
            for scope, active_id in self._active.items():
                if active_id not in self._policies:
                    self._active[scope] = _SCOPE_DEFAULTS.get(scope, _SCOPE_DEFAULTS[SCOPE_MAIN])

    def set_active(self, policy_id: str, scope: Optional[str] = None) -> bool:
        """激活策略。scope 缺省时按策略注册时的 scope 归位（旧调用 set_active("minimal") 零变化）。"""
        with self._lock:
            item = self._policies.get(policy_id)
            if item is None:
                return False
            target_scope = scope if scope is not None else item[2]
            self._active[target_scope] = policy_id
            return True

    def get_active(self, scope: str = SCOPE_MAIN) -> LoopPolicy:
        with self._lock:
            active_id = self._active.get(scope, _SCOPE_DEFAULTS[SCOPE_MAIN])
            item = self._policies.get(active_id)
            if item is None:
                # 回落该 scope 默认 → 全局 default
                item = self._policies.get(_SCOPE_DEFAULTS.get(scope, "")) or self._policies.get(
                    _SCOPE_DEFAULTS[SCOPE_MAIN]
                )
        if item is None:
            # P3 兜底：无任何 LoopPolicy 插件（system 整体被禁/加载失败）→ 返回内置最简策略 + warning
            # 行为对齐 DefaultLoopPolicy 子集：tool_calls_found → CONTINUE；其余 STOP。
            # 无限 max_rounds（与现状 while 无上限一致）。
            from loguru import logger
            from app.plugins.registries._builtin_fallback import BuiltInDefaultLoopPolicy

            logger.warning(
                f"[LoopPolicyRegistry] 未加载任何 LoopPolicy 插件（scope={scope}），"
                f"降级使用内置兜底策略"
            )
            return BuiltInDefaultLoopPolicy()
        return item[0]

    def policies(self, scope: Optional[str] = None) -> Dict[str, LoopPolicy]:
        with self._lock:
            if scope is None:
                return {k: v[0] for k, v in self._policies.items()}
            return {k: v[0] for k, v in self._policies.items() if v[2] == scope}

    @staticmethod
    def get_instance() -> "LoopPolicyRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = LoopPolicyRegistry()
            return _instance


_instance: Optional[LoopPolicyRegistry] = None
_instance_lock = threading.Lock()
