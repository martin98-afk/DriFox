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
        # 直接走类名（不经 cls）：元类上的 cls 类型推断解析不到类方法，
        # 且 ALIAS_MAP 语义上就是「类级公开拷贝」，不依赖元类实例。
        return ToolNameMapper._build_alias_map()


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
    # 正向映射缓存（PERF T32）：缓存键 = (registry version, extra 版本)，两者任一变化即重建
    _alias_map_cache: Optional[Dict[str, List[str]]] = None
    _alias_map_version: Optional[tuple] = None  # (registry version, extra_alias_version)
    _extra_alias_version: int = 0  # register_alias 自增（不经过 registry version）
    # 反向映射缓存（registry 版本变化时失效）
    _reverse_map: Optional[Dict[str, str]] = None
    _reverse_version: int = -1

    @classmethod
    def _alias_map_cached(cls) -> Dict[str, List[str]]:
        """获取别名映射缓存（内部快路径，返回共享引用，调用方只读不得改写）。

        [PERF T32] 旧 _build_alias_map() 每次调用都全量重建（遍历 registry +
        重建 dict），to_native/is_known 每次调用各触发一次 → 别名解析 O(N²)。
        现按 (registry version, extra 版本) 惰性重建一次，后续调用 O(1) 命中。
        """
        try:
            from app.tools import _ensure_plugin_tools_loaded
            from app.tools.registry import ToolRegistry

            _ensure_plugin_tools_loaded()  # [PERF] 首读前确保插件工具已加载（幂等）
            version = ToolRegistry.get_instance().version()
        except Exception:
            version = -1
        key = (version, cls._extra_alias_version)
        cache = cls._alias_map_cache
        if cache is not None and cls._alias_map_version == key:
            return cache
        try:
            from app.tools import _ensure_plugin_tools_loaded
            from app.tools.registry import ToolRegistry

            _ensure_plugin_tools_loaded()
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
        except Exception:
            result = {name: list(aliases) for name, aliases in cls._extra_aliases.items()}
        cls._alias_map_cache = result
        cls._alias_map_version = key
        return result

    @classmethod
    def _build_alias_map(cls) -> Dict[str, List[str]]:
        """从 registry 聚合全部工具的别名映射（拷贝语义：调用方可安全改写）

        公开面（ALIAS_MAP / 外部调用）走本方法，与缓存隔离污染；
        内部热路径请用 _alias_map_cached()。
        """
        return {name: list(aliases) for name, aliases in cls._alias_map_cached().items()}

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
        for native, aliases in cls._alias_map_cached().items():
            for alias in aliases:
                reverse[alias] = native
        cls._reverse_map = reverse
        cls._reverse_version = version
        return reverse


    @classmethod
    def to_native(cls, name: str) -> str:
        """将任意已知工具名转换为 DriFox 原生名

        如果已经是原生名或未知名，原样返回。
        """
        if not name:
            return name

        name_lower = name.lower()

        # 快速路径：已经是原生名
        if name_lower in cls._alias_map_cached():
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
        aliases = cls._alias_map_cached().get(native, [])
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
        if native in cls._alias_map_cached():
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
        # 双保险：版本自增（供未来基于版本的判定）+ 直接失效两处缓存
        cls._extra_alias_version += 1
        cls._alias_map_cache = None
        cls._reverse_map = None
