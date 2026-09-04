# -*- coding: utf-8 -*-
"""assistant_manager.py — 助手管理（参考 OpenHanako core/agent.ts / server/routes/agents.ts）

提供 AssistantManager 单例，负责：
1. 助手 CRUD（创建 / 列表 / 删除 / 主助手 / 排序 / 切换）
2. 助手元数据读写（assistant.yaml）
3. 人格注入（identity_and_persona：只拼 personas/<yuan>/persona.md 基底；
   人格由 PersonaRegistry 管理，新增人格走 persona-creator 技能）
4. 头像管理（avatars/assistant.{png,jpg,webp,svg}）
5. 置顶记忆（pinned.md，自动解析为 list[tuple[str, str]] = (title, content)）
6. 当下记忆（memory/today.md，对应当下/今日记忆）
7. 长期记忆（memory/longterm.md，Dream 整理结果）
8. Dream 自动整理（原子化 → 去重 → 优化 → 合成 → 验证；持久化修订）
9. 专属技能（skills/<name>.md 描述 + assistant.yaml 的 skills 白名单）

AssistantManager 与 AgentManager 解耦：
- 不修改 /plugins/system/agents/*.md
- 不修改 AgentManager 既有行为
- 通过 register_assistant_agent() 派生一个动态 agent（name = "assistant_<id>"，
  mode=subagent，仅含 assistant yaml 中的 skills 白名单）注入 AgentManager。
  调用 subagent_para(name="assistant_<id>") 即"调用助手"，被调用的助手看到
  的是 persona.md + pinned.md + today.md + longterm.md 拼出的 system prompt。

存储根目录：<app_data_dir>/assistant_hub/<assistant_id>/
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from loguru import logger


# 复用项目里 yaml 加载的兼容写法：safe_load + 失败兜底 {}
def _safe_yaml_load(text: str) -> Dict[str, Any]:
    try:
        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _yaml_dump(data: Dict[str, Any]) -> str:
    """统一 yaml 写出风格：utf-8，无 None 字段，allow_unicode 不转义中文"""
    cleaned = {k: v for k, v in data.items() if v is not None}
    return yaml.safe_dump(
        cleaned,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )


# ────────────────────────────────────────────────────────────────────
# 元数据 dataclass
# ────────────────────────────────────────────────────────────────────


@dataclass
class Assistant:
    """助手元数据（与磁盘 assistant.yaml 一一对应）

    字段取值遵循 openhanako 的最小可用集合，未设置时回落到 builtin default。
    """

    id: str
    name: str = ""
    yuan: str = "build"  # 体系（openhanako 概念：DriFox 这里映射为系统智能体名）
    color: str = "#7C3AED"  # 头像主色
    avatar_path: str = ""  # 相对 avatars/ 的文件路径，空表示内置色块
    primary: bool = False  # 是否主助手
    order: int = 0
    # 专属技能白名单：非空 = 仅启用名单内技能；空 = 全部启用（再减黑名单）
    skills_whitelist: List[str] = field(default_factory=list)
    # 专属技能黑名单：whitelist 为空时生效，名单内技能不注入
    skills_blacklist: List[str] = field(default_factory=list)
    # 专属技能（skills/*.md）总开关：关闭后不注入提示词，read_skill 工具暂停
    skills_enabled: bool = True
    # 是否启用 memory 体系（关闭后 today/longterm 都不注入）
    memory_enabled: bool = True
    # dream 自动整理：默认关闭（手动触发）
    dream_auto_enabled: bool = False
    # 模型覆盖：空表示继承 yuan
    model: str = ""
    temperature: Optional[float] = None
    steps: Optional[int] = None
    # 对外人格（控制其他 agent 调用本助手时看到的描述）
    public_description: str = ""
    # 经验体系（recall/record_experience 工具 + 每日反思）：默认关闭
    experience_enabled: bool = False
    # 项目笔记（AGENTS.md）注入开关：主智能体按此开关控制；子智能体始终注入
    project_notes_enabled: bool = True
    # 项目上下文（项目根目录 + git 状态）注入开关：主智能体按此开关控制；子智能体始终注入
    project_context_enabled: bool = True
    # 工具权限档位（对话前 schema 过滤）：full=不过滤 / readonly=仅安全类工具 / minimal=仅 bash
    tool_access: str = "full"
    # 记忆整理模型（llm_saved_providers 的 config_id）：空 = 跟随全局当前模型
    utility_model: str = ""
    # 对用户的称呼（{{userName}} 模板变量覆盖）：空 = 跟随系统用户名
    user_addressing: str = ""
    # 创建/更新时间戳
    created_at: str = ""
    updated_at: str = ""

    def to_yaml(self) -> str:
        d = {
            "id": self.id,
            "name": self.name,
            "yuan": self.yuan,
            "color": self.color,
            "avatar_path": self.avatar_path,
            "primary": self.primary,
            "order": self.order,
            "skills_whitelist": list(self.skills_whitelist),
            "skills_blacklist": list(self.skills_blacklist),
            "skills_enabled": self.skills_enabled,
            "memory_enabled": self.memory_enabled,
            "dream_auto_enabled": self.dream_auto_enabled,
            "model": self.model,
            "temperature": self.temperature,
            "steps": self.steps,
            "public_description": self.public_description,
            "experience_enabled": self.experience_enabled,
            "project_notes_enabled": self.project_notes_enabled,
            "project_context_enabled": self.project_context_enabled,
            "tool_access": self.tool_access,
            "utility_model": self.utility_model,
            "user_addressing": self.user_addressing,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return _yaml_dump(d)

    @classmethod
    def from_yaml(cls, text: str, default_id: str = "") -> "Assistant":
        data = _safe_yaml_load(text)
        if not isinstance(data, dict):
            data = {}
        a = cls(
            id=str(data.get("id") or default_id),
            name=str(data.get("name") or ""),
            yuan=str(data.get("yuan") or "build"),
            color=str(data.get("color") or "#7C3AED"),
            avatar_path=str(data.get("avatar_path") or ""),
            primary=bool(data.get("primary", False)),
            order=int(data.get("order", 0) or 0),
            skills_whitelist=list(data.get("skills_whitelist") or []),
            skills_blacklist=list(data.get("skills_blacklist") or []),
            skills_enabled=bool(data.get("skills_enabled", True)),
            memory_enabled=bool(data.get("memory_enabled", True)),
            dream_auto_enabled=bool(data.get("dream_auto_enabled", False)),
            model=str(data.get("model") or ""),
            temperature=data.get("temperature"),
            steps=data.get("steps"),
            public_description=str(data.get("public_description") or ""),
            experience_enabled=bool(data.get("experience_enabled", False)),
            project_notes_enabled=bool(data.get("project_notes_enabled", True)),
            project_context_enabled=bool(data.get("project_context_enabled", True)),
            tool_access=str(data.get("tool_access") or "full"),
            utility_model=str(data.get("utility_model") or ""),
            user_addressing=str(data.get("user_addressing") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )
        # 旧 yuan 值映射到 v2 persona id（kong→none）
        a.yuan = _MIGRATE_YUAN.get(a.yuan, a.yuan)
        return a

    @classmethod
    def new(cls, name: str, id: Optional[str] = None) -> "Assistant":
        """工厂：创建新助手（生成 id 与时间戳，颜色随机但稳定）"""
        aid = id or _generate_id(name)
        now = _now_iso()
        return cls(
            id=aid,
            name=name,
            yuan="build",
            color=_pick_stable_color(aid),
            avatar_path="",
            primary=False,
            order=0,
            skills_whitelist=[],
            skills_blacklist=[],
            skills_enabled=True,
            memory_enabled=True,
            dream_auto_enabled=False,
            model="",
            temperature=None,
            steps=None,
            public_description="",
            project_notes_enabled=True,
            project_context_enabled=True,
            created_at=now,
            updated_at=now,
        )


# ────────────────────────────────────────────────────────────────────
# 助手运行时上下文（推送至 dynamic agent 时用）
# ────────────────────────────────────────────────────────────────────


@dataclass
class AssistantContext:
    """助手运行时上下文（单实例，随当前活跃助手可变）"""

    pinned: List[Tuple[str, str]] = field(default_factory=list)  # (pin_id, content)
    today: str = ""
    longterm: str = ""

    def to_prompt_block(self, max_pins: int = 30, max_each_chars: int = 1200) -> str:
        """把上下文格式化为注入 system prompt 的 markdown 块（参考 openhanako compile.ts）"""
        parts: List[str] = []
        # 置顶
        if self.pinned:
            pin_lines = []
            for pid, content in self.pinned[:max_pins]:
                content = (content or "").strip()
                if not content:
                    continue
                if len(content) > max_each_chars:
                    content = content[:max_each_chars].rstrip() + "…"
                pin_lines.append(f"- {content}")
            if pin_lines:
                parts.append("## 置顶记忆\n\n" + "\n".join(pin_lines))
        # 当下
        if self.today.strip():
            parts.append("## 当下记忆\n\n" + self.today.strip()[:max_each_chars])
        # 长期
        if self.longterm.strip():
            parts.append("## 长期记忆\n\n" + self.longterm.strip()[:max_each_chars])
        return "\n\n".join(parts)


# ────────────────────────────────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    from datetime import datetime

    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


_ID_SAFE_RE = re.compile(r"[^a-z0-9_-]+")


def _generate_id(name: str) -> str:
    """由中文/英文名生成稳定 id（小写 + 短 hash 兜底重名）"""
    base = _ID_SAFE_RE.sub("-", (name or "").strip().lower()).strip("-")
    if not base:
        base = "assistant"
    short = uuid.uuid5(uuid.NAMESPACE_DNS, name or "assistant").hex[:6]
    return f"{base}-{short}"


def _pick_stable_color(seed: str) -> str:
    """稳定调色板：根据 seed 生成 16 色调色板之一"""
    palette = [
        "#7C3AED",  # violet
        "#DB2777",  # pink
        "#DC2626",  # red
        "#EA580C",  # orange
        "#D97706",  # amber
        "#CA8A04",  # yellow
        "#65A30D",  # lime
        "#16A34A",  # green
        "#059669",  # emerald
        "#0891B2",  # cyan
        "#0284C7",  # sky
        "#2563EB",  # blue
        "#4F46E5",  # indigo
        "#6D28D9",  # purple
        "#9333EA",  # fuchsia
        "#475569",  # slate
    ]
    h = sum(ord(c) for c in seed) if seed else 0
    return palette[h % len(palette)]


def _avatar_supported_exts() -> Tuple[str, ...]:
    return ("png", "jpg", "jpeg", "webp", "svg")


# 旧 yuan 值 → v2 persona id（kong→none；butter/ming 仍为合法内置人格，不迁移）
_MIGRATE_YUAN = {"kong": "none"}

# core 子包加载器缓存（模块名 assistant_hub_core.<key>）
_CORE_MODULES: Dict[str, Any] = {}


def _load_core_module(key: str, rel: str):
    """按文件路径加载 plugins/assistant_hub/core/ 下模块并缓存（hook/UI 共享实例语义）。

    mtime 自检：manager 本身随 assistant_hub_manager 模块热重建，但本 dict 缓存
    独立于 sys.modules——文件更新后按 mtime 重新加载，否则 core 改动不生效。
    """
    import importlib.util

    path = Path(__file__).resolve().parent / "core" / rel
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _CORE_MODULES.get(key)
    if cached is not None and getattr(cached, "_source_mtime", -1.0) >= mtime:
        return cached
    spec = importlib.util.spec_from_file_location(f"assistant_hub_core.{key}", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    _CORE_MODULES[key] = module
    sys_modules()[f"assistant_hub_core.{key}"] = module
    spec.loader.exec_module(module)
    module._source_mtime = mtime
    return module


def sys_modules():
    import sys

    return sys.modules


# ────────────────────────────────────────────────────────────────────
# 对外描述模板（AGENTS.public.md：其他 agent 调用本助手时看到的简介）
# ────────────────────────────────────────────────────────────────────


_BUILTIN_PUBLIC_TEMPLATE = """# {name}

{name} 是一个**通用 AI 助手**。可以聊天、写代码、改文档，并保留本次会话中产生的关键记忆。
"""


# ────────────────────────────────────────────────────────────────────
# AssistantManager
# ────────────────────────────────────────────────────────────────────


class AssistantManager:
    """助手管理器（全局单例）"""

    _instance: Optional["AssistantManager"] = None

    # 会话级临时助手：{session_id: assistant_id}（运行时态，重启清零；
    # 由 @提及触发，仅影响对应会话的 system prompt）
    _session_overrides: Dict[str, str] = {}

    # 会话归属映射：{session_id: assistant_id}（持久化 _session_map.json）；
    # 记忆传送带按它过滤素材，保证各助手记忆互相独立
    _session_map: Dict[str, str] = {}

    def __init__(self, root_dir: Optional[str] = None):
        self._root: Path = Path(root_dir) if root_dir else self._default_root()
        self._assistants: Dict[str, Assistant] = {}
        self._loaded = False
        # 唯一来源的运行时上下文（每个活跃助手一份）
        self._contexts: Dict[str, AssistantContext] = {}

    # ── 单例 ──

    @classmethod
    def get_instance(cls, root_dir: Optional[str] = None) -> "AssistantManager":
        if cls._instance is None:
            cls._instance = cls(root_dir=root_dir)
        if root_dir and Path(root_dir) != cls._instance._root:
            cls._instance._root = Path(root_dir)
            cls._instance._loaded = False
        if not cls._instance._loaded:
            cls._instance._load_all()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """测试/重载用：清空单例"""
        cls._instance = None
        cls._session_overrides = {}
        cls._session_map = {}

    # ── 根目录 ──

    def _default_root(self) -> Path:
        """默认根目录：<app_data_dir>/assistant_hub"""
        try:
            from app.utils.utils import get_app_data_dir

            app_data = get_app_data_dir()
        except Exception:
            app_data = None
        if not app_data:
            # 兜底：用户目录/.drifox/assistant_hub
            app_data = str(Path.home() / ".drifox")
        return Path(app_data) / "assistant_hub"

    @property
    def root(self) -> Path:
        return self._root

    # ── 文件 IO ──

    def _assistant_dir(self, aid: str) -> Path:
        return self._root / aid

    def _yaml_path(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "assistant.yaml"

    def _public_md_path(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "AGENTS.public.md"

    def _pinned_path(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "pinned.md"

    def _today_path(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "memory" / "today.md"

    def _longterm_path(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "memory" / "longterm.md"

    def _dream_history_path(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "memory" / "dream_history.json"

    def _skills_dir(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "skills"

    def _avatar_dir(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "avatars"

    def _ensure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    # ── 加载 ──

    def _load_all(self) -> None:
        """启动时从磁盘扫描全部助手"""
        self._ensure_dir(self._root)
        self._load_session_map()
        try:
            for entry in sorted(self._root.iterdir(), key=lambda p: p.name):
                if not entry.is_dir():
                    continue
                yaml_path = entry / "assistant.yaml"
                if not yaml_path.exists():
                    continue
                try:
                    text = yaml_path.read_text(encoding="utf-8")
                    a = Assistant.from_yaml(text, default_id=entry.name)
                    self._assistants[a.id] = a
                except Exception as e:
                    logger.warning(f"[assistant_hub] 加载 {entry.name} 失败: {e}")
        except FileNotFoundError:
            pass
        if not self._assistants:
            self._seed_defaults()
        self._converge_primary()
        self._loaded = True
        logger.info(f"[assistant_hub] 已加载 {len(self._assistants)} 个助手")

    def _converge_primary(self) -> None:
        """多主/无主收敛（读侧防御）。

        双主来源：热重载窗口期旧实例 update() 整体写回旧 primary 态。
        规则：唯一主 → 不动；多主/无主 → 保留稳定排序首位。结果写回磁盘。
        """
        if not self._assistants:
            return
        primaries = [a for a in self._assistants.values() if a.primary]
        if len(primaries) == 1:
            return
        stable = self.list_assistants_sorted_by_stable()
        keep = stable[0].id
        now = _now_iso()
        for k, v in self._assistants.items():
            new_primary = k == keep
            if v.primary != new_primary:
                v.primary = new_primary
                v.updated_at = now
                self._write_yaml(v)

    def list_assistants_sorted_by_stable(self) -> List[Assistant]:
        """稳定排序（不考虑 primary）：order 升序 → created_at 升序 → id。"""
        return sorted(self._assistants.values(), key=lambda a: (a.order, a.created_at, a.id))

    def _seed_defaults(self) -> None:
        """首次启动（库为空）预置 4 个助手：DriFox（主助手+默认激活）/ 花子 / 空 / 毒蛇妹。

        只在根目录完全为空时执行一次；用户删除后不会复活（目录已存在 yaml）。
        头像：从 personas/<id>/avatar.png 复制到助手 avatars/agent.png。
        毒蛇妹：默认关闭记忆（不沉淀跨会话上下文）、开启经验（保留经验库与反思）。
        """
        seeds = [
            ("build", "DriFox", "build", True, None, None),
            ("hanako", "花子", "hanako", False, None, None),
            ("pure", "空", "none", False, None, None),
            ("viper-mei", "毒蛇妹", "viper-mei", False, False, True),
        ]
        try:
            for name, display, yuan, primary, memory, experience in seeds:
                a = self.create(display, id=name, yuan=yuan)
                if memory is not None:
                    a.memory_enabled = bool(memory)
                if experience is not None:
                    a.experience_enabled = bool(experience)
                # 人格头像 → 助手头像
                try:
                    reg = self.persona_registry()
                    pap = reg.avatar_path(yuan)
                    if pap and pap.exists():
                        self.save_avatar_from_bytes(a.id, pap.read_bytes(), pap.suffix.lstrip("."))
                except Exception as e:
                    logger.debug(f"[assistant_hub] seed 头像复制失败 ({name}): {e}")
                if primary:
                    a.primary = True
                self.update(a)
            logger.info("[assistant_hub] 已预置默认助手: build / hanako / pure / viper-mei")
        except Exception as e:
            logger.warning(f"[assistant_hub] 预置助手失败: {e}")

    # ── CRUD ──

    def list_assistants(self) -> List[Assistant]:
        """列出全部助手（按 order 升序，主助手居首）"""
        items = list(self._assistants.values())
        items.sort(key=lambda a: (not a.primary, a.order, a.created_at))
        return items

    def get(self, aid: str) -> Optional[Assistant]:
        return self._assistants.get(aid)

    def has(self, aid: str) -> bool:
        return aid in self._assistants

    def create(self, name: str, id: Optional[str] = None, yuan: str = "build") -> Assistant:
        """创建新助手并落盘（人格走 PersonaRegistry，不落盘身份/提示词文件）"""
        a = Assistant.new(name=name, id=id)
        a.yuan = yuan or a.yuan
        self._assistants[a.id] = a
        self._ensure_dir(self._assistant_dir(a.id))
        # 写 yaml
        self._write_yaml(a)
        ppath = self._public_md_path(a.id)
        if not ppath.exists():
            ppath.write_text(
                _BUILTIN_PUBLIC_TEMPLATE.format(name=a.name or a.id),
                encoding="utf-8",
            )
        self._ensure_dir(self._avatar_dir(a.id))
        self._ensure_dir(self._skills_dir(a.id))
        logger.info(f"[assistant_hub] 创建助手: {a.id} ({a.name})")
        return a

    def delete(self, aid: str) -> bool:
        """删除助手（连同目录），不存在或唯一助手时返回 False"""
        if aid not in self._assistants:
            return False
        if self._assistants[aid].primary or len(self._assistants) <= 1:
            return False  # 主助手与唯一助手不可删除
        path = self._assistant_dir(aid)
        self._assistants.pop(aid, None)
        self._contexts.pop(aid, None)
        try:
            if path.exists():
                shutil.rmtree(path)
        except Exception as e:
            logger.warning(f"[assistant_hub] 删除 {path} 失败: {e}")
        logger.info(f"[assistant_hub] 删除助手: {aid}")
        return True

    def reload_from_disk(self) -> None:
        """重新从盘加载助手元数据。

        热重载窗口期 UI 与工具可能各持一个实例（旧卡片持旧实例写内存+盘，
        工具拿新实例读旧内存）→ 判定前重读盘消除分歧。update() 均即时落盘，
        重读不会丢失已确认状态。
        """
        self._load_all()

    def update(self, a: Assistant) -> None:
        """更新助手元数据落盘。

        写侧互斥：热重载/多实例窗口期，其他实例的内存态可能过期
        （曾把旧主助手整体写回造成双主），这里以 a 为准收敛——
        a.primary=True 时清掉其余助手的主标记（内存+盘）。
        """
        a.updated_at = _now_iso()
        self._assistants[a.id] = a
        self._write_yaml(a)
        if a.primary:
            now = a.updated_at
            for k, v in self._assistants.items():
                if k != a.id and v.primary:
                    v.primary = False
                    v.updated_at = now
                    self._write_yaml(v)

    def set_primary(self, aid: str) -> bool:
        """设置主助手（互斥）。

        主助手即不 @ 时的默认身份：切换后清空 system prompt 缓存，
        所有会话下一条消息生效。
        """
        if aid not in self._assistants:
            return False
        changed = not self._assistants[aid].primary
        now = _now_iso()
        for k, v in self._assistants.items():
            new_primary = k == aid
            if v.primary != new_primary:
                v.primary = new_primary
                v.updated_at = now
                self._write_yaml(v)
        if changed:
            self._invalidate_session_prompt_caches()
        return True

    def save_order(self, ordered_ids: List[str]) -> bool:
        """保存助手排序"""
        ok = True
        for idx, aid in enumerate(ordered_ids):
            a = self._assistants.get(aid)
            if not a:
                continue
            a.order = idx
            self._write_yaml(a)
        return ok

    def _write_yaml(self, a: Assistant) -> None:
        path = self._yaml_path(a.id)
        self._ensure_dir(path.parent)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(a.to_yaml(), encoding="utf-8")
        tmp.replace(path)

    # ── ID 校验 ──

    @staticmethod
    def validate_id(aid: str) -> bool:
        if not aid or len(aid) > 64:
            return False
        return bool(re.match(r"^[a-z0-9][a-z0-9_\-]*$", aid.lower()))

    # ── 当前助手（= 主助手，不 @ 时的默认身份）──

    @classmethod
    def active_id(cls) -> str:
        """当前生效助手 id（= 主助手；@提及的会话级临时切换走 session override）。"""
        inst = cls.get_instance()
        return next((a.id for a in inst._assistants.values() if a.primary), next(iter(inst._assistants), ""))

    # ── 会话级临时助手（@提及触发，仅影响单个 session 的 system prompt）──

    @classmethod
    def set_session_override(cls, session_id: str, aid: str) -> bool:
        """设置某会话的临时助手（aid 为空串 = 清除 override，跟随全局主助手）。

        仅影响 session_id 对应会话的 BuildSystemPrompt 注入，不改变全局
        主助手。返回是否发生变化。
        """
        if not session_id:
            return False
        if aid:
            mgr = cls.get_instance()
            if not mgr.has(aid):
                return False
        overrides = cls.get_instance()._session_overrides
        if overrides.get(session_id, "") == aid:
            return False
        if aid:
            overrides[session_id] = aid
        else:
            overrides.pop(session_id, None)
        # 同步归属映射（提前落盘，不等轮次结束）
        if aid:
            cls.record_session_aid(session_id, aid)
        return True

    @classmethod
    def record_session_aid(cls, session_id: str, aid: str) -> None:
        """记录会话归属（sid 当前使用的助手）：记忆素材过滤的数据源。"""
        if not session_id or not aid:
            return
        inst = cls.get_instance()
        if cls._session_map.get(session_id) == aid:
            return
        cls._session_map[session_id] = aid
        # 防膨胀：超上限裁掉最旧的一半（dict 保插入序）
        if len(cls._session_map) > 1000:
            for k in list(cls._session_map.keys())[:500]:
                cls._session_map.pop(k, None)
        inst._save_session_map()

    @classmethod
    def resolve_session_aid(cls, session_id: str) -> str:
        """会话当前归属助手；映射无记录/助手已删回落主助手（兼容旧数据）。"""
        fallback = cls.active_id()
        if not session_id:
            return fallback
        aid = cls._session_map.get(session_id, "")
        if aid and cls.get_instance().has(aid):
            return aid
        return fallback

    def _session_map_path(self) -> Path:
        return self._root / "_session_map.json"

    def _save_session_map(self) -> None:
        try:
            self._ensure_dir(self._root)
            self._session_map_path().write_text(json.dumps(self._session_map, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[assistant_hub] 保存 session map 失败: {e}")

    def _load_session_map(self) -> None:
        try:
            data = json.loads(self._session_map_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                type(self)._session_map = {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass

    @classmethod
    def get_session_override(cls, session_id: str) -> str:
        """读会话级临时助手；无 override 或助手已删除返回空串（跟随全局）。"""
        if not session_id:
            return ""
        aid = cls.get_instance()._session_overrides.get(session_id, "")
        if aid and not cls.get_instance().has(aid):
            cls.get_instance()._session_overrides.pop(session_id, None)
            return ""
        return aid

    # ── 工具权限档位（对话前 schema 过滤）──

    _TOOL_ACCESS_MODES = ("full", "readonly", "minimal")

    @classmethod
    def tool_access_for(cls, session_id: str) -> str:
        """会话生效的工具权限档位：会话 override 助手优先，否则主助手；异常兜底 full。"""
        try:
            aid = cls.get_session_override(session_id) if session_id else ""
            if not aid:
                aid = cls.active_id()
            a = cls.get_instance().get(aid)
            mode = str(getattr(a, "tool_access", "") or "full")
            return mode if mode in cls._TOOL_ACCESS_MODES else "full"
        except Exception:
            return "full"

    @staticmethod
    def _invalidate_session_prompt_caches() -> None:
        """清空所有窗口所有 session 的 system_prompt 缓存。

        遍历 ChatBackend 活跃实例的 session manager，把每个 session 的
        system_prompt 置空，下次 build_messages 强制重建（重新触发
        BuildSystemPrompt hooks → 新助手身份注入）。
        """
        try:
            from app.core.backend import ChatBackend

            for backend in list(ChatBackend._active_instances):
                sm = getattr(backend, "session_manager", None)
                if sm is None:
                    continue
                sessions = getattr(sm, "sessions", None)
                if isinstance(sessions, dict):
                    items = sessions.values()
                else:
                    try:
                        items = sm.get_all_sessions() or []
                    except Exception:
                        items = []
                for session in items:
                    try:
                        session.system_prompt = ""
                        if hasattr(session, "_system_prompt_agent"):
                            session._system_prompt_agent = ""
                    except Exception:
                        pass
            # 兜底：ChatBackend._active_instances 可能未暴露，走 engine 级缓存
            try:
                from app.core.engines.ui.engine import ChatEngine

                for engine in list(getattr(ChatEngine, "_instances", []) or []):
                    try:
                        engine._invalidate_session_system_prompt_cache()
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception as e:
            from loguru import logger

            logger.debug(f"[assistant_hub] 清空 system prompt 缓存失败: {e}")

    @staticmethod
    def _invalidate_session_prompt(session_id: str) -> None:
        """只清空指定 session 的 system_prompt 缓存（会话级临时助手切换用）。

        下次 build_messages 强制重建该会话的 system prompt，重新触发
        BuildSystemPrompt hooks → 按会话 override 注入新助手身份。
        其他会话缓存不动，避免全局闪烁。
        """
        if not session_id:
            return
        try:
            from app.core.backend import ChatBackend

            for backend in list(ChatBackend._active_instances):
                sm = getattr(backend, "session_manager", None)
                if sm is None:
                    continue
                # SessionManager.sessions 是 List[ChatSession]（历史版曾为 dict，
                # 两种形态都兼容），按 session_id 匹配
                sessions = getattr(sm, "sessions", None)
                if isinstance(sessions, dict):
                    candidates = [sessions.get(session_id)]
                elif isinstance(sessions, (list, tuple)):
                    candidates = [s for s in sessions if getattr(s, "session_id", "") == session_id]
                else:
                    continue
                for session in candidates:
                    if session is None:
                        continue
                    try:
                        session.system_prompt = ""
                        if hasattr(session, "_system_prompt_agent"):
                            session._system_prompt_agent = ""
                    except Exception:
                        pass
        except Exception as e:
            from loguru import logger

            logger.debug(f"[assistant_hub] 清空会话 {session_id} prompt 缓存失败: {e}")

    # ── 对外描述 (AGENTS.public.md：其他 agent 调用本助手时看到的简介) ──

    def read_public_md(self, aid: str) -> str:
        path = self._public_md_path(aid)
        if path.exists():
            return path.read_text(encoding="utf-8")
        a = self.get(aid)
        name = a.name if a else aid
        return _BUILTIN_PUBLIC_TEMPLATE.format(name=name)

    def write_public_md(self, aid: str, content: str) -> bool:
        path = self._public_md_path(aid)
        self._ensure_dir(path.parent)
        path.write_text(content, encoding="utf-8")
        return True

    # ── 头像 ──

    def avatar_files(self, aid: str) -> List[Path]:
        d = self._avatar_dir(aid)
        if not d.exists():
            return []
        result: List[Path] = []
        for ext in _avatar_supported_exts():
            p = d / f"agent.{ext}"
            if p.exists():
                result.append(p)
        return result

    def avatar_path(self, aid: str) -> Optional[Path]:
        files = self.avatar_files(aid)
        return files[0] if files else None

    def assistant_avatar_path(self, aid: str) -> Optional[Path]:
        """助手头像显示链：元人格头像 → 助手自有头像（存量回落）→ None。

        头像归属人格：人格头像（含用户级覆盖）优先，改一处全助手跟随；
        助手自有 agent.png（含预置种子头像）仅在该人格无头像时兜底显示。
        """
        a = self.get(aid)
        if a and a.yuan:
            try:
                p = self.persona_registry().avatar_path(a.yuan)
                if p:
                    return p
            except Exception:
                pass
        return self.avatar_path(aid)

    def has_own_avatar(self, aid: str) -> bool:
        """助手是否有自有头像（区别于人格回落）。"""
        return self.avatar_path(aid) is not None

    def save_avatar_from_bytes(self, aid: str, data: bytes, ext: str) -> Optional[Path]:
        ext = ext.lower().lstrip(".")
        if ext not in _avatar_supported_exts():
            return None
        d = self._avatar_dir(aid)
        self._ensure_dir(d)
        for old_ext in _avatar_supported_exts():
            old = d / f"agent.{old_ext}"
            if old.exists():
                try:
                    old.unlink()
                except OSError:
                    pass
        target = d / f"agent.{ext}"
        target.write_bytes(data)
        return target

    def clear_avatar(self, aid: str) -> bool:
        d = self._avatar_dir(aid)
        if not d.exists():
            return True
        for old_ext in _avatar_supported_exts():
            old = d / f"agent.{old_ext}"
            if old.exists():
                try:
                    old.unlink()
                except OSError:
                    pass
        return True

    # ── 置顶记忆 ──

    @staticmethod
    def _parse_pinned(text: str) -> List[Tuple[str, str]]:
        """解析 pinned.md 为 list[(pin_id, content)]

        格式：每条以 ``- `` 或 ``* `` 开头；末尾可隐含 ``<!-- pin:<id> -->``
        注释；缺省 id 则按行号生成稳定 ID。
        """
        items: List[Tuple[str, str]] = []
        for idx, raw in enumerate(text.splitlines()):
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            if not (stripped.startswith("- ") or stripped.startswith("* ")):
                continue
            content = stripped[2:].strip()
            pin_id = f"pin-{idx}"
            m = re.search(r"<!--\s*pin:([a-zA-Z0-9_\-]+)\s*-->", content)
            if m:
                pin_id = m.group(1)
                content = re.sub(r"<!--\s*pin:[a-zA-Z0-9_\-]+\s*-->", "", content).strip()
            items.append((pin_id, content))
        return items

    @staticmethod
    def _dump_pinned(items: List[Tuple[str, str]]) -> str:
        lines = ["# 置顶记忆", ""]
        lines.append("本文件的内容会始终注入到助手的 system prompt，永不衰减或被 Dream 覆盖。")
        lines.append("可以直接编辑保存；在 UI 中也可以一条条增删。")
        lines.append("")
        for pid, content in items:
            content = (content or "").strip()
            if not content:
                continue
            lines.append(f"- {content} <!-- pin:{pid} -->")
        return "\n".join(lines) + "\n"

    def read_pinned(self, aid: str) -> List[Tuple[str, str]]:
        path = self._pinned_path(aid)
        if not path.exists():
            return []
        try:
            return self._parse_pinned(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[assistant_hub] 解析 pinned.md 失败 ({aid}): {e}")
            return []

    def write_pinned(self, aid: str, items: List[Tuple[str, str]]) -> bool:
        path = self._pinned_path(aid)
        self._ensure_dir(path.parent)
        path.write_text(self._dump_pinned(items), encoding="utf-8")
        return True

    # ── 当下 / 长期记忆 ──

    def read_today(self, aid: str) -> str:
        path = self._today_path(aid)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def write_today(self, aid: str, content: str) -> bool:
        path = self._today_path(aid)
        self._ensure_dir(path.parent)
        path.write_text(content, encoding="utf-8")
        return True

    def read_longterm(self, aid: str) -> str:
        path = self._longterm_path(aid)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def write_longterm(self, aid: str, content: str) -> bool:
        path = self._longterm_path(aid)
        self._ensure_dir(path.parent)
        path.write_text(content, encoding="utf-8")
        return True

    # ── Dream 自动整理 ──

    def dream_history(self, aid: str) -> List[Dict[str, Any]]:
        path = self._dream_history_path(aid)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _append_dream_history(self, aid: str, entry: Dict[str, Any]) -> None:
        history = self.dream_history(aid)
        history.insert(0, entry)
        history = history[:50]  # 最多保留 50 条
        path = self._dream_history_path(aid)
        self._ensure_dir(path.parent)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_dream(
        self,
        aid: str,
        trigger: str = "manual",
        on_progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Dream 自动整理（参考 openhanako lib/memory/dream/runner.ts）

        本地无 LLM 调度器（避免与项目既有 LLM 调度冲突）。Dream 步骤：
        1. **读取（gather）**: 合并 today + longterm → 原始文本
        2. **原子化（atomize）**: 按段落 / 行拆为原子单元
        3. **去重（dedupe）**: 保留首条完全相同的文本，去掉后续重复
        4. **优化（optimize）**: 去掉空白/空行，标准化标点
        5. **合成（compose）**: 排序后写入 longterm.md，并写一条 revision
        6. **验证（verify）**: 检查生成结果可被再读回

        Args:
            aid: 助手 id
            trigger: 触发来源（"manual" / "auto"），保留在历史中
            on_progress: 可选回调 fn(stage: str, payload: dict)
        """
        if not self.has(aid):
            return {"ok": False, "error": "assistant not found"}
        ts = _now_iso()
        run_id = f"dream-{int(time.time() * 1000)}"

        def _notify(stage: str, **payload: Any) -> None:
            if on_progress:
                try:
                    on_progress(stage, payload)
                except Exception as e:  # 回调失败不影响主流程
                    logger.warning(f"[assistant_hub.dream] 回调失败 ({stage}): {e}")

        _notify("start", run_id=run_id, trigger=trigger)

        today = self.read_today(aid)
        longterm = self.read_longterm(aid)
        merged = "\n".join([s for s in (today, longterm) if s and s.strip()]).strip()
        _notify("gather", today_chars=len(today), longterm_chars=len(longterm))

        if not merged:
            entry = {
                "run_id": run_id,
                "ts": ts,
                "trigger": trigger,
                "stages": {"gather": 0, "atomize": 0, "dedupe": 0, "optimize": 0, "compose": 0},
                "result": "empty",
            }
            self._append_dream_history(aid, entry)
            _notify("done", run_id=run_id, units=0)
            return {"ok": True, "run_id": run_id, "units": 0, "result": "empty"}

        # 1) atomize: 行级
        units: List[str] = []
        for line in merged.splitlines():
            s = line.strip()
            if not s:
                continue
            # 跳过 markdown 标题 / 注释行
            if s.startswith("#"):
                continue
            units.append(s)
        _notify("atomize", units=len(units))

        # 2) dedupe: 完全相同只保留首
        seen = set()
        deduped: List[str] = []
        for u in units:
            norm = re.sub(r"\s+", "", u).lower()
            if norm in seen:
                continue
            seen.add(norm)
            deduped.append(u)
        _notify("dedupe", units=len(deduped))

        # 3) optimize: 合并相邻空行 / 修整标点
        optimized: List[str] = []
        for s in deduped:
            s = re.sub(r"\s{2,}", " ", s).strip()
            s = re.sub(r"^[•·]+\s*", "- ", s)
            if not s.endswith((".", "!", "?", "。", "！", "？", "；")):
                pass
            optimized.append(s)
        _notify("optimize", units=len(optimized))

        # 4) compose
        header = f"# 长期记忆（Dream {run_id} · {ts}）\n\n"
        body = "\n".join(f"- {s}" for s in optimized)
        new_longterm = header + body + "\n"
        old_longterm = longterm
        self.write_longterm(aid, new_longterm)
        _notify("compose", units=len(optimized))

        # 5) verify: 再读一次，确保一致
        verify_text = self.read_longterm(aid)
        ok = verify_text == new_longterm
        _notify("verify", ok=ok)

        entry = {
            "run_id": run_id,
            "ts": ts,
            "trigger": trigger,
            "stages": {
                "gather": len(merged),
                "atomize": len(units),
                "dedupe": len(deduped),
                "optimize": len(optimized),
                "compose": len(new_longterm),
            },
            "result": "ok" if ok else "verify_mismatch",
            "prev_longterm_chars": len(old_longterm),
            "new_longterm_chars": len(new_longterm),
        }
        self._append_dream_history(aid, entry)
        _notify("done", run_id=run_id, units=len(optimized))
        return {"ok": ok, "run_id": run_id, "units": len(optimized)}

    # ── 专属技能 ──

    def enabled_skills(self, aid: str) -> List[Dict[str, Any]]:
        """过滤后的启用技能：总开关关→空；whitelist 非空→仅白名单；空→全部减 blacklist。"""
        a = self.get(aid)
        if a is None or not a.skills_enabled:
            return []
        skills = self.list_skills(aid)
        if a.skills_whitelist:
            wl = set(a.skills_whitelist)
            return [s for s in skills if s["name"] in wl]
        bl = set(a.skills_blacklist)
        return [s for s in skills if s["name"] not in bl]

    def list_skills(self, aid: str) -> List[Dict[str, Any]]:
        """列出助手专属技能（每条 {name, path, description, content_chars}）"""
        d = self._skills_dir(aid)
        if not d.exists():
            return []
        out: List[Dict[str, Any]] = []
        for p in sorted(d.glob("*.md")):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                text = ""
            # 取第一段 # xxx 作为 description
            desc = ""
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("#"):
                    desc = line.lstrip("#").strip()
                    break
            out.append(
                {
                    "name": p.stem,
                    "path": str(p),
                    "description": desc,
                    "content_chars": len(text),
                }
            )
        return out

    def read_skill(self, aid: str, name: str) -> str:
        p = self._skills_dir(aid) / f"{name}.md"
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    def write_skill(self, aid: str, name: str, content: str) -> str:
        """写入技能，返回规范化文件名（不含 .md）；失败返回空串。"""
        safe = re.sub(r"[^a-zA-Z0-9_\-]+", "-", name).strip("-").lower()
        if not safe:
            return ""
        d = self._skills_dir(aid)
        self._ensure_dir(d)
        (d / f"{safe}.md").write_text(content, encoding="utf-8")
        return safe

    def delete_skill(self, aid: str, name: str) -> bool:
        safe = re.sub(r"[^a-zA-Z0-9_\-]+", "-", name).strip("-").lower()
        if not safe:
            return False
        p = self._skills_dir(aid) / f"{safe}.md"
        try:
            if p.exists():
                p.unlink()
            return True
        except OSError:
            return False

    # ── 上下文快照 ──

    def get_context(self, aid: str) -> AssistantContext:
        """读取助手当下 context（缓存；任何写入会自动 invalidate）"""
        if aid in self._contexts:
            return self._contexts[aid]
        ctx = AssistantContext(
            pinned=self.read_pinned(aid),
            today=self.read_today(aid),
            longterm=self.read_longterm(aid),
        )
        self._contexts[aid] = ctx
        return ctx

    def invalidate_context(self, aid: str) -> None:
        self._contexts.pop(aid, None)

    # ── 公开元数据（不带敏感字段）──

    def public_metadata(self, a: Assistant) -> Dict[str, Any]:
        return {
            "id": a.id,
            "name": a.name,
            "yuan": a.yuan,
            "color": a.color,
            "avatar_path": a.avatar_path,
            "primary": a.primary,
            "memory_enabled": a.memory_enabled,
            "dream_auto_enabled": a.dream_auto_enabled,
            "experience_enabled": a.experience_enabled,
            "model": a.model,
            "public_description": a.public_description,
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }

    # ════════════════════════════════════════════════════════════════
    #  v2 门面：人格 / 记忆传送带 / Dream / 经验（转发 core 子包）
    # ════════════════════════════════════════════════════════════════

    # ── core 模块懒加载 ──

    def _core_persona(self):
        return _load_core_module("persona", "persona.py")

    def _core_llm(self):
        return _load_core_module("llm_client", "llm_client.py")

    def _core_compile(self):
        return _load_core_module("memory.compile", "memory/compile.py")

    def _core_dream(self):
        return _load_core_module("memory.dream", "memory/dream.py")

    def _core_experience(self):
        return _load_core_module("experience", "experience.py")

    def _core_ticker(self):
        return _load_core_module("memory.ticker", "memory/ticker.py")

    def _utility_llm(self, aid: str):
        """返回绑定了该助手记忆整理模型的 chat_once（走主对话引擎，单回合）。

        utility_model 复合键格式（对齐 cron-tasks）："&lt;config_id&gt;||&lt;model_name&gt;"；
        兼容旧纯 config_id。解析失败/配置不存在 → None override = 回退全局当前模型。

        调用链：chat_once → services["create_engine_session"] → 主对话引擎，
        并用本插件的 single_turn 循环策略钳制成一回合（见 core/llm_client.py）。
        """
        a = self.get(aid)
        model_key = (a.utility_model if a else "") or ""
        chat_once = self._core_llm().chat_once
        override = self._resolve_model_override(model_key)

        def _call(messages, **kwargs):
            if override is not None:
                return chat_once(messages, model_config=override, **kwargs)
            return chat_once(messages, **kwargs)

        return _call

    def _resolve_model_override(self, model_key: str) -> Optional[Dict[str, Any]]:
        """解析 "<config_id>||<model_name>" 复合键为完整配置覆盖。

        未指定/配置缺失 → None（回退当前全局模型）。对齐 cron-tasks scheduler。
        数据源：main_widget._valid_configs（双源兜底，同 ui/sections.py）。
        """
        if not model_key:
            return None
        config_id, _, model_name = str(model_key).partition("||")
        mw = None
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            reg = UIPluginRegistry.get_instance()
            mw = getattr(reg, "_main_widget", None) or next(
                iter(getattr(reg, "_window_main_widgets", {}).values()), None
            )
        except Exception:
            mw = None
        valid = getattr(mw, "_valid_configs", None) if mw is not None else None
        if not isinstance(valid, dict):
            # 兜底 llm_saved_providers（无窗口场景）
            try:
                from app.utils.config import Settings

                saved = Settings.get_instance().llm_saved_providers.value or {}
            except Exception:
                return None
            cfg = saved.get(config_id)
            if not isinstance(cfg, dict) or not cfg:
                return None
            override = dict(cfg)
            if model_name:
                override["模型名称"] = model_name
            return override
        cfg = valid.get(config_id)
        if not isinstance(cfg, dict) or not cfg:
            return None
        override = dict(cfg)
        if model_name:
            override["模型名称"] = model_name
        return override

    # ── 通用 ──

    def user_name(self) -> str:
        return self._core_persona().resolve_user_name()

    def persona_registry(self):
        return self._core_persona().PersonaRegistry.get_instance()

    def assistant_dir(self, aid: str) -> Path:
        return self._assistant_dir(aid)

    def memory_dir(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "memory"

    def experience_dir(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "experience"

    def experience_index_path(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "experience.md"

    def dream_dir(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "memory" / "dream"

    # ── 人格段组装 ──

    def identity_and_persona(self, aid: str) -> str:
        """人格段：只拼 personas/<yuan>/persona.md 基底，fill 模板变量。

        persona="none" 时不注入（纯净助手）；人格不可编辑，新增人格走
        persona-creator 技能写入 personas/<id>/persona.md 热生效。
        """
        a = self.get(aid)
        if a is None:
            return ""
        persona_mod = self._core_persona()
        reg = persona_mod.PersonaRegistry.get_instance()
        # 对用户的称呼：助手自定义优先，空回落系统用户名
        user = (a.user_addressing or "").strip() or self.user_name()
        agent_name = a.name or a.id
        persona = reg.get(a.yuan)
        if persona is None or not persona.prompt.strip():
            return ""
        return reg.render(a.yuan, persona.prompt.strip(), agent_name=agent_name, user_name=user)

    # ── 记忆传送带 ──

    def compiled_memory(self, aid: str) -> str:
        """assemble 产物 memory.md（缺失/空返回 ""）。"""
        return self._core_compile().assemble(self._assistant_dir(aid))

    def compile_chain(self, aid: str, *, light: bool = False, require_new: bool = False) -> Dict[str, Any]:
        """跑编译链（ticker/手动入口）。

        light=True：只 compile_today + assemble（每 10 轮轻量链）
        light=False：完整日批（daily→today→roll→facts→assemble）
        require_new=True：今日无新增轮次时跳过 facts 等后续 LLM 步骤
        （日批遍历多助手时避免对无活动助手白烧调用）。

        素材过滤：compile_today 按会话归属映射（_session_map）只取
        本助手名下会话，保证各助手记忆互相独立；映射无记录的旧会话
        归主助手（与升级前行为一致）。

        LLM 不可用时各步静默降级（返回各步状态）。
        """
        aid_dir = self._assistant_dir(aid)
        cm = self._core_compile()
        result: Dict[str, Any] = {"ok": True, "steps": {}}
        try:
            llm = self._utility_llm(aid)
        except Exception as e:
            return {"ok": False, "error": f"llm_unavailable: {e}"}
        try:
            active = self.active_id()
            session_map = dict(type(self)._session_map)

            def _filter(s: dict) -> bool:
                return session_map.get(s.get("session_id", ""), active) == aid

            if not light:
                # 日批顺序铁律：先蒸馏昨日草稿，再增量编译今日
                prev_today = (
                    (aid_dir / "memory" / "today.md").read_text(encoding="utf-8")
                    if (aid_dir / "memory" / "today.md").exists()
                    else ""
                )
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                result["steps"]["compile_daily"] = cm.compile_daily(aid_dir, prev_today, yesterday, llm=llm)
            result["steps"]["compile_today"] = cm.compile_today(aid_dir, llm=llm, _session_filter=_filter)
            if not light:
                result["steps"]["roll_daily_window"] = {"folded": cm.roll_daily_window(aid_dir)}
                if require_new and not result["steps"]["compile_today"].get("changed"):
                    # 今日无新增：跳过 facts（LLM 成本），assemble 收尾
                    text = cm.assemble(aid_dir)
                    result["steps"]["assemble"] = {"chars": len(text)}
                    if not text:
                        result["memory_empty"] = True
                    self.invalidate_context(aid)
                    return result
                result["steps"]["compile_facts"] = cm.compile_facts(aid_dir, llm=llm)
            text = cm.assemble(aid_dir)
            result["steps"]["assemble"] = {"chars": len(text)}
            if not text:
                result["memory_empty"] = True
            # 同步失效旧 context 缓存
            self.invalidate_context(aid)
        except Exception as e:
            logger.warning(f"[assistant_hub] 编译链异常: {e}")
            result["ok"] = False
            result["error"] = str(e)
        return result

    # ── Dream ──

    def dream_runner(self, aid: str, progress=None):
        return self._core_dream().DreamRunner(self._assistant_dir(aid), llm=self._utility_llm(aid), progress=progress)

    def dream_start(self, aid: str, trigger: str = "manual", progress=None) -> Dict[str, Any]:
        return self.dream_runner(aid, progress=progress).start(trigger)

    def dream_status(self, aid: str) -> Dict[str, Any]:
        return self.dream_runner(aid).status()

    def dream_revisions(self, aid: str) -> List[Dict[str, Any]]:
        return self.dream_runner(aid).list_revisions()

    def dream_restore(self, aid: str, revision_id: str) -> Dict[str, Any]:
        return self.dream_runner(aid).restore_revision(revision_id)

    def dream_start_auto_if_eligible(self, aid: str, logical_date: str) -> Optional[Dict[str, Any]]:
        return self.dream_runner(aid).start_automatic_if_eligible(logical_date)

    # ── 经验 ──

    def experience_record(self, aid: str, category: str, content: str) -> Dict[str, Any]:
        r = self._core_experience().record_entry(self._assistant_dir(aid), category, content)
        self.invalidate_context(aid)
        return r

    def experience_list(self, aid: str) -> List[Dict[str, Any]]:
        return self._core_experience().list_documents(self._assistant_dir(aid))

    def experience_read(self, aid: str, category: str) -> str:
        return self._core_experience().read_document(self._assistant_dir(aid), category)

    def experience_read_index(self, aid: str) -> str:
        return self._core_experience().read_index(self._assistant_dir(aid))

    def experience_delete(self, aid: str, category: str, index: int) -> Dict[str, Any]:
        return self._core_experience().delete_entry(self._assistant_dir(aid), category, index)

    def experience_reflect(self, aid: str) -> Dict[str, Any]:
        """经验反思：从 memory.md 提炼工作心得，随后自动压缩超限分类（需 LLM；LLM 不可用静默返回）。"""
        try:
            llm = self._utility_llm(aid)
        except Exception as e:
            return {"added": 0, "items": [], "error": f"llm_unavailable: {e}"}
        r = self._core_experience().reflect(
            self._assistant_dir(aid),
            identity_and_persona=self.identity_and_persona(aid),
            memory_md=self.compiled_memory(aid),
            llm=llm,
        )
        r["consolidated"] = self._experience_auto_consolidate(aid, llm)
        return r

    def _experience_auto_consolidate(self, aid: str, llm) -> List[Dict[str, Any]]:
        """水位检查：全库条目超阈值时自动压缩一次（无人管理，反思收尾时跑）。"""
        try:
            if not self._core_experience().needs_consolidate(self._assistant_dir(aid)):
                return []
        except Exception:
            return []
        try:
            r = self._core_experience().consolidate(self._assistant_dir(aid), llm=llm)
        except Exception as e:
            return [{"error": str(e)}]
        if r.get("changed"):
            self.invalidate_context(aid)
        return [r]

    def experience_consolidate(self, aid: str) -> Dict[str, Any]:
        """手动全库压缩（跨分类语义合并去重 + 重新归类）。"""
        try:
            llm = self._utility_llm(aid)
        except Exception as e:
            return {"changed": False, "error": f"llm_unavailable: {e}"}
        r = self._core_experience().consolidate(self._assistant_dir(aid), llm=llm)
        if r.get("changed"):
            self.invalidate_context(aid)
        return r
