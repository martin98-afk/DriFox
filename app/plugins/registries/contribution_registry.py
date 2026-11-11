# -*- coding: utf-8 -*-
"""插件贡献注册表 — 插件运行时注入命令/技能源/智能体的统一扩展点（能力池）。

三类贡献（kind）：
- skill_source: 动态技能源。provider(workdir) -> list[dict]，条目与 get_local_skills
  静态条目同构（name/qualified_name/description/path），额外支持
  source（来源标记，如 "project"）与 auto_enable（自动进「偏好技能」段）字段。
  watched_dirs(workdir) -> list[Path] 参与缓存 key（目录内容变化自动失效）。
- command: 斜杠命令定义 dict（name/description/display_type/prompt_text）。
- agent: 智能体定义 dict（Agent.from_dict 兼容的字段集）。

键 (plugin, kind, name)，重复注册覆盖（幂等）。unregister_source(plugin) 按插件名
整组注销，挂接 plugin_manager.disable_plugin 卸载链。

对齐 stream_sink_registry.py / serializer_registry.py 的单例模式。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional


class ContributionRegistry:
    """插件贡献注册表：register 覆盖同键；unregister_source 按插件名整组注销"""

    def __init__(self) -> None:
        # plugin -> kind -> name -> entry
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # ---- 注册 ----

    def register_skill_source(
        self,
        plugin: str,
        name: str,
        provider: Callable[[Any], List[Dict[str, Any]]],
        watched_dirs: Optional[Callable[[Any], List[Any]]] = None,
        description: str = "",
    ) -> None:
        with self._lock:
            self._data.setdefault(plugin, {}).setdefault("skill_source", {})[name] = {
                "provider": provider,
                "watched_dirs": watched_dirs,
                "description": description,
            }

    def register_command(self, plugin: str, definition: Dict[str, Any]) -> None:
        name = str(definition.get("name") or "").strip()
        if not name:
            raise ValueError("贡献 command 缺少 name")
        with self._lock:
            self._data.setdefault(plugin, {}).setdefault("command", {})[name] = dict(definition)

    def register_agent(self, plugin: str, definition: Dict[str, Any]) -> None:
        name = str(definition.get("name") or "").strip()
        if not name:
            raise ValueError("贡献 agent 缺少 name")
        with self._lock:
            self._data.setdefault(plugin, {}).setdefault("agent", {})[name] = dict(definition)

    def unregister_source(self, plugin: str) -> None:
        with self._lock:
            self._data.pop(plugin, None)

    # ---- 查询（按插件名字母序稳定排序） ----

    def iter_skill_sources(self) -> List[tuple]:
        """[(plugin, name, entry), ...]"""
        with self._lock:
            out: List[tuple] = []
            for plugin in sorted(self._data):
                for name, entry in sorted(self._data[plugin].get("skill_source", {}).items()):
                    out.append((plugin, name, entry))
            return out

    def get_commands(self) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            for plugin in sorted(self._data):
                for d in self._data[plugin].get("command", {}).values():
                    out.append({**d, "_plugin": plugin})
            return out

    def get_command(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for plugin in sorted(self._data):
                d = self._data[plugin].get("command", {}).get(name)
                if d is not None:
                    return {**d, "_plugin": plugin}
        return None

    def get_agent_def(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for plugin in sorted(self._data):
                d = self._data[plugin].get("agent", {}).get(name)
                if d is not None:
                    return {**d, "_plugin": plugin}
        return None

    def get_agents(self) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            for plugin in sorted(self._data):
                for d in self._data[plugin].get("agent", {}).values():
                    out.append({**d, "_plugin": plugin})
            return out

    @staticmethod
    def get_instance() -> "ContributionRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = ContributionRegistry()
            return _instance


_instance: Optional[ContributionRegistry] = None
_instance_lock = threading.Lock()
