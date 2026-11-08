# -*- coding: utf-8 -*-
"""插件展示名解析（国际化预留）。

plugin.json 契约：``display_name`` 是语言码到文本的映射，例如
``{"zh": "插件市场", "en": "Plugin Marketplace"}``。当前语言取不到时依次回退
``zh`` → 映射里任意非空值 → 插件标识名 ``name``。本轮只填 ``zh``，未来新增语言
只需往映射里加键，消费方零改动。

``name`` 是插件标识主键（安装 / 去重 / 排序 / 记录 / 依赖全程依赖），永不参与
显示替换。本模块位于主程序公共层，供系统设置页与插件市场共用；插件代码可
``import app.plugins.contracts.plugin_display``，反向不成立。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# 语言优先级中末尾的兜底语言。当前界面为中文，未来接入 locale 后由
# preferred_language() 给出首选码，映射里没有该码时仍会走到 zh。
_FALLBACK_LANG = "zh"


def preferred_language() -> str:
    """当前界面语言码（小写，短码）。

    全程序尚无 QTranslator / locale 框架，界面文案为中文，固定返回 zh。
    未来做多语言版本：这里改为读取设置项 / QLocale 即可，消费方零改动。
    """
    return "zh"


def plugin_display_name(meta: Dict[str, Any], lang: Optional[str] = None) -> str:
    """返回应显示给用户的插件名。

    Args:
        meta: 插件 manifest / 市场条目 / PluginInfo 的 manifest 等含 name 的 dict
        lang: 目标语言码；缺省用 preferred_language()

    Returns:
        映射中该语言的文本；无可用映射时回退 ``name``。
    """
    raw = meta.get("display_name") if isinstance(meta, dict) else None
    if isinstance(raw, dict):
        prefer = (lang or preferred_language() or "").lower()
        for code in (prefer, prefer.split("-")[0], _FALLBACK_LANG):
            val = raw.get(code)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in raw.values():
            if isinstance(val, str) and val.strip():
                return val.strip()
    elif isinstance(raw, str) and raw.strip():
        # 兼容早期单字符串写法（迁移期兜底，不作为正式契约）
        return raw.strip()
    return str((meta.get("name") if isinstance(meta, dict) else None) or "")
