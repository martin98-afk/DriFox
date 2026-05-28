# -*- coding: utf-8 -*-
"""
AutoLoop 循环引擎 — 自动任务执行循环

包含：
- AutoLoopEngine：核心状态机（规划/执行/归档三阶段）
- AutoLoopConfig：配置数据类
- AutoLoopPromptComposer：Prompt 模板管理器
"""
from app.core.engines.auto_loop.engine import AutoLoopEngine, LoopState
from app.core.engines.auto_loop.config import AutoLoopConfig
from app.core.engines.auto_loop.prompt_composer import AutoLoopPromptComposer

__all__ = ["AutoLoopEngine", "LoopState", "AutoLoopConfig", "AutoLoopPromptComposer"]
