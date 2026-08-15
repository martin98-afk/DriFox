# -*- coding: utf-8 -*-
"""
工具注册表 — 工具插件化的核心（进程级单例）

职责：
- 注册 / 注销 / 枚举 / 查询工具
- 每个工具注册时携带完整元数据：schema、danger（危险/安全）、icon（SVG 图标名）、
  cn_name（中文显示名）、group（权限卡片分组）、description（权限卡片行描述）、
  aliases（Claude Code 风格别名）、impl（执行函数）
- 版本号单调递增：驱动 schema 缓存失效 + UI（权限卡片/渲染层）热刷新
- 热插拔并发安全：执行方调用前取 snapshot()，unregister 只移除映射，已持有引用不受影响

数据流（单一数据源）：
- get_builtin_tools_schema()   → registry.schemas()        （LLM 工具定义）
- render_helpers 图标/中文名    → registry 元数据            （消息卡片渲染）
- tool_control_card 分组/描述   → registry 元数据            （权限设置卡片）
- tool_classifier 危险分类      → registry.danger            （权限控制器）
- ToolNameMapper 别名          → registry aliases           （hook 上下文/命令解析）
- ToolExecutor 执行            → registry.impl               （工具分发）
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

# 工具危险级别（注册时必须显式声明）
DANGER_SAFE = "safe"
DANGER_DANGEROUS = "dangerous"

# 默认分组：未显式声明 group 的 safe 工具落入"安全操作"兜底组
GROUP_DEFAULT_SAFE = "安全操作"
GROUP_DEFAULT_DANGEROUS = "其他"


@dataclass
class ToolRegistration:
    """单个工具的完整注册信息"""

    name: str
    schema: Dict[str, Any]
    impl: Optional[Callable] = None  # 执行函数（可选；内置工具可走 ToolExecutor 特殊分发）
    danger: str = DANGER_SAFE  # safe | dangerous（强制声明）
    icon: str = "工具"  # SVG 图标文件名（不含扩展名，渲染层 fallback "工具"）
    icon_dir: str = ""  # 插件自带深色图标目录（绝对路径；空 → 渲染回退主程序 qrc 资源）
    icon_dir_light: str = ""  # 插件自带浅色图标目录（主题感知；空 → 回退深色/qrc）
    cn_name: str = ""  # 中文显示名（空 → 渲染层回退原名）
    group: str = ""  # 权限卡片分组（空 → 按 danger 落入兜底组）
    description: str = ""  # 权限卡片行内描述
    source: str = "builtin"  # builtin | plugin:<name>
    team_only: bool = False  # 团队专用：仅团队成员可见（非成员从 schema 定义中过滤）
    aliases: List[str] = field(default_factory=list)  # Claude Code 风格别名
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_plugin(self) -> bool:
        return self.source != "builtin"

    @property
    def display_group(self) -> str:
        """生效分组：显式 group 优先，否则按危险级别兜底"""
        if self.group:
            return self.group
        return GROUP_DEFAULT_DANGEROUS if self.danger == DANGER_DANGEROUS else GROUP_DEFAULT_SAFE

    @property
    def display_cn_name(self) -> str:
        return self.cn_name or self.name


class ToolRegistry:
    """工具注册表（进程级单例）。version 号驱动缓存失效与热重载快照。"""

    _instance: Optional["ToolRegistry"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._tools: Dict[str, ToolRegistration] = {}
        self._lock = threading.RLock()
        self._version = 0
        self._listeners: List[Callable[[int], None]] = []  # (new_version) 缓存失效钩子

    # ========== 单例 ==========

    @classmethod
    def get_instance(cls) -> "ToolRegistry":
        """获取进程级单例"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅测试用）"""
        with cls._instance_lock:
            cls._instance = None

    # ========== 注册 / 注销 ==========

    def register(
        self,
        name: str,
        schema: Dict[str, Any],
        impl: Optional[Callable] = None,
        danger: Optional[str] = None,
        icon: str = "",
        icon_dir: str = "",
        icon_dir_light: str = "",
        cn_name: str = "",
        group: str = "",
        description: str = "",
        source: str = "builtin",
        aliases: Optional[List[str]] = None,
        team_only: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        trusted: bool = False,
    ) -> bool:
        """注册工具。返回是否发生了变更（覆盖已存在视为变更）。

        - danger 强制声明：插件工具（source != builtin）未声明 danger 拒绝注册
        - source 白名单：builtin 源只能由内置种子流程（trusted=True）写入，
          防止插件伪装 builtin 绕过 danger 强制声明
        """
        if not name or not isinstance(name, str):
            logger.warning(f"[ToolRegistry] 非法工具名: {name!r}")
            return False
        if source == "builtin" and not trusted:
            logger.warning(f"[ToolRegistry] 工具 {name} 声称 builtin 源但非可信流程，拒绝注册")
            return False
        if danger is None:
            if source != "builtin":
                logger.warning(f"[ToolRegistry] 插件工具 {name} 未声明 danger，拒绝注册（必须显式声明）")
                return False
            # builtin：按 tool_classifier 分类推断（registry 空时兜底 safe）
            danger = _classify_fallback(name)
        if danger not in (DANGER_SAFE, DANGER_DANGEROUS):
            logger.warning(f"[ToolRegistry] 工具 {name} danger 非法值 {danger!r}，拒绝注册")
            return False
        reg = ToolRegistration(
            name=name,
            schema=copy.deepcopy(schema),
            impl=impl,
            danger=danger,
            icon=icon or "工具",
            icon_dir=icon_dir,
            icon_dir_light=icon_dir_light,
            cn_name=cn_name,
            group=group,
            description=description,
            source=source,
            team_only=bool(team_only),
            aliases=list(aliases or []),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._tools[name] = reg
            self._version += 1
        self._notify_change()
        return True

    def unregister(self, name: str) -> bool:
        """注销工具。返回是否发生了变更。"""
        with self._lock:
            if name not in self._tools:
                return False
            del self._tools[name]
            self._version += 1
        self._notify_change()
        return True

    def clear(self) -> None:
        """清空全部注册（测试用）"""
        with self._lock:
            if not self._tools:
                return
            self._tools.clear()
            self._version += 1
        self._notify_change()

    # ========== 查询 ==========

    def get(self, name: str) -> Optional[ToolRegistration]:
        with self._lock:
            return self._tools.get(name)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._tools.keys())

    def list(self) -> List[ToolRegistration]:
        """返回全部注册（副本，调用方可安全持有）"""
        with self._lock:
            return list(self._tools.values())

    def schemas(self) -> List[Dict[str, Any]]:
        """返回全部 schema 深拷贝（给 LLM 用）"""
        with self._lock:
            return [copy.deepcopy(r.schema) for r in self._tools.values()]

    def snapshot(self) -> Dict[str, ToolRegistration]:
        """执行快照：工具执行前取用，热重载（unregister）不影响已启动调用。"""
        with self._lock:
            return dict(self._tools)

    def version(self) -> int:
        with self._lock:
            return self._version

    # ========== 聚合查询（渲染层 / 权限层 / 别名层共用） ==========

    def get_meta(self, name: str) -> Dict[str, Any]:
        """获取单个工具的展示元数据（渲染层/权限卡片用）。未注册返回空 dict。"""
        reg = self.get(name)
        if reg is None:
            return {}
        return {
            "icon": reg.icon,
            "cn_name": reg.display_cn_name,
            "group": reg.display_group,
            "danger": reg.danger,
            "description": reg.description,
            "source": reg.source,
        }

    def get_icon(self, name: str) -> str:
        reg = self.get(name)
        return reg.icon if reg is not None else "工具"

    def get_icon_dir(self, name: str) -> str:
        """获取工具插件的自带深色图标目录（空 = 无插件图标，渲染回退主程序资源）"""
        reg = self.get(name)
        return reg.icon_dir if reg is not None else ""

    def get_icon_dir_light(self, name: str) -> str:
        """获取工具插件的自带浅色图标目录（空 = 无浅色版，渲染回退深色/qrc）"""
        reg = self.get(name)
        return reg.icon_dir_light if reg is not None else ""

    def get_cn_name(self, name: str) -> str:
        reg = self.get(name)
        return reg.display_cn_name if reg is not None else ""

    def get_danger(self, name: str) -> str:
        """查询工具危险级别（未注册默认 safe）"""
        reg = self.get(name)
        return reg.danger if reg is not None else DANGER_SAFE

    def get_group(self, name: str) -> str:
        reg = self.get(name)
        return reg.display_group if reg is not None else GROUP_DEFAULT_SAFE

    def get_aliases(self, name: str) -> List[str]:
        reg = self.get(name)
        return list(reg.aliases) if reg is not None else []

    def is_team_only(self, name: str) -> bool:
        """工具是否团队专用（仅团队成员可见）"""
        reg = self.get(name)
        return reg.team_only if reg is not None else False

    def team_only_tools(self) -> List[str]:
        """全部团队专用工具名（供 schema 过滤）"""
        with self._lock:
            return [n for n, r in self._tools.items() if r.team_only]

    def group_map(self) -> Dict[str, List[ToolRegistration]]:
        """按展示分组聚合（权限卡片用）。保持注册顺序，组内危险工具在前。"""
        groups: Dict[str, List[ToolRegistration]] = {}
        with self._lock:
            for reg in self._tools.values():
                groups.setdefault(reg.display_group, []).append(reg)
        for tools in groups.values():
            tools.sort(key=lambda r: 0 if r.danger == DANGER_DANGEROUS else 1)
        return groups

    def dangerous_tools(self) -> List[str]:
        with self._lock:
            return [n for n, r in self._tools.items() if r.danger == DANGER_DANGEROUS]

    def safe_tools(self) -> List[str]:
        with self._lock:
            return [n for n, r in self._tools.items() if r.danger == DANGER_SAFE]

    # ========== 作用域过滤（按 agent 可用工具） ==========

    def filter_by_agent(self, agent_name: Optional[str], agent_tools: Optional[List[str]]) -> List[ToolRegistration]:
        """按 agent 作用域过滤：agent_tools 为 None 表示全量；否则仅返回白名单内工具。"""
        with self._lock:
            if agent_tools is None or not agent_tools:
                return list(self._tools.values())
            wanted = set(agent_tools)
            return [r for name, r in self._tools.items() if name in wanted]

    # ========== 缓存失效钩子 ==========

    def on_change(self, listener: Callable[[int], None]) -> None:
        """注册变更监听（schema 缓存失效 / UI 刷新）。返回后立即以当前版本回调一次。"""
        with self._lock:
            self._listeners.append(listener)
        try:
            listener(self._version)
        except Exception as e:
            logger.warning(f"[ToolRegistry] 变更监听回调失败: {e}")

    def _notify_change(self) -> None:
        with self._lock:
            version = self._version
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(version)
            except Exception as e:
                logger.warning(f"[ToolRegistry] 变更监听回调失败: {e}")


def _classify_fallback(tool_name: str) -> str:
    """builtin 源未声明 danger 时的兜底分类（registry 空时无法查表，按危险关键词启发式）"""
    dangerous_hint = (
        "write", "edit", "multi_edit", "bash", "exec", "kill", "remove", "delete",
        "upload", "mouse", "keyboard", "subagent_para", "subagent_dag",
        "todowrite", "stage_files", "bg_start", "bg_stop",
    )
    base = tool_name
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        base = parts[2] if len(parts) > 2 else tool_name
    return DANGER_DANGEROUS if base in dangerous_hint else DANGER_SAFE
