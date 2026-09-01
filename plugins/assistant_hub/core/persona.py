# -*- coding: utf-8 -*-
"""persona.py — 助手人格注册表（DriFox 特色预置 + 用户自定义）。

人格 = 三类资产合一（对齐 openhanako yuan 概念，但内容 DriFox 原创优先）：
- prompt（人格基底模板）：注入 system prompt 的人格段，支持 {{userName}}/{{agentName}}
- tag（思考块标签）：UI 方角小牌与思考块协议名（build=推演 / hanako=MOOD / 无=空）
- avatar：personas/<id>/avatar.png

存储（预设单独存文件夹：人格基底 / 身份 / 行为约束）：
- 内置：plugins/assistant_hub/personas/<id>/persona.md（基底）
        + <id>/identity.md（身份）+ <id>/agents.md（行为约束）+ <id>/avatar.png
  兼容旧平铺布局：personas/<id>.md + <id>.identity.md / <id>.agents.md + avatars/<id>.png
- 自定义：<app_data>/assistant_hub/personas.json（builtin 不可删改）
- "none"：恒存在的空人格（纯净助手，进 chips 行参与选择）
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger


@dataclass
class Persona:
    id: str
    name: str = ""
    description: str = ""
    tag: str = ""
    avatar: str = ""
    builtin: bool = False
    prompt: str = ""
    # 可选伴随模板：personas/<id>.identity.md / personas/<id>.agents.md
    # （设置页身份/AGENTS.md 回落链：落盘文件 → 人格专属模板 → 内置通用模板）
    identity_template: str = ""
    agents_template: str = ""


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
        # 用户级头像覆盖目录：换头像不写插件目录（内置人格只读，部署目录可能无写权限），
        # 存 <app_data>/assistant_hub/persona_avatars/<pid>.<ext>
        self._avatar_override_dir = self._custom_path.parent / "persona_avatars"
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
        """加载内置人格。

        新布局（自包含子文件夹，人格预设单独存档）：
            personas/<id>/persona.md    # 人格基底（frontmatter + 正文）
            personas/<id>/identity.md   # 身份简介伴随模板（可选）
            personas/<id>/agents.md     # 行为约束伴随模板（可选）
            personas/<id>/avatar.png    # 头像（可选）
        旧布局（兼容，子文件夹优先）：
            personas/<id>.md + personas/<id>.identity.md / <id>.agents.md
            personas/avatars/<id>.png
        """
        self._builtin = {}
        # 新布局：子文件夹 personas/<id>/persona.md
        for d in sorted(self._builtin_dir.iterdir()) if self._builtin_dir.exists() else []:
            if not d.is_dir():
                continue
            pmd = d / "persona.md"
            if pmd.exists():
                self._load_persona_md(pmd)
        # 旧布局：平铺 personas/*.md（已被子文件夹占用的 id 跳过）
        for md in sorted(self._builtin_dir.glob("*.md")):
            if ".identity." in md.name or ".agents." in md.name:
                continue  # 旧布局伴随模板在下方单独挂
            pid = self._peek_persona_id(md)
            if pid and pid in self._builtin:
                continue
            self._load_persona_md(md)
        # 伴随模板：子文件夹 identity.md / agents.md 优先，回落平铺 <id>.identity.md / <id>.agents.md
        for p in self._builtin.values():
            sub = self._builtin_dir / p.id
            ipath = sub / "identity.md" if (sub / "identity.md").exists() else self._builtin_dir / f"{p.id}.identity.md"
            apath = sub / "agents.md" if (sub / "agents.md").exists() else self._builtin_dir / f"{p.id}.agents.md"
            try:
                if ipath.exists():
                    p.identity_template = _parse_frontmatter(ipath.read_text(encoding="utf-8"))[1]
                if apath.exists():
                    p.agents_template = _parse_frontmatter(apath.read_text(encoding="utf-8"))[1]
            except Exception:
                continue

    def _peek_persona_id(self, md: Path) -> str:
        try:
            meta, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
            return meta.get("id") or md.stem
        except Exception:
            return md.stem

    def _load_persona_md(self, md: Path) -> None:
        try:
            meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
        except Exception:
            return
        # id：frontmatter 优先；子文件夹布局回落文件夹名，平铺布局回落文件名
        pid = meta.get("id") or (md.parent.name if md.parent == self._builtin_dir else md.stem) or md.stem
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
        """头像解析：用户级覆盖 → 显式 avatar 字段 → 子文件夹 avatar.png → avatars/<id>.png。"""
        p = self.get(pid)
        if p is None:
            return None
        # 用户级覆盖（换头像落这里，内置/自定义人格统一）
        if self._avatar_override_dir.exists():
            for ext in ("png", "jpg", "jpeg", "webp", "svg"):
                cand = self._avatar_override_dir / f"{pid}.{ext}"
                if cand.exists():
                    return cand
        if p.avatar:
            cand = Path(p.avatar)
            if cand.is_absolute() and cand.exists():
                return cand
            rel = self._builtin_dir / p.avatar
            if rel.exists():
                return rel
        # 新布局：personas/<id>/avatar.png
        sub = self._builtin_dir / pid / "avatar.png"
        if sub.exists():
            return sub
        # 旧布局：personas/avatars/<avatar 或 id>.png
        rel = self._builtin_dir / "avatars" / f"{pid}.png"
        return rel if rel.exists() else None

    def has_avatar_override(self, pid: str) -> bool:
        """该人格是否存在用户级头像覆盖（控制「恢复默认」按钮可用性）。"""
        if not self._avatar_override_dir.exists():
            return False
        return any(
            (self._avatar_override_dir / f"{pid}.{ext}").exists() for ext in ("png", "jpg", "webp", "svg")
        )

    def set_avatar(self, pid: str, data: bytes, ext: str) -> Optional[Path]:
        """写入用户级头像覆盖（同 pid 只保留一份，换扩展名时清旧文件）。"""
        ext = (ext or "png").lower().lstrip(".")
        if ext == "jpeg":
            ext = "jpg"
        if ext not in ("png", "jpg", "webp", "svg"):
            return None
        try:
            self._avatar_override_dir.mkdir(parents=True, exist_ok=True)
            for old_ext in ("png", "jpg", "webp", "svg"):
                if old_ext != ext:
                    old = self._avatar_override_dir / f"{pid}.{old_ext}"
                    if old.exists():
                        old.unlink()
            target = self._avatar_override_dir / f"{pid}.{ext}"
            target.write_bytes(data)
            return target
        except OSError as e:
            logger.warning(f"[assistant_hub] 写入人格头像失败 ({pid}): {e}")
            return None

    def clear_avatar(self, pid: str) -> bool:
        """删除用户级头像覆盖（回落人格默认头像）。"""
        if not self._avatar_override_dir.exists():
            return True
        removed = False
        for ext in ("png", "jpg", "webp", "svg"):
            f = self._avatar_override_dir / f"{pid}.{ext}"
            if f.exists():
                try:
                    f.unlink()
                    removed = True
                except OSError:
                    pass
        return removed or True

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
        self._custom_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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
