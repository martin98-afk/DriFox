# -*- coding: utf-8 -*-
"""
工具名别名映射器 — 双向映射（registry 驱动）

数据源：ToolRegistry（工具插件注册时的 aliases 元数据）。

功能：
1. to_native(): 将 Claude Code/Cursor 等外部平台工具名 → DriFox 原生名（小写）
2. to_claude_style(): 将任意已知工具名 → Claude Code 风格（PascalCase）
   用于 hook context，使第三方 Claude Code 插件能正确匹配工具名。

LLM 永远看到 DriFox 原生名，此映射器只在系统内部使用。
"""
from typing import Dict, List, Optional


class _ToolNameMapperMeta(type):
    """元类：让 ToolNameMapper.ALIAS_MAP 作为动态属性（兼容旧代码直接读 ALIAS_MAP）"""

    @property
    def ALIAS_MAP(cls) -> Dict[str, List[str]]:
        return cls._build_alias_map()


class ToolNameMapper(metaclass=_ToolNameMapperMeta):
    """
    工具名双向映射器（registry 驱动）

    用法：
        ToolNameMapper.to_native("Read")       → "read"
        ToolNameMapper.to_native("read")       → "read"  (passthrough)
        ToolNameMapper.to_claude_style("edit") → "Edit"
        ToolNameMapper.to_claude_style("read") → "Read"
    """

    # 运行时补充别名（register_alias 写入，叠加在 registry 之上）
    _extra_aliases: Dict[str, List[str]] = {}
    # 反向映射缓存（registry 版本变化时失效）
    _reverse_map: Optional[Dict[str, str]] = None
    _reverse_version: int = -1

    @classmethod
    def _build_alias_map(cls) -> Dict[str, List[str]]:
        """从 registry 聚合全部工具的别名映射"""
        try:
            from app.tools import _ensure_plugin_tools_loaded
            from app.tools.registry import ToolRegistry

            _ensure_plugin_tools_loaded()  # [PERF] 首读前确保插件工具已加载（幂等）
            result: Dict[str, List[str]] = {}
            for reg in ToolRegistry.get_instance().list():
                aliases = list(reg.aliases)
                if aliases:
                    result[reg.name] = aliases
            for name, aliases in cls._extra_aliases.items():
                result.setdefault(name, [])
                for a in aliases:
                    if a not in result[name]:
                        result[name].append(a)
            return result
        except Exception:
            return dict(cls._extra_aliases)

    @classmethod
    def _reverse(cls) -> Dict[str, str]:
        """构建反向映射（带 registry 版本缓存）"""
        try:
            from app.tools.registry import ToolRegistry

            version = ToolRegistry.get_instance().version()
        except Exception:
            version = -1
        if cls._reverse_map is not None and cls._reverse_version == version:
            return cls._reverse_map
        reverse: Dict[str, str] = {}
        for native, aliases in cls._build_alias_map().items():
            for alias in aliases:
                reverse[alias] = native
        cls._reverse_map = reverse
        cls._reverse_version = version
        return reverse

    @classmethod
    def known_names(cls) -> List[str]:
        """获取全部已知工具名（registry 驱动，供 hook 设置卡片下拉等使用）"""
        try:
            from app.tools import _ensure_plugin_tools_loaded
            from app.tools.registry import ToolRegistry

            _ensure_plugin_tools_loaded()  # [PERF] 首读前确保插件工具已加载（幂等）
            return ToolRegistry.get_instance().names()
        except Exception:
            return sorted(cls._build_alias_map().keys())

    @classmethod
    def to_native(cls, name: str) -> str:
        """将任意已知工具名转换为 DriFox 原生名

        如果已经是原生名或未知名，原样返回。
        """
        if not name:
            return name

        name_lower = name.lower()

        # 快速路径：已经是原生名
        if name_lower in cls._build_alias_map():
            return name_lower

        reverse = cls._reverse()

        # 精确匹配（保留大小写，如 "Read" → "read"）
        if name in reverse:
            return reverse[name]

        # 不区分大小写匹配（如 "ReAd" → "read"）
        if name_lower in reverse:
            return reverse[name_lower]

        return name  # 未知名，原样返回

    @classmethod
    def to_claude_style(cls, name: str) -> str:
        """将任意已知工具名转换为 Claude Code 风格（PascalCase）

        用于 hook context 中的 tool_name 字段，使第三方 Claude Code 插件
        （如 security-guidance、hookify 等）能通过 'Edit|Write|MultiEdit'
        等大小写敏感匹配来正确识别工具。

        Args:
            name: 工具名（如 "edit", "Write", "multi_edit", "Read"）

        Returns:
            Claude Code 风格的工具名（如 "Edit", "Write", "MultiEdit", "Read"）
            如果是 MCP 工具（mcp__ 前缀）或未知名，原样返回。
        """
        if not name:
            return name

        # MCP 工具或未知工具：原样返回
        if name.startswith("mcp__"):
            return name

        # 先归一化到 DriFox 原生名
        native = cls.to_native(name)

        # 快速路径：原生名本身就是 Claude Code 风格（如 "mcp__xxx"）
        if native.startswith("mcp__"):
            return native

        # 查找别名映射中该原生名的第一个别名（通常是 Claude Code 风格）
        aliases = cls._build_alias_map().get(native, [])
        if aliases:
            return aliases[0]  # 第一个别名：如 "edit" → "Edit", "read" → "Read"

        # 回退：简单首字母大写
        return native.capitalize()

    @classmethod
    def is_known(cls, name: str) -> bool:
        """检查工具名是否可映射到已知的 DriFox 工具

        已知工具包括：
        - 注册工具（registry 中的原生名）
        - MCP 工具（以 mcp__ 开头的动态工具名）

        Args:
            name: 工具名（如 "Read", "SomeCustomTool"）

        Returns:
            True 表示已知工具，False 表示无法映射的未知名
        """
        if not name:
            return False
        native = cls.to_native(name)
        if native in cls._build_alias_map():
            return True
        # MCP 工具（动态发现，运行时注入）
        if native.startswith("mcp__"):
            return True
        return False

    @classmethod
    def register_alias(cls, native_name: str, alias: str):
        """动态注册别名（运行时补充，叠加在 registry 之上）"""
        native_lower = native_name.lower()
        if native_lower not in cls._extra_aliases:
            cls._extra_aliases[native_lower] = []
        if alias not in cls._extra_aliases[native_lower]:
            cls._extra_aliases[native_lower].append(alias)
        # 清除反向映射缓存以便重建
        cls._reverse_map = None
