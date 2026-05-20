# -*- coding: utf-8 -*-
"""
对话引擎模块 — 所有对话引擎的统一入口

引擎目录结构：
    engines/
    ├── base.py              # BaseEngine 抽象基类
    ├── ui/                  # UI 桌面对话引擎
    ├── gateway/             # Gateway 即时通讯引擎
    └── auto_loop/           # AutoLoop 自动循环引擎

使用方式：
    from app.core.engines.ui import UIEngine, ChatEngine
    from app.core.engines.gateway import GatewayEngine
    from app.core.engines.auto_loop import AutoLoopEngine, LoopState, AutoLoopConfig
"""
from app.core.engines.base import BaseEngine
from app.core.engines.ui import UIEngine, ChatEngine
from app.core.engines.gateway import GatewayEngine
from app.core.engines.auto_loop import AutoLoopEngine, LoopState, AutoLoopConfig, AutoLoopPromptComposer

__all__ = [
    "BaseEngine",
    "UIEngine",
    "ChatEngine",
    "GatewayEngine",
    "AutoLoopEngine",
    "LoopState",
    "AutoLoopConfig",
    "AutoLoopPromptComposer",
]
