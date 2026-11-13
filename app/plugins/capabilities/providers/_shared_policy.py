# -*- coding: utf-8 -*-
"""工具权限策略只读面（tool_core / ui_core 共用）。

两处 provider 的 _tool_policy_api 原为逐字同构副本，收敛于此。
"""

from __future__ import annotations


def tool_policy_api():
    """工具权限策略只读面（toggles / policy / off_behavior 的判定）。"""

    def api() -> dict:
        from app.core.tools.tool_permission_controller import resolve_tool_off_policy
        from app.utils.config import Settings

        def off_policy(name: str) -> str:
            s = Settings.get_instance()
            toggles = dict(s.tool_toggles.value or {})
            if toggles.get(name, True) is not False:
                return "allow"
            policies = dict(s.tool_permission_policy.value or {})
            behavior = s.tool_off_behavior.value or "deny"
            try:
                return str(resolve_tool_off_policy(name, None, policies, behavior) or "")
            except Exception:
                return ""

        def toggles() -> dict:
            return dict(Settings.get_instance().tool_toggles.value or {})

        return {"off_policy": off_policy, "toggles": toggles}

    return api
