# -*- coding: utf-8 -*-
"""插件展示名解析：中文别名优先，缺失回退插件标识名。"""

from typing import Any, Dict


def plugin_display_name(meta: Dict[str, Any]) -> str:
    """返回市场 UI 应显示的插件名。

    name 是插件标识主键（安装/去重/排序/记录全程依赖），永不参与显示替换。
    """
    alias = meta.get("display_name_zh")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    return str(meta.get("name", "") or "")
