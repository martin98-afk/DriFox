# -*- coding: utf-8 -*-
"""
UI 对话引擎 — 桌面界面的 LLM 对话引擎
"""
from app.core.engines.ui.engine import UIEngine

# 向后兼容别名（ChatEngine 是旧名，UIEngine 是新名）
ChatEngine = UIEngine

__all__ = ["UIEngine", "ChatEngine"]
