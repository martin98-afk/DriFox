# -*- coding: utf-8 -*-
"""项目技能源注册 — 把 core/project_skills 的 provider 挂进主程序贡献注册表。

tools/ 目录模块约定必须有 register(registry) 入口（plugin_tool_loader 扫描）；
本模块不注册工具，仅挂接 skill_source 贡献。本模块经 spec_from_file_location
加载（无 package 上下文），core 兄弟模块按路径加载。
"""

from pathlib import Path
import importlib.util
import sys

_CORE_MODULE_NAME = "assistant_hub_project_skills"

def _load_project_skills():
    mod = sys.modules.get(_CORE_MODULE_NAME)
    if mod is not None:
        return mod
    source = Path(__file__).resolve().parent.parent / "core" / "project_skills.py"
    if not source.exists():
        return None
    spec = importlib.util.spec_from_file_location(_CORE_MODULE_NAME, str(source))
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_CORE_MODULE_NAME, None)
        return None
    return module

def register(registry) -> None:
    try:
        from app.plugins.registries.contribution_registry import ContributionRegistry

        mod = _load_project_skills()
        if mod is None:
            return
        ContributionRegistry.get_instance().register_skill_source(
            plugin="assistant_hub",
            name="project_skills",
            provider=mod.provider,
            watched_dirs=mod.watched_dirs,
            description="当前项目 .drifox/skills/ 下的项目内技能",
        )
    except Exception:
        pass
