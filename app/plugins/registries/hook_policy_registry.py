# -*- coding: utf-8 -*-
"""Hook 触发策略注册表 — 按 scope 分域激活插件策略。

设计仿照 LoopPolicyRegistry（`loop_policy_registry.py`）：

- scope="main"         主智能体（chat_worker），默认激活 "all"
- scope="subagent"     子智能体（subagent_worker），默认激活 "subagent_default"
- scope="team_member"  团队成员窗口，默认激活 "team_member"

策略未声明 scope 时按 "main" 兜底（向后兼容旧插件）。

零硬编码兜底：某 scope 的激活 id 不在 _policies 时回落该 scope 的默认 id，
再不在则回落 "all"，最后抛 RuntimeError 引导启用 system 插件。
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

from app.plugins.contracts.hook_policy import (
    SCOPE_MAIN,
    SCOPE_SUBAGENT,
    SCOPE_TEAM_MEMBER,
    HookPolicy,
)

# 各 scope 的默认策略 id（由系统插件 plugins/system/hook_policies/*.py 注册）
_SCOPE_DEFAULTS: Dict[str, str] = {
    SCOPE_MAIN: "all",
    SCOPE_SUBAGENT: "subagent_default",
    SCOPE_TEAM_MEMBER: "team_member",
}

# 各 scope 的最终兜底（任何 scope 失效时退化到此）
_FALLBACK_POLICY_ID = "all"


class HookPolicyRegistry:
    def __init__(self) -> None:
        # policy_id -> (policy, source, scope)
        self._policies: Dict[str, Tuple[HookPolicy, str, str]] = {}
        # scope -> 激活的 policy_id
        self._active: Dict[str, str] = dict(_SCOPE_DEFAULTS)
        self._lock = threading.Lock()

    def register(self, policy: HookPolicy, source: str = "") -> None:
        scope = getattr(policy, "scope", SCOPE_MAIN) or SCOPE_MAIN
        with self._lock:
            self._policies[policy.id] = (policy, source, scope)

    def unregister_source(self, source: str) -> None:
        with self._lock:
            dead = [k for k, (_, s, _) in self._policies.items() if s == source]
            for k in dead:
                del self._policies[k]
            # 各 scope 激活槽失效则回落该 scope 默认 → 最终兜底
            for scope, active_id in self._active.items():
                if active_id not in self._policies:
                    self._active[scope] = _SCOPE_DEFAULTS.get(scope, _FALLBACK_POLICY_ID)

    def set_active(self, policy_id: str, scope: Optional[str] = None) -> bool:
        """激活策略。scope 缺省时按策略注册时的 scope 归位。"""
        with self._lock:
            item = self._policies.get(policy_id)
            if item is None:
                return False
            target_scope = scope if scope is not None else item[2]
            self._active[target_scope] = policy_id
            return True

    def get_active(self, scope: str = SCOPE_MAIN) -> HookPolicy:
        """取某 scope 当前激活的策略对象

        回落链：scope 默认 → 全局 all → 抛 RuntimeError。
        """
        with self._lock:
            active_id = self._active.get(scope, _SCOPE_DEFAULTS.get(scope, _FALLBACK_POLICY_ID))
            item = self._policies.get(active_id)
            if item is None:
                # 回落该 scope 默认 → 最终兜底 all
                item = self._policies.get(_SCOPE_DEFAULTS.get(scope, "")) or self._policies.get(_FALLBACK_POLICY_ID)
        if item is None:
            raise RuntimeError(f"未加载任何 HookPolicy 插件（scope={scope}），请确认 system 插件已启用")
        return item[0]

    def policies(self, scope: Optional[str] = None) -> Dict[str, HookPolicy]:
        with self._lock:
            if scope is None:
                return {k: v[0] for k, v in self._policies.items()}
            return {k: v[0] for k, v in self._policies.items() if v[2] == scope}

    @staticmethod
    def get_instance() -> "HookPolicyRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = HookPolicyRegistry()
            return _instance


_instance: Optional[HookPolicyRegistry] = None
_instance_lock = threading.Lock()
