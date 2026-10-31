# -*- coding: utf-8 -*-
"""assistant_hub.core — 助手中心核心子包（persona / 记忆 store / 经验库）。

模块经 importlib 按文件路径加载（assistant_manager._load_core_module），
不依赖包上下文；本 __init__ 仅为常规 import 路径兜底。核心模块不做任何 LLM 调用。
"""
