# -*- coding: utf-8 -*-
"""
QSS 生成器 - 将主题 YAML 的 components 段解析为 QSS 样式表

职责：
1. Resolver：解析 {colors.xxx} 引用，替换为实际色值
2. Renderer：将解析后的组件配置转为 QSS 字符串
3. Manager：缓存 + 集成 ThemeManager
4. Defaults：组件未声明属性的全局默认值
"""
import re
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# ── 组件映射注册表 ──────────────────────────────────
# 每个组件定义：(QSS 选择器, {属性名→CSS属性}, {状态名→伪类})

COMPONENT_MAP: Dict[str, dict] = {}

# ── 全局默认值 ──────────────────────────────────────

DEFAULTS = {
    "bg": "transparent",
    "text": "#ffffff",
    "border_color": "transparent",
    "border_width": "0px",
    "border_radius": "4px",
    "padding": "0px",
    "shadow": "none",
    "font_size": "14px",
    "font_weight": "normal",
    "opacity": "1.0",
}


# ══════════════════════════════════════════════════════
# Resolver — 引用解析
# ══════════════════════════════════════════════════════

class Resolver:
    """解析 {colors.xxx} 引用为实际色值"""

    _COLOR_REF_PATTERN = re.compile(r"\{colors\.([a-zA-Z_][a-zA-Z0-9_]*)\}")

    @classmethod
    def resolve(cls, value: str, colors: dict) -> str:
        """替换字符串中的 {colors.xxx} 引用

        Args:
            value: 可能包含 {colors.xxx} 的字符串
            colors: 主题的 colors dict

        Returns:
            替换后的字符串，不存在的引用保留原样 + 日志警告
        """
        def _replace(match):
            key = match.group(1)
            if key in colors:
                return colors[key]
            logger.warning(f"[QSSGenerator] colors.{key} 不存在，保留引用")
            return match.group(0)

        return cls._COLOR_REF_PATTERN.sub(_replace, value)

    @classmethod
    def resolve_dict(cls, config: dict, colors: dict) -> dict:
        """递归替换 dict 中所有字符串值里的引用"""
        result = {}
        for k, v in config.items():
            if isinstance(v, str):
                result[k] = cls.resolve(v, colors)
            elif isinstance(v, dict):
                result[k] = cls.resolve_dict(v, colors)
            elif isinstance(v, list):
                result[k] = [cls.resolve(item, colors) if isinstance(item, str) else item for item in v]
            else:
                result[k] = v
        return result


# ══════════════════════════════════════════════════════
# Renderer — QSS 输出
# ══════════════════════════════════════════════════════

class Renderer:
    """将解析后的组件配置转为 QSS 字符串"""

    _PROP_MAP = {
        "bg": "background",
        "text": "color",
        "border_color": "border-color",
        "border_width": "border-width",
        "border_radius": "border-radius",
        "padding": "padding",
        "shadow": "box-shadow",
        "font_size": "font-size",
        "font_weight": "font-weight",
        "opacity": "opacity",
    }

    @classmethod
    def render(cls, component_id: str, config: dict) -> str:
        """生成单个组件的 QSS

        Args:
            component_id: 组件 ID（必须在 COMPONENT_MAP 中注册）
            config: 已解析的配置 dict（颜色引用已替换为实际值）

        Returns:
            QSS 样式字符串（可 setStyleSheet）
        """
        if component_id not in COMPONENT_MAP:
            logger.warning(f"[QSSGenerator] 未知组件: {component_id}")
            return ""

        meta = COMPONENT_MAP[component_id]
        selector = meta["selector"]
        props = meta.get("props", {})
        states = meta.get("states", {})

        parts = []

        # 默认状态
        default_block = cls._build_qss_block(config, props)
        if default_block:
            parts.append(f"{selector} {{\n{default_block}}}")

        # 状态覆盖
        state_config = config.get("states", {})
        for state_name, state_values in state_config.items():
            if not isinstance(state_values, dict):
                continue
            pseudo = states.get(state_name)
            if not pseudo:
                continue
            merged = dict(config)
            merged.update(state_values)
            block = cls._build_qss_block(merged, props)
            if block:
                parts.append(f"{selector}{pseudo} {{\n{block}}}")

        return "\n\n".join(parts)

    @classmethod
    def _build_qss_block(cls, config: dict, props: dict) -> str:
        """从配置中提取 props 指定的属性，生成 QSS 声明块"""
        lines = []
        for prop_name, css_prop in props.items():
            value = cls._get_prop_value(config, prop_name)
            if value is None or value == "inherit":
                continue
            lines.append(f"  {css_prop}: {value};")

        # 智能合并 border
        border_parts = cls._try_merge_border(config, props)
        if border_parts:
            lines.append(f"  border: {border_parts};")

        return "\n".join(lines) + "\n" if lines else ""

    @classmethod
    def _get_prop_value(cls, config: dict, prop_name: str) -> Optional[str]:
        """从 config 中获取属性值（含状态合并），未声明则用 Defaults"""
        value = config.get(prop_name)
        if value is not None:
            return str(value)
        return DEFAULTS.get(prop_name)

    @classmethod
    def _try_merge_border(cls, config: dict, props: dict) -> Optional[str]:
        """尝试合并 border_width + border_color → border 简写"""
        if "border_width" not in props and "border_color" not in props:
            return None
        width = cls._get_prop_value(config, "border_width")
        color = cls._get_prop_value(config, "border_color")
        if not width or width == "0px" or width == "0":
            return None
        if not color or color == "transparent":
            return None
        return f"{width} solid {color}"


# ══════════════════════════════════════════════════════
# Manager — 缓存与集成
# ══════════════════════════════════════════════════════

class QSSManager:
    """QSS 缓存 + 集成入口"""

    def __init__(self):
        self._cache: Dict[str, Dict[str, str]] = {}  # {theme_id: {component_id: qss}}

    def build(self, component_id: str, component_config: dict, colors: dict) -> str:
        """构建单个组件的 QSS（不缓存）"""
        resolved = Resolver.resolve_dict(component_config, colors)
        return Renderer.render(component_id, resolved)

    def build_all(self, theme_id: str, components: dict, colors: dict) -> Dict[str, str]:
        """构建主题所有组件的 QSS，写入缓存"""
        result = {}
        for cid, ccfg in components.items():
            qss = self.build(cid, ccfg, colors)
            if qss:
                result[cid] = qss
        self._cache[theme_id] = result
        return result

    def get(self, theme_id: str, component_id: str) -> str:
        """从缓存获取组件 QSS（缓存未命中返回空字符串）"""
        return self._cache.get(theme_id, {}).get(component_id, "")

    def get_all(self, theme_id: str) -> Dict[str, str]:
        """获取主题的所有组件 QSS"""
        return self._cache.get(theme_id, {})

    def invalidate(self, theme_id: str = None):
        """清除缓存"""
        if theme_id:
            self._cache.pop(theme_id, None)
        else:
            self._cache.clear()


# 全局单例
qss_manager = QSSManager()


# ══════════════════════════════════════════════════════
# 公共入口函数
# ══════════════════════════════════════════════════════

def apply_theme_qss(theme_id: str) -> Dict[str, str]:
    """从 ThemeManager 读取主题数据，生成并缓存所有组件的 QSS

    Args:
        theme_id: 主题 ID

    Returns:
        {component_id: qss} dict，可用于逐个应用样式
    """
    from app.utils.theme_manager import theme_manager as tm
    theme = tm.get_theme(theme_id)
    if not theme:
        logger.warning(f"[QSSGenerator] 主题 '{theme_id}' 不存在")
        return {}

    components = theme.get("components", {})
    colors = theme.get("colors", {})

    return qss_manager.build_all(theme_id, components, colors)


def get_component_qss(component_id: str) -> str:
    """获取当前主题下某组件的 QSS（便捷方法）

    用于 UI 组件初始化时应用主题样式。
    必须在 ThemeManager 初始化完成后调用。
    """
    from app.utils.theme_manager import theme_manager as tm
    theme_id = tm.get_current_theme_id()
    return qss_manager.get(theme_id, component_id)


def rebuild_all():
    """重建所有已加载主题的 QSS（ThemeManager.on_reload 回调）"""
    from app.utils.theme_manager import theme_manager as tm
    theme_ids = tm.list_themes()
    for tid in theme_ids:
        apply_theme_qss(tid)
