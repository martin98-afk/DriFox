# -*- coding: utf-8 -*-
"""AutoLoop 核心逻辑包 — 引擎/配置/提示词/Worker/适配器（自插件根导入 autoloop_core.*）"""

from autoloop_core.config import AutoLoopConfig
from autoloop_core.engine import AutoLoopEngine, LoopState
from autoloop_core.prompt_composer import AutoLoopPromptComposer

__all__ = [
    "AutoLoopConfig",
    "AutoLoopEngine",
    "AutoLoopPromptComposer",
    "LoopState",
]
