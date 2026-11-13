# -*- coding: utf-8 -*-
"""主题色字符串 → QColor 的统一解析（8 处历史副本的语义并集）。

覆盖格式与优先级：
1. QColor 实例直接返回
2. ``#hex``：QColor 原生解析 + isValid 校验
3. ``rgba()/rgb()``：alpha 兼容 0-255 整数与 0.0-1.0 浮点两派
   （值 <=1 视为浮点比例 ×255，否则按整数直取），各通道 clamp 0-255
4. ``hsl()/hsla()``：QColor(str) 不认 hsl 而 QSS 认，QPainter 侧必须自己
   换算 fromHsl（h 取模 360，s/l 百分比 → 0-255 clamp）；缺这条分支时
   团队角色块会整体退灰（tab_panel 历史 bug）
5. 其他串（颜色名等）交给 QColor(原串) 回退
6. 全部失败 → QColor(fallback)（有效时），仍无效 → 灰色 QColor(128,128,128)

各历史副本的私有 fallback 默认值（#212126 / #2a2a2e / 无）经 fallback
参数保留；只扩展支持范围，不改变任何现有成功解析的结果。
"""

from __future__ import annotations

import re

from PyQt5.QtGui import QColor

_HSL_RE = re.compile(r"hsla?\s*\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%", re.IGNORECASE)

_FALLBACK_GRAY = QColor(128, 128, 128)


def parse_color(value, fallback: str = "#212126") -> QColor:
    """把主题色串解析为 QColor；全部格式失败时依次回退 fallback / 灰色。"""
    if isinstance(value, QColor):
        return value
    s = str(value or "").strip().strip("'\"")
    try:
        if s.startswith("#"):
            c = QColor(s)
            if c.isValid():
                return c
        elif s.lower().startswith(("rgba(", "rgb(")):
            inner = s[s.index("(") + 1 : s.rindex(")")]
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 3:
                r, g, b = (int(round(float(parts[i]))) for i in range(3))
                a = 255
                if len(parts) >= 4:
                    av = float(parts[3])
                    a = int(round(av * 255)) if av <= 1 else int(round(av))
                return QColor(
                    max(0, min(255, r)),
                    max(0, min(255, g)),
                    max(0, min(255, b)),
                    max(0, min(255, a)),
                )
        elif s.lower().startswith("hsl"):
            m = _HSL_RE.match(s.strip())
            if m:
                h = int(round(float(m.group(1)))) % 360
                sat = int(round(float(m.group(2)) * 255 / 100))
                light = int(round(float(m.group(3)) * 255 / 100))
                return QColor.fromHsl(h, max(0, min(255, sat)), max(0, min(255, light)))
        else:
            c = QColor(s)
            if c.isValid():
                return c
    except Exception:
        pass
    fc = QColor(fallback)
    return fc if fc.isValid() else _FALLBACK_GRAY
