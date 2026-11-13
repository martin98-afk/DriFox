# -*- coding: utf-8 -*-
"""插件自有模块加载器（ui 入口与后续所有消费方共用一份）。

``ui/`` 由宿主以独立模块（``ui_plugin_system-im``）加载，插件根与 ``core/``
不在其包路径内，只能按文件路径加载：

- ``core/`` 作为包 ``system_im_core`` 加载（内部模块用相对 import）；
- ``engine.py`` / ``service.py`` 作为顶层模块 ``system_im_engine`` /
  ``system_im_service`` 加载。

模块名固定并写入 ``sys.modules``，保证同一进程内多次加载拿到同一类对象
（否则 isinstance 与 Qt 信号连接会跨类失配）。命名空间在 plugin.json 的
``module_prefixes`` 中声明，热重载由宿主按声明统一清理。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

CORE_PACKAGE = "system_im_core"
ROOT_MODULES = {"engine": "system_im_engine", "service": "system_im_service"}

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_core() -> Any:
    """加载（或复用）``core`` 包，返回包模块。"""
    cached = sys.modules.get(CORE_PACKAGE)
    if cached is not None:
        return cached
    pkg_dir = _PLUGIN_ROOT / "core"
    init_py = pkg_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        CORE_PACKAGE,
        str(init_py),
        submodule_search_locations=[str(pkg_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法创建 core 包 spec: {init_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[CORE_PACKAGE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(CORE_PACKAGE, None)
        raise
    return module


def load_root_module(name: str) -> Any:
    """加载（或复用）插件根的顶层模块：``engine`` / ``service``。"""
    mod_name = ROOT_MODULES.get(name)
    if mod_name is None:
        raise ValueError(f"未知插件模块: {name!r}（可选 {sorted(ROOT_MODULES)}）")
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / f"{name}.py"
    if not path.exists():
        raise RuntimeError(f"插件模块不存在: {path}")
    load_core()  # engine/service 依赖 system_im_core.*
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法创建模块 spec: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return module
