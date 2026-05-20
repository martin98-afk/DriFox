# -*- coding: utf-8 -*-
"""
[兼容重导出] 聊天引擎 — 已迁移至 app/core/engines/ui/

保持向后兼容，所有原有 import 语句继续可用。
"""
from app.core.engines.ui import ChatEngine  # noqa: F401

__all__ = ["ChatEngine"]
