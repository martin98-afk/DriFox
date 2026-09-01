# -*- coding: utf-8 -*-
"""persona.py — 助手人格注册表（DriFox 特色预置 + 用户自定义）。

人格 = 三类资产合一（对齐 openhanako yuan 概念，但内容 DriFox 原创优先）：
- prompt（人格底座模板）：注入 system prompt 的人格段，支持 {{userName}}/{{agentName}}
- tag（思考块标签）：UI 方角小牌与思考块协议名（build=推演 / hanako=MOOD / 无=空）
- avatar：personas/avatars/<id>.png

存储：
- 内置：plugins/assistant_hub/personas/*.md（frontmatter + 正文，随插件分发）
- 自定义：<app_data>/assistant_hub/personas.json（builtin 不可删改）
- "none"：恒存在的空人格（纯净助手，UI 走横幅不进 chips）
"""
from __future__ import annotations

import getpass
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Persona:
    id: str
    name: str = ""
    description: str = ""
    tag: str = ""
    avatar: str = ""
    builtin: bool = False
    prompt: str = ""


def resolve_user_name() -> str:
    """用户名：系统账号，空回落「用户」。（{{userName}} 模板变量来源）"""
    try:
        name = (getpass.getuser() or "").strip()
    except Exception:
        name = ""
    return name or "用户"


def _parse_frontmatter(text: str) -> tuple:
    """解析 frontmatter（--- 包裹的 yaml 键值平文，避免引入 yaml 依赖到本模块）。"""
    meta: Dict[str, str] = {}
    body = text
    if text.lstrip().startswith("---"):
        lines = text.lstrip().splitlines()
        if lines[0].strip() == "---":
            end = -1
            for i, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    end = i
                    break
            if end > 0:
                for line in lines[1:end]:
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip()] = v.strip().strip('"').strip("'")
                body = "\n".join(lines[end + 1 :])
    return meta, body.strip()


def _builtin_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "personas"


class PersonaRegistry:
    """人格注册表单例（内置只读 + 自定义持久化）。"""

    _instance: Optional["PersonaRegistry"] = None

    def __init__(self, custom_path: Optional[Path] = None, builtin_dir: Optional[Path] = None):
        self._custom_path = custom_path or (_persona_data_dir() / "personas.json")
        self._builtin_dir = builtin_dir or _builtin_dir()
        self._custom: Dict[str, Persona] = {}
        self._builtin: Dict[str, Persona] = {}
        self._load_builtins()
        self._load_custom()

    # ── 单例 ──
    @classmethod
    def get_instance(
        cls, custom_path: Optional[Path] = None, builtin_dir: Optional[Path] = None, reset: bool = False
    ) -> "PersonaRegistry":
        if cls._instance is None or reset:
            cls._instance = cls(custom_path=custom_path, builtin_dir=builtin_dir)
        return cls._instance

    # ── 加载 ──
    def _load_builtins(self) -> None:
        self._builtin = {}
        for md in sorted(self._builtin_dir.glob("*.md")):
            try:
                meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
            except Exception:
                continue
            pid = meta.get("id") or md.stem
            self._builtin[pid] = Persona(
                id=pid,
                name=meta.get("name") or pid,
                description=meta.get("description") or "",
                tag=meta.get("tag") or "",
                avatar=meta.get("avatar") or "",
                builtin=True,
                prompt=body if pid != "none" else "",
            )

    def _load_custom(self) -> None:
        self._custom = {}
        if not self._custom_path.exists():
            return
        try:
            data = json.loads(self._custom_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            self._custom[str(item["id"])] = Persona(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                description=str(item.get("description") or ""),
                tag=str(item.get("tag") or ""),
                avatar=str(item.get("avatar") or ""),
                builtin=False,
                prompt=str(item.get("prompt") or ""),
            )

    # ── 查询 ──
    def list_all(self) -> List[Persona]:
        merged = dict(self._builtin)
        merged.update(self._custom)
        return list(merged.values())

    def get(self, pid: str) -> Optional[Persona]:
        for p in self.list_all():
            if p.id == pid:
                return p
        return None

    def avatar_path(self, pid: str) -> Optional[Path]:
        """内置头像优先 personas/avatars/<avatar 或 id>.png；自定义 avatar 存绝对路径。"""
        p = self.get(pid)
        if p is None:
            return None
        if p.avatar:
            cand = Path(p.avatar)
            if cand.is_absolute() and cand.exists():
                return cand
            rel = self._builtin_dir / "avatars" / p.avatar
            if rel.exists():
                return rel
        rel = self._builtin_dir / "avatars" / f"{pid}.png"
        return rel if rel.exists() else None

    # ── 自定义维护 ──
    def upsert(self, p: Persona) -> bool:
        if p.builtin or p.id in self._builtin:
            return False
        self._custom[p.id] = p
        self._persist()
        return True

    def delete(self, pid: str) -> bool:
        if pid in self._builtin or pid not in self._custom:
            return False
        self._custom.pop(pid, None)
        self._persist()
        return True

    def _persist(self) -> None:
        self._custom_path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "tag": p.tag,
                "avatar": p.avatar,
                "prompt": p.prompt,
            }
            for p in self._custom.values()
        ]
        self._custom_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 渲染 ──
    def render(self, pid: str, template: str, *, agent_name: str, user_name: str) -> str:
        """fill 模板变量；persona 不存在或模板为空返回空串。"""
        if self.get(pid) is None or not template:
            return ""
        return template.replace("{{userName}}", user_name).replace("{{agentName}}", agent_name)


def _persona_data_dir() -> Path:
    """<app_data>/assistant_hub（与 AssistantManager.root 一致的探测逻辑）。"""
    try:
        from app.utils.utils import get_app_data_dir

        return Path(get_app_data_dir()) / "assistant_hub"
    except Exception:
        pass
    try:
        import os

        appdata = os.getenv("APPDATA")
        if appdata:
            p = Path(appdata) / "DriFox" / "assistant_hub"
            if p.exists():
                return p
    except Exception:
        pass
    return Path.home() / ".drifox" / "assistant_hub"


def load_module_standalone(module_path: Path, name: str):
    """hook 独立加载入口：按文件路径加载本模块并缓存 sys.modules。"""
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(name, str(module_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# dataclass field 引用保持（避免 lint 误报未使用导入）
_ = field
