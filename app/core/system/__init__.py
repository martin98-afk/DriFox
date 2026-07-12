# -*- coding: utf-8 -*-
"""系统级能力模块：锁屏远程、电源管理等。"""
from app.core.system.lock_screen_remote import (
    LockScreenRemoteManager,
    get_lock_screen_remote_manager,
    register_command_handler,
)

__all__ = [
    "LockScreenRemoteManager",
    "get_lock_screen_remote_manager",
    "register_command_handler",
]
