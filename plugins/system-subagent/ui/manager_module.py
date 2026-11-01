# -*- coding: utf-8 -*-
"""插件根 manager.py 的加载器（ui 入口与 host 共用一份）

ui/ 被宿主以独立模块加载（``ui_plugin_system-subagent``），插件根不在其包内，
只能用文件路径加载。模块名固定并缓存到 ``sys.modules``，保证同一进程内
UI 入口与窗口宿主拿到同一个类对象（否则 isinstance / 信号连接会跨类失配）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

MANAGER_MODULE = "system_subagent_manager"


def load_manager_module() -> Any:
    source = Path(__file__).resolve().parent.parent / "manager.py"
    spec = importlib.util.spec_from_file_location(MANAGER_MODULE, str(source))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法创建 manager 模块 spec: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MANAGER_MODULE] = module
    spec.loader.exec_module(module)
    return module
