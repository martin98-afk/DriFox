# app/core/conversation/config.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class PermissionStrategy(Enum):
    """权限策略"""
    INTERACTIVE = "interactive"    # UI 弹窗确认
    AUTO_ALLOW = "auto_allow"      # 全部放行（AutoLoop）
    AGENT_CONFIG = "agent_config"  # 按 Agent 配置，ask 视为 deny（Gateway）
    AUTO_DENY = "auto_deny"        # 全部拒绝，只读模式（Cron 保守模式）


@dataclass
class ConversationConfig:
    """消费者的对话配置"""
    permission_strategy: PermissionStrategy = PermissionStrategy.AUTO_ALLOW
    agent_permission_config: Dict[str, Any] = field(default_factory=dict)
    # INTERACTIVE 策略下需要外部提供权限检查回调
    interactive_check_callback: Optional[Callable[[str, dict], str]] = None
