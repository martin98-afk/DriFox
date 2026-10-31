# -*- coding: utf-8 -*-
"""项目技能源 — 扫描 <workdir>/.drifox/skills/ 的动态技能 provider。

语义：
- 放进目录即被发现（与插件静态技能同权），切项目/标签页自动换批
  （求值与 watched_dirs 都按调用方传入的 workdir，无全局状态）
- 自动启用（auto_enable=True，自动进「偏好技能」段）
- 禁用：<workdir>/.drifox/skills/.disabled.json → {"disabled": ["name-a"]}
- 开关：当前生效助手的 project_skills_enabled=False 时返回空

被 tools/project_skills_source.py 注册进 ContributionRegistry（plugin="assistant_hub"）。
加载约定：本模块可能经 spec_from_file_location 独立加载（无 package 上下文），
禁止相对导入。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

_MANAGER_MODULE_NAME = "assistant_hub_manager"

def skills_root(workdir: Any) -> Path:
    return Path(workdir) / ".drifox" / "skills"

def _manager():
    """按路径加载 assistant_manager（sys.modules 单例，与 hooks/ui 共享）"""
    import importlib.util
    import sys

    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is not None:
        return mod.AssistantManager.get_instance()
    source = Path(__file__).resolve().parent.parent / "assistant_manager.py"
    if not source.exists():
        return None
    spec = importlib.util.spec_from_file_location(_MANAGER_MODULE_NAME, str(source))
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MANAGER_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_MANAGER_MODULE_NAME, None)
        return None
    return module.AssistantManager.get_instance()

def enabled_for_active_assistant() -> bool:
    """当前生效助手的 project_skills_enabled（默认开；助手体系不可用时视为开）"""
    try:
        mgr = _manager()
        if mgr is None:
            return True
        aid = mgr.active_id()
        if not aid or not mgr.has(aid):
            return True
        return bool(getattr(mgr.get(aid), "project_skills_enabled", True))
    except Exception:
        return True

def _load_disabled(root: Path) -> set:
    try:
        data = json.loads((root / ".disabled.json").read_text(encoding="utf-8"))
        items = data.get("disabled", []) if isinstance(data, dict) else []
        return {str(x) for x in items}
    except (OSError, ValueError):
        return set()

def _parse_frontmatter(md_path: Path) -> Dict[str, str]:
    try:
        content = md_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not content.startswith("---"):
        return {}
    try:
        import yaml  # 延迟导入

        meta = yaml.safe_load(content.split("---", 2)[1]) or {}
        return {
            str(k): str(v)
            for k, v in meta.items()
            if isinstance(v, (str, int)) and k in ("name", "description")
        }
    except Exception:
        return {}

def provider(workdir: Any) -> List[Dict[str, Any]]:
    if not enabled_for_active_assistant():
        return []
    root = skills_root(workdir)
    if not root.is_dir():
        return []
    disabled = _load_disabled(root)
    out: List[Dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for d in entries:
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        md = d / "SKILL.md"
        if not md.exists():
            md = d / "skill.md"
        if not md.exists():
            continue
        meta = _parse_frontmatter(md)
        name = meta.get("name") or d.name
        if name in disabled:
            continue
        out.append({
            "name": name,
            "qualified_name": name,
            "description": meta.get("description", ""),
            "path": str(d.resolve()),
            "source": "project",
            "auto_enable": True,
        })
    return out

def watched_dirs(workdir: Any) -> List[Path]:
    """目录不存在也返回：主程序按存在性计入缓存 key，目录创建即失效重扫"""
    return [skills_root(workdir)]
