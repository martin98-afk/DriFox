# -*- coding: utf-8 -*-
"""assistant_manager.py — 助手管理（参考 OpenHanako core/agent.ts / server/routes/agents.ts）

提供 AssistantManager 单例，负责：
1. 助手 CRUD（创建 / 列表 / 删除 / 主助手 / 排序 / 切换）
2. 助手元数据读写（assistant.yaml）
3. 人格注入（identity_and_persona：只拼 personas/<yuan>/persona.md 基底；
   人格由 PersonaRegistry 管理，新增人格走 persona-creator 技能）
4. 头像管理（avatars/assistant.{png,jpg,webp,svg}）
5. 人工提示（pinned.md，SessionStart 注入会话消息）
6. 记忆体系（照抄 WorkBuddy 双层结构 · 每助手独立）：
   - 全局记忆  <aid_dir>/MEMORY.md（跨项目）
   - 项目记忆  {workdir}/.drifox/memory/<aid>/（MEMORY.md + YYYY-MM-DD.md 日志）
   - 注入走 BuildSystemPrompt（记忆说明书 + 全局 + 项目 + 日志索引）
   - 写入走 memory_read / memory_write 专用工具（模型自主写，无后台管线）

AssistantManager 与 AgentManager 解耦：
- 不修改 /plugins/system/agents/*.md
- 不修改 AgentManager 既有行为
- 通过 register_assistant_agent() 派生一个动态 agent（name = "assistant_<id>"，
  mode=subagent）注入 AgentManager。

存储根目录：<app_data_dir>/assistant_hub/<assistant_id>/
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from loguru import logger


# 复用项目里 yaml 加载的兼容写法：safe_load + 失败兜底 {}
def _caps(cap_id):
    """取某能力（无 ctx 场景统一走 sdk 的 caps_for，host 由宿主解析）。"""
    from app.plugins.sdk import caps_for

    return caps_for(cap_id)


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
    # 是否启用记忆体系（关闭后记忆不注入、memory_write 暂停）
    memory_enabled: bool = True
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
    # 预置工具档位（控制初始注入的 schema，不影响可用性）：full=全量 / readonly=仅安全类 / minimal=bash+write+edit / search=仅搜索+中转 / none=无工具
    tool_access: str = "full"
    # 记忆字符上限（UI 可配；<=0 时回落内置默认）
    global_memory_max_chars: int = 4000
    project_memory_max_chars: int = 3000
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
            "memory_enabled": self.memory_enabled,
            "model": self.model,
            "temperature": self.temperature,
            "steps": self.steps,
            "public_description": self.public_description,
            "experience_enabled": self.experience_enabled,
            "project_notes_enabled": self.project_notes_enabled,
            "project_context_enabled": self.project_context_enabled,
            "tool_access": self.tool_access,
            "global_memory_max_chars": self.global_memory_max_chars,
            "project_memory_max_chars": self.project_memory_max_chars,
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
            memory_enabled=bool(data.get("memory_enabled", True)),
            model=str(data.get("model") or ""),
            temperature=data.get("temperature"),
            steps=data.get("steps"),
            public_description=str(data.get("public_description") or ""),
            experience_enabled=bool(data.get("experience_enabled", False)),
            project_notes_enabled=bool(data.get("project_notes_enabled", True)),
            project_context_enabled=bool(data.get("project_context_enabled", True)),
            tool_access=str(data.get("tool_access") or "full"),
            global_memory_max_chars=int(data.get("global_memory_max_chars", 4000) or 0),
            project_memory_max_chars=int(data.get("project_memory_max_chars", 3000) or 0),
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
            memory_enabled=True,
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
    # 直接写 sys.modules：模块名前缀 assistant_hub_core. 已在 plugin.json 的
    # module_prefixes 声明（AST 安全网按声明放行 + 热重载按声明 purge）。
    # 早先这里用 sys_modules() 包一层以绕开静态扫描 —— 属规避手法，已纠正。
    sys.modules[f"assistant_hub_core.{key}"] = module
    spec.loader.exec_module(module)
    module._source_mtime = mtime
    return module


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

    # 会话级临时助手：{session_id: assistant_id}（持久化 _session_overrides.json，
    # 重启后恢复；由 @提及触发，仅影响对应会话的注入身份与开关判断）
    _session_overrides: Dict[str, str] = {}

    # 会话归属映射：{session_id: assistant_id}（持久化 _session_map.json）；
    # memory_read/memory_write 工具按它解析记忆归属，保证各助手记忆互相独立
    _session_map: Dict[str, str] = {}

    def __init__(self, root_dir: Optional[str] = None):
        self._root: Path = Path(root_dir) if root_dir else self._default_root()
        self._assistants: Dict[str, Assistant] = {}
        self._loaded = False

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
            from app.plugins.sdk import get_app_data_dir

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

    def _avatar_dir(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "avatars"

    def _ensure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    # ── 加载 ──

    def _load_all(self) -> None:
        """启动时从磁盘扫描全部助手"""
        self._ensure_dir(self._root)
        self._load_session_map()
        self._load_session_overrides()
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
            ("viper", "毒蛇妹", "viper", False, False, True),
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
            logger.info("[assistant_hub] 已预置默认助手: build / hanako / pure / viper")
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
            self._invalidate_identity_cache()
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
        # Windows 下 Defender/索引服务可能瞬时持有目标句柄，replace 报 WinError 5，
        # 短退避重试吸收（20/40/80/160ms，总窗口约 300ms），穷尽才抛
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (2**attempt))

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
        # 防膨胀：超上限裁掉最旧的一半（dict 保插入序，对齐 _session_map）
        if len(overrides) > 1000:
            for k in list(overrides.keys())[:500]:
                overrides.pop(k, None)
        # 持久化：重启后恢复，SessionStart 记忆注入也能按临时助手开关判断
        cls.get_instance()._save_session_overrides()
        # 同步归属映射（提前落盘，不等轮次结束）
        if aid:
            cls.record_session_aid(session_id, aid)
        # 身份行跟随：消息身份解析有会话级缓存，override 变化必须失效，
        # 否则新消息仍显示旧助手（2026-09-22 身份行不更新根因）
        cls._invalidate_identity_cache()
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
        inst = cls.get_instance()
        aid = inst._session_overrides.get(session_id, "")
        if aid and not inst.has(aid):
            inst._session_overrides.pop(session_id, None)
            inst._save_session_overrides()
            return ""
        return aid

    def _session_overrides_path(self) -> Path:
        return self._root / "_session_overrides.json"

    def _save_session_overrides(self) -> None:
        try:
            self._ensure_dir(self._root)
            self._session_overrides_path().write_text(
                json.dumps(self._session_overrides, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.debug(f"[assistant_hub] 保存 session overrides 失败: {e}")

    def _load_session_overrides(self) -> None:
        try:
            data = json.loads(self._session_overrides_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                type(self)._session_overrides = {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass

    # ── 预置工具档位（初始注入 schema，hooks 与 UI 统计共用归属逻辑）──

    _TOOL_ACCESS_MODES = ("full", "readonly", "minimal", "search", "none")

    @classmethod
    def tool_access_for(cls, session_id: str) -> str:
        """会话生效的预置工具档位：会话 override 助手优先，否则主助手；异常兜底 full。"""
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

        经能力池 session.invalidate：一次语义动作覆盖 ChatBackend 活跃实例与
        ChatEngine 兜底两条路径（插件不再摸私有类属性）。
        """
        invalidate = _caps("session.invalidate")
        if invalidate is None:
            logger.warning("[assistant_hub] 无 session.invalidate 能力，跳过缓存失效")
            return
        try:
            invalidate()
        except Exception as e:
            logger.warning(f"[assistant_hub] 失效 system_prompt 缓存失败: {e}")

    def _invalidate_session_prompt(session_id: str) -> None:
        """只清空指定 session 的 system_prompt 缓存（会话级临时助手切换用）。

        经能力池 session.invalidate(session_id)：一次语义动作，插件不再遍历
        ChatBackend._active_instances 摸私有类属性。
        """
        if not session_id:
            return
        invalidate = _caps("session.invalidate")
        if invalidate is None:
            logger.warning("[assistant_hub] 无 session.invalidate 能力，跳过缓存失效")
            return
        try:
            invalidate(session_id)
        except Exception as e:
            logger.warning(f"[assistant_hub] 失效会话 system_prompt 失败: {e}")

    @staticmethod
    def _invalidate_identity_cache() -> None:
        """清空主程序消息身份解析缓存（主助手切换 / 会话 override 变化时）。

        ``message_identity.resolve_identity`` 按 (session_id, role) 缓存解析结果，
        助手身份变化后不清缓存，新消息的身份行（名字+头像）仍解析出旧助手，
        直到新建会话（新 session_id 换缓存 key）才恢复——2026-09-22 三个症状
        （切主助手头像不更新 / @临时助手身份行不跟随）的共同根因。
        解析成本极低，整表清空即可。
        """
        try:
            from app.plugins.sdk import clear_cache

            clear_cache()
        except Exception as e:
            logger.debug(f"[assistant_hub] 清空身份缓存失败: {e}")

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

    # ── 用户头像（该助手视角下「你」的头像，注入消息身份行显示）──

    def user_avatar_path(self, aid: str) -> Optional[Path]:
        """用户头像：avatars/user.{ext}，未设置返回 None（渲染层回落色块+首字母）。"""
        d = self._avatar_dir(aid)
        if not d.exists():
            return None
        for ext in _avatar_supported_exts():
            p = d / f"user.{ext}"
            if p.exists():
                return p
        return None

    def save_user_avatar_from_bytes(self, aid: str, data: bytes, ext: str) -> Optional[Path]:
        """保存用户头像（预置库复制与本地上传统一走 bytes 落盘，不引用源路径）。"""
        ext = ext.lower().lstrip(".")
        if ext not in _avatar_supported_exts():
            return None
        d = self._avatar_dir(aid)
        self._ensure_dir(d)
        for old_ext in _avatar_supported_exts():
            old = d / f"user.{old_ext}"
            if old.exists():
                try:
                    old.unlink()
                except OSError:
                    pass
        target = d / f"user.{ext}"
        target.write_bytes(data)
        return target

    def clear_user_avatar(self, aid: str) -> bool:
        d = self._avatar_dir(aid)
        if not d.exists():
            return True
        for old_ext in _avatar_supported_exts():
            old = d / f"user.{old_ext}"
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
        lines = ["# 人工提示", ""]
        lines.append("本文件的内容会在会话启动时注入，永不衰减。")
        lines.append("可以直接编辑保存；在 UI 中也可以一条条增删。")
        lines.append("")
        for pid, content in items:
            content = (content or "").strip()
            if not content:
                continue
            lines.append(f"- {content} <!-- pin:{pid} -->")
        return "\n".join(lines) + "\n"

    # ── 人工提示（pinned.md）──

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
            "experience_enabled": a.experience_enabled,
            "model": a.model,
            "public_description": a.public_description,
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }

    # ════════════════════════════════════════════════════════════════
    #  v2 门面：人格 / 记忆（双层 store）/ 经验（转发 core 子包）
    # ════════════════════════════════════════════════════════════════

    # ── core 模块懒加载 ──

    def _core_persona(self):
        return _load_core_module("persona", "persona.py")

    def _core_llm(self):
        return _load_core_module("llm_client", "llm_client.py")

    def _core_store(self):
        return _load_core_module("memory.store", "memory/store.py")

    def _core_prompts(self):
        return _load_core_module("memory.prompts", "memory/prompts.py")

    def _core_experience(self):
        return _load_core_module("experience", "experience.py")

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
            # 无窗口场景：走 model.config 能力（从主程序内存态取，
            # 不读磁盘 app.config —— 密钥模式下那里是密文/空串）
            get_config = _caps("model.config")
            if get_config is None:
                return None
            try:
                cfg = get_config(config_id, model_name)
            except Exception:
                return None
            if not isinstance(cfg, dict) or not cfg:
                return None
            return dict(cfg)
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

    def experience_dir(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "experience"

    def experience_index_path(self, aid: str) -> Path:
        return self._assistant_dir(aid) / "experience.md"

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

    # ── 注入块（BuildSystemPrompt / SessionStart hook 与 UI 统计共用，单一数据源）──

    def identity_block(self, aid: str) -> str:
        """人格块（BuildSystemPrompt hook 注入系统提示词）。

        仅含助手身份与人格段；记忆说明书与记忆内容由 memory_system_block
        追加（同 hook 内拼接），人工提示由 SessionStart hook 以会话消息注入。
        无人格段时返回空串（保持原行为）。
        """
        a = self.get(aid)
        if a is None:
            return ""
        persona_block = self.identity_and_persona(aid)
        if not persona_block.strip():
            return ""
        header = f"# 助手：{a.name or a.id}\n\n你是 {a.name or a.id}——一个由用户创建的专属 AI 助手。"
        return header + "\n\n" + persona_block.strip()

    def pinned_block(self, aid: str) -> str:
        """人工提示块（SessionStart hook 注入会话消息，role=user 的 hook 消息）。

        人工添加的明确要求，不受 memory_enabled 控制，始终注入。
        """
        a = self.get(aid)
        if a is None:
            return ""
        pinned = self.read_pinned(aid)
        pin_lines = [f"- {(c or '').strip()}" for _pid, c in pinned if (c or "").strip()]
        if not pin_lines:
            return ""
        return "# 人工提示\n\n以下是用户人工添加的明确要求，直接遵守即可。\n\n" + "\n".join(pin_lines)

    def memory_system_block(self, aid: str, workdir: str = "") -> str:
        """记忆体系块（BuildSystemPrompt hook 注入系统提示词）。

        照抄 WorkBuddy 注入结构：说明书 + <global_memory> + <project_memory> +
        <daily_logs> 索引 + 超期蒸馏 ACTION。memory_enabled=False 返回空串；
        记忆超限时截断并附 ACTION 指令逼模型先整理。
        """
        a = self.get(aid)
        if a is None or not a.memory_enabled:
            return ""
        store = self._core_store()
        prompts = self._core_prompts()
        aid_dir = self._assistant_dir(aid)

        global_file = str(store.global_memory_path(aid_dir))
        proj_dir = (
            str(store.project_dir(workdir, aid))
            if workdir
            else "{项目根}/" + store.PROJECT_DIR_NAME + "/" + store.MEMORY_SUBDIR + "/" + aid
        )
        limits = self.memory_max_chars(aid)
        sections = store.load_sections(aid_dir)
        parts: list[str] = [prompts.build_memory_system(global_file, proj_dir, limits, sections)]

        # 全局记忆
        g_text = (store.read_global_memory(aid_dir) or "").strip()
        if g_text:
            if len(g_text) > limits["global"]:
                g_text = (
                    g_text[: limits["global"]]
                    + "\n... (全局记忆超限，已截断)\n\n**ACTION REQUIRED**: 全局记忆已超上限。先整理 "
                    + global_file
                    + "：合并去重、删过时内容、保持精简，然后继续用户的任务。"
                )
            parts.append(
                "<global_memory>\n以下是跨项目的全局记忆（用户对助手长期稳定的要求）。\n\n"
                + g_text
                + "\n</global_memory>"
            )

        # 项目记忆
        p_text = (store.read_project_memory(workdir, aid) or "").strip() if workdir else ""
        if p_text:
            p_file = str(store.project_memory_path(workdir, aid))
            if len(p_text) > limits["project"]:
                p_text = (
                    p_text[: limits["project"]]
                    + "\n... (项目记忆超限，已截断)\n\n**ACTION REQUIRED**: 项目记忆已超上限。先整理 "
                    + p_file
                    + "：合并去重、删过时内容、保持精简，然后继续用户的任务。"
                )
            parts.append("<project_memory>\n以下是本项目的工作记忆。\n\n" + p_text + "\n</project_memory>")

        # 近期日志索引 + 超期蒸馏提示
        if workdir:
            index = store.daily_index_text(workdir, aid)
            if index:
                parts.append("<daily_logs>\n" + index + "\n</daily_logs>")
            stale = store.stale_daily(workdir, aid)
            if stale:
                parts.append(
                    "**ACTION REQUIRED**: 以下旧日志已超过 30 天且未蒸馏，请按记忆说明书蒸馏流程处理"
                    "（校对→并入项目 MEMORY.md→严选全局→打标 mark_distilled）：" + "、".join(stale[:10])
                )

        return "\n\n".join(parts)

    def prompt_stats(self, aid: str) -> Dict[str, int]:
        """注入块统计：chars=人格+人工提示+记忆体系块字符数；tokens_est≈1.6 字符/token。"""
        block = ""
        try:
            parts = [self.identity_block(aid), self.pinned_block(aid), self.memory_system_block(aid)]
            block = "\n\n".join(p for p in parts if p)
        except Exception:
            pass
        chars = len(block)
        return {"chars": chars, "tokens_est": int(chars / 1.6)}

    # ── 记忆读写门面（hook / 工具 / UI 共用）──

    def memory_max_chars(self, aid: str = "") -> Dict[str, int]:
        """记忆文件字符上限：助手配置优先（<=0 回落内置默认）。"""
        store = self._core_store()
        a = self.get(aid) if aid else None
        g = int(getattr(a, "global_memory_max_chars", 0) or 0) if a is not None else 0
        p = int(getattr(a, "project_memory_max_chars", 0) or 0) if a is not None else 0
        return {
            "global": g if g > 0 else store.GLOBAL_MEMORY_MAX_CHARS,
            "project": p if p > 0 else store.PROJECT_MEMORY_MAX_CHARS,
        }

    def memory_paths(self, aid: str, workdir: str = "") -> Dict[str, str]:
        """该助手的记忆路径集合（UI 展示用）。"""
        store = self._core_store()
        aid_dir = self._assistant_dir(aid)
        out = {"global": str(store.global_memory_path(aid_dir))}
        if workdir:
            out["project_dir"] = str(store.project_dir(workdir, aid))
            out["project"] = str(store.project_memory_path(workdir, aid))
        return out

    def read_global_memory(self, aid: str) -> str:
        return self._core_store().read_global_memory(self._assistant_dir(aid))

    def write_global_memory(self, aid: str, content: str) -> bool:
        return self._core_store().write_global_memory(self._assistant_dir(aid), content)

    def read_project_memory(self, aid: str, workdir: str) -> str:
        return self._core_store().read_project_memory(workdir, aid)

    def write_project_memory(self, aid: str, workdir: str, content: str) -> bool:
        return self._core_store().write_project_memory(workdir, aid, content)

    def memory_fingerprint(self, aid: str, kind: str, workdir: str = "") -> str:
        """记忆文件版本指纹（memory_read 返回、memory_write base 校验用）。"""
        store = self._core_store()
        if kind == "global":
            return store.memory_fingerprint(store.global_memory_path(self._assistant_dir(aid)))
        if kind == "project" and workdir:
            return store.memory_fingerprint(store.project_memory_path(workdir, aid))
        return ""

    def append_memory(self, aid: str, kind: str, text: str, workdir: str = "", section: str = "") -> Dict[str, Any]:
        """向全局/项目 MEMORY.md 追加一块（不覆盖已有行）。

        section 仅对 global 有意义：插入该分区块末尾（块不存在则新建）。
        """
        store = self._core_store()
        if kind == "global":
            return store.append_memory_file(store.global_memory_path(self._assistant_dir(aid)), text, section)
        if kind == "project" and workdir:
            return store.append_memory_file(store.project_memory_path(workdir, aid), text)
        return {"ok": False, "error": "bad_kind"}

    # ── 全局记忆分区（用户可编辑 schema）──

    def memory_sections(self, aid: str) -> List[Dict[str, str]]:
        """读分区定义；文件缺失时落盘默认分区（首次打开即为可用骨架）。

        用户手动清空全部区（文件仍在）返回 []＝自由格式，不会被重新塞回默认值。
        """
        return self._core_store().ensure_default_sections(self._assistant_dir(aid))

    def set_memory_sections(self, aid: str, sections: List[Dict[str, str]]) -> Dict[str, Any]:
        """写分区定义；改名时同步改写 MEMORY.md 里的旧标题（避免制造孤儿）。

        改名识别：按列表位置对比新旧（UI 编辑只改 name/hint，不重排）。
        删除分区**不**动文件：孤儿标题保留，下次蒸馏重分配（用户决策 2026-10-25）。
        """
        store = self._core_store()
        aid_dir = self._assistant_dir(aid)
        old = store.load_sections(aid_dir)
        r = store.save_sections(aid_dir, sections)
        if not r.get("ok"):
            return r
        new = store.load_sections(aid_dir)
        g_path = store.global_memory_path(aid_dir)
        text = store.read_text(g_path)
        renamed = []
        if text.strip():
            for i, old_item in enumerate(old):
                if i >= len(new):
                    break
                old_name, new_name = old_item.get("name"), new.get(i, {}).get("name")
                if old_name and new_name and old_name != new_name:
                    text = store.rename_section_title(text, str(old_name), str(new_name))
                    renamed.append(f"{old_name}→{new_name}")
            if renamed:
                store.write_global_memory(aid_dir, text)
        return {"ok": True, "count": len(new), "renamed": renamed}

    def memory_sections_block(self, aid: str) -> str:
        """分区清单 prompt 文本（蒸馏/说明书共用）；无分区返回空串。"""
        return self._core_store().sections_prompt_text(self.memory_sections(aid))

    def default_memory_sections(self) -> List[Dict[str, str]]:
        """默认分区（UI 恢复默认用的唯一定义源）。"""
        return self._core_store().default_sections()


    def validate_global_memory(self, aid: str, content: str) -> Dict[str, Any]:
        """校验候选全局记忆是否符合分区 schema（空 schema 恒通过）。"""
        return self._core_store().validate_global_sections(content, self.memory_sections(aid))

    def write_memory_guarded(
        self,
        aid: str,
        kind: str,
        content: str,
        base: str = "",
        consolidate: bool = False,
        snapshot=None,
        workdir: str = "",
    ) -> Dict[str, Any]:
        """带护栏的整体替换（乐观锁 + 收缩确认 + 锁内快照）。"""
        store = self._core_store()
        if kind == "global":
            return store.write_memory_guarded(
                store.global_memory_path(self._assistant_dir(aid)), content, base, consolidate, snapshot
            )
        if kind == "project" and workdir:
            return store.write_memory_guarded(
                store.project_memory_path(workdir, aid), content, base, consolidate, snapshot
            )
        return {"ok": False, "error": "bad_kind"}

    def append_daily(
        self, aid: str, workdir: str, text: str, date: str = "", level: str = "INFO", summary: str = ""
    ) -> Dict[str, Any]:
        return self._core_store().append_daily(workdir, aid, text, date, level, summary)

    def list_daily(self, aid: str, workdir: str, limit: int = 7) -> List[Dict[str, Any]]:
        return self._core_store().list_daily(workdir, aid, limit)

    def read_daily(self, aid: str, workdir: str, date: str = "") -> str:
        return self._core_store().read_daily(workdir, aid, date)

    def read_daily_paged(
        self, aid: str, workdir: str, date: str = "", page: int = 1, per_page: int = 20
    ) -> Dict[str, Any]:
        return self._core_store().read_daily_paged(workdir, aid, date, page, per_page)

    def query_daily(
        self,
        aid: str,
        workdir: str,
        from_date: str = "",
        to_date: str = "",
        levels: Optional[List[str]] = None,
        search: str = "",
        summary_only: bool = False,
        page: int = 1,
        per_page: int = 20,
    ) -> Dict[str, Any]:
        return self._core_store().query_daily(
            workdir, aid, from_date, to_date, levels, search, summary_only, page, per_page
        )

    def daily_exists(self, aid: str, workdir: str, date: str = "") -> bool:
        return self._core_store().daily_exists(workdir, aid, date)

    def undistilled_daily_before(self, aid: str, workdir: str, before: str = "") -> List[str]:
        return self._core_store().undistilled_before(workdir, aid, before)

    def mark_daily_distilled(self, aid: str, workdir: str, date: str) -> Dict[str, Any]:
        return self._core_store().mark_distilled(workdir, aid, date)

    def distill_gate_block(self, aid: str, workdir: str) -> str:
        """新日首写门禁的蒸馏指令；无未蒸馏旧日志返回空串（工具层据此放行）。"""
        store = self._core_store()
        prompts = self._core_prompts()
        dates = store.undistilled_before(workdir, aid)
        if not dates:
            return ""
        return prompts.build_distill_gate(
            dates,
            str(store.project_memory_path(workdir, aid)),
            str(store.global_memory_path(self._assistant_dir(aid))),
            self.memory_sections(aid),
        )

    # ── 快照 / 健康（用户侧恢复与状态，模型工具面不含）──

    def snapshot_memory(self, aid: str, kind: str, workdir: str = "") -> Dict[str, Any]:
        """覆盖写前快照：global 取全局 MEMORY.md；project 取当前 workdir 的 MEMORY.md。"""
        store = self._core_store()
        a_dir = self._assistant_dir(aid)
        src = store.global_memory_path(a_dir) if kind == "global" else store.project_memory_path(workdir, aid)
        return store.snapshot_memory(a_dir, kind, src, workdir)

    def memory_snapshots(self, aid: str, kind: str, workdir: str = "") -> List[Dict[str, Any]]:
        return self._core_store().list_snapshots(self._assistant_dir(aid), kind, workdir)

    def read_memory_snapshot(self, aid: str, kind: str, name: str, workdir: str = "") -> str:
        return self._core_store().read_snapshot(self._assistant_dir(aid), kind, name, workdir)

    def restore_memory_snapshot(self, aid: str, kind: str, name: str, workdir: str = "") -> Dict[str, Any]:
        store = self._core_store()
        a_dir = self._assistant_dir(aid)
        target = store.global_memory_path(a_dir) if kind == "global" else store.project_memory_path(workdir, aid)
        return store.restore_snapshot(a_dir, kind, name, target, workdir)

    def memory_health(self, aid: str, workdir: str = "") -> Dict[str, Any]:
        """健康聚合：未蒸馏天数 / 超限 / 上次蒸馏 / 快照数 / 漂移（UI 状态行 + 恢复弹窗数据源）。"""
        store = self._core_store()
        a_dir = self._assistant_dir(aid)
        lims = self.memory_max_chars(aid)
        g_text = store.read_global_memory(a_dir).strip()
        p_text = store.read_project_memory(workdir, aid).strip() if workdir else ""
        stale_days = 0
        last_distill = ""
        if workdir:
            pdir = store.project_dir(workdir, aid)
            if pdir.exists():
                undistilled = store.undistilled_before(workdir, aid)
                if undistilled:
                    d0 = datetime.strptime(undistilled[0], "%Y-%m-%d").date()
                    stale_days = (datetime.now().date() - d0).days
                newest = ""
                for f in pdir.glob("*.md"):
                    parsed = store.parse_daily_stem(f.stem)
                    if parsed and parsed[1] and parsed[0] > newest:
                        newest = parsed[0]
                last_distill = newest
        return {
            "stale_days": stale_days,
            "over_limit": {
                "global": len(g_text) > lims["global"],
                "project": bool(workdir) and len(p_text) > lims["project"],
            },
            "usage": {"global": [len(g_text), lims["global"]], "project": [len(p_text), lims["project"]]},
            "last_distill": last_distill,
            "snapshot_counts": {
                "global": len(store.list_snapshots(a_dir, "global")),
                "project": len(store.list_snapshots(a_dir, "project", workdir)) if workdir else 0,
            },
            "drift": store.detect_workdir_drift(a_dir, workdir) if workdir else {"drifted": False},
        }

    def migrate_project_memory(self, aid: str, old_workdir: str, new_workdir: str) -> Dict[str, Any]:
        return self._core_store().migrate_project_memory(self._assistant_dir(aid), old_workdir, new_workdir)

    def dismiss_workdir_drift(self, aid: str, old_workdir: str) -> None:
        self._core_store().dismiss_drift(self._assistant_dir(aid), old_workdir)

    def clear_all_memory(self, aid: str, workdir: str = "") -> None:
        """清除该助手全部记忆（全局 MEMORY.md + 项目记忆目录）；清除前强制快照（可恢复）；人工提示保留。"""
        store = self._core_store()
        a_dir = self._assistant_dir(aid)
        store.snapshot_memory(a_dir, "global", store.global_memory_path(a_dir))
        if workdir:
            store.snapshot_memory(a_dir, "project", store.project_memory_path(workdir, aid), workdir)
        store.clear_memory(a_dir, workdir)

    # ── 经验 ──

    def experience_record(self, aid: str, category: str, content: str) -> Dict[str, Any]:
        return self._core_experience().record_entry(self._assistant_dir(aid), category, content)

    def experience_list(self, aid: str) -> List[Dict[str, Any]]:
        return self._core_experience().list_documents(self._assistant_dir(aid))

    def experience_read(self, aid: str, category: str) -> str:
        return self._core_experience().read_document(self._assistant_dir(aid), category)

    def experience_read_index(self, aid: str) -> str:
        return self._core_experience().read_index(self._assistant_dir(aid))

    def experience_delete(self, aid: str, category: str, index: int) -> Dict[str, Any]:
        return self._core_experience().delete_entry(self._assistant_dir(aid), category, index)

    def experience_reflect(self, aid: str) -> Dict[str, Any]:
        """经验反思：从近期日志/项目记忆提炼工作心得，随后自动压缩超限分类（需 LLM）。

        数据源改版（拆传送带后）：项目 MEMORY.md + 近期日志拼接，取代原编译产物 memory.md。
        """
        try:
            llm = self._utility_llm(aid)
        except Exception as e:
            return {"added": 0, "items": [], "error": f"llm_unavailable: {e}"}
        r = self._core_experience().reflect(
            self._assistant_dir(aid),
            identity_and_persona=self.identity_and_persona(aid),
            memory_md=self._recent_memory_text(aid),
            llm=llm,
        )
        r["consolidated"] = self._experience_auto_consolidate(aid, llm)
        return r

    def _recent_memory_text(self, aid: str) -> str:
        """反思素材：项目 MEMORY.md + 最近日志（无 workdir 时取全局记忆兑底）。"""
        parts: list[str] = []
        wd = self._current_workdir()
        if wd:
            pm = self.read_project_memory(aid, wd).strip()
            if pm:
                parts.append("# 项目记忆\n" + pm)
            for it in self.list_daily(aid, wd, limit=3):
                body = self.read_daily(aid, wd, it["date"]).strip()
                if body:
                    parts.append(f"# {it['date']}\n{body}")
        if not parts:
            g = self.read_global_memory(aid).strip()
            if g:
                parts.append("# 全局记忆\n" + g)
        return "\n\n".join(parts)

    @staticmethod
    def _current_workdir(pool=None) -> str:
        """当前应使用的项目工作目录（UI 查看/反思/统计等无 tool_ctx 场景用）。

        pool（宿主 ctx 能力池）优先：宿主已按卡片形态解析窗口并裁剪授权
        （dock 卡=归属窗口，全局卡=当前选中 tab），插件只消费语义快照，
        全程不持有窗口对象。None（后台线程）时回退全局链路：caps_for
        （授权仍按 manifest 裁剪）→ UIPluginRegistry 注册表。

        历史 bug（0.4.0）：manifest api_version=2 但 capabilities 漏声明
        window.current → caps_for 恒 None → 每次落到注册表 fallback 取
        「第一个注册窗口」的 workdir，多窗口下必然串味。
        """
        if pool is not None and "window.current" in pool:
            try:
                snap = pool["window.current"]
                snap = snap() if callable(snap) else snap
                if snap and snap.get("workdir"):
                    return str(snap["workdir"])
            except Exception as e:
                logger.debug(f"[assistant_hub] 取能力池窗口快照失败，回退全局链: {e}")
        try:
            # 无池（后台线程等）：全局链路（授权仍按 manifest 声明裁剪）。
            # 卡片路径已由 _ctx_caps_pool() 实时刷新宿主 note_host。
            snap = _caps("window.current")
            if snap is not None:
                snap = snap()
            if snap and snap.get("workdir"):
                return str(snap["workdir"])
        except Exception as e:
            logger.debug(f"[assistant_hub] 全局链取窗口快照失败: {e}")
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            reg = UIPluginRegistry.get_instance()
            mw = getattr(reg, "_main_widget", None) or next(
                iter(getattr(reg, "_window_main_widgets", {}).values()), None
            )
            if mw is not None and getattr(mw, "backend", None) is not None:
                return str(mw.backend.tool_executor.get_workdir() or "")
        except Exception:
            pass
        return ""

    def _experience_auto_consolidate(self, aid: str, llm) -> List[Dict[str, Any]]:
        """水位检查：全库条目超阈值时自动压缩一次（无人管理，反思收尾时跑）。

        冷却熔断：上次失败 30 分钟内跳过（防失败任务每次反思重烧双倍超时）；
        retries=0（失败有冷却兜底，重试只翻倍烧时间）。手动压缩不受限。
        """
        try:
            core = self._core_experience()
            if not core.needs_consolidate(self._assistant_dir(aid)):
                return []
            if core.consolidate_in_cooldown(self._assistant_dir(aid)):
                return []
        except Exception:
            return []
        try:
            r = core.consolidate(self._assistant_dir(aid), llm=llm, retries=0)
        except Exception as e:
            return [{"error": str(e)}]
        return [r]

    def experience_consolidate(self, aid: str) -> Dict[str, Any]:
        """手动全库压缩（跨分类语义合并去重 + 重新归类）。"""
        try:
            llm = self._utility_llm(aid)
        except Exception as e:
            return {"changed": False, "error": f"llm_unavailable: {e}"}
        r = self._core_experience().consolidate(self._assistant_dir(aid), llm=llm)
        return r
