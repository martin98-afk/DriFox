# -*- coding: utf-8 -*-
"""FluentIcon SVG 渲染缓存补丁

qfluentwidgets 的图标绘制链路（ToolButton / IconWidget.paintEvent →
drawIcon → FluentIconBase.render → drawSvgIcon）每次重绘都新建
QSvgRenderer 重新解析并光栅化 SVG，无任何缓存。列表滚动时视口内
数十个图标 widget 每帧各走一遍完整链路，构成掉帧主热点（离屏基准
中约占总帧时长 40-50%，WARP 软件光栅下被进一步放大）。

本补丁替换 qfluentwidgets.common.icon.drawSvgIcon 为带缓存版本：
1. QSvgRenderer 按 SVG 源（str 路径 / bytes 代码）缓存，消除重复解析
   （QSvgRenderer 复用为 Qt 官方推荐模式）；
2. 渲染结果按 (源, 尺寸, dpr) 缓存为 QPixmap，命中时直接 drawPixmap。

补丁打在 qfluentwidgets 模块属性上，全进程生效（主程序同样受益）。
行为与原实现一致、纯加速：key 覆盖主题与颜色差异（不同 path/内容
不同 key）；缓存条目为内置图标与插件 icon.svg，量级数百、内存可忽略，
超上限整体清空防异常膨胀。
"""

from typing import Optional

from PyQt5.QtCore import QRectF, Qt, QSize
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer

_APPLIED = False

# 渲染器缓存：SVG 源(str 路径 / bytes 代码) → QSvgRenderer
_renderers: dict = {}
# 位图缓存：(SVG 源, 宽, 高, dpr) → QPixmap
_pixmaps: dict = {}

# 缓存上限（正常量级远小于此，超限整体清空防异常膨胀）
_CACHE_LIMIT = 1000


def _clear_cache():
    """清空全部缓存（主题切换等大变化时可主动调用）"""
    _renderers.clear()
    _pixmaps.clear()


def _apply_icon_cache_patch() -> bool:
    """打补丁（幂等）。返回是否本次实际生效。"""
    global _APPLIED
    if _APPLIED:
        return False

    import qfluentwidgets.common.icon as _icon_mod

    _orig_draw_svg_icon = _icon_mod.drawSvgIcon

    def _cached_draw_svg_icon(icon, painter: QPainter, rect):
        # 仅缓存 str（svg 文件路径）与 bytes（svg 代码）两种源；
        # 其它类型（QByteArray 等）走原实现
        key: Optional[object] = icon if isinstance(icon, (str, bytes)) else None
        if key is None:
            _orig_draw_svg_icon(icon, painter, rect)
            return

        rectf = QRectF(rect)
        w = round(rectf.width())
        h = round(rectf.height())
        dpr = painter.device().devicePixelRatioF()
        # 尺寸非整数时退回 renderer 路径，避免亚像素误差
        if w <= 0 or h <= 0 or abs(rectf.width() - w) > 0.01 or abs(rectf.height() - h) > 0.01:
            renderer = _renderers.get(key)
            if renderer is None:
                renderer = QSvgRenderer(icon)
                _renderers[key] = renderer
            renderer.render(painter, rectf)
            return

        pkey = (key, w, h, dpr)
        pix = _pixmaps.get(pkey)
        if pix is None:
            renderer = _renderers.get(key)
            if renderer is None:
                renderer = QSvgRenderer(icon)
                _renderers[key] = renderer
            pix = QPixmap(w * dpr, h * dpr)
            pix.setDevicePixelRatio(dpr)
            pix.fill(Qt.transparent)
            p = QPainter(pix)
            renderer.render(p, QRectF(0, 0, w, h))
            p.end()
            if len(_pixmaps) >= _CACHE_LIMIT:
                _pixmaps.clear()
            _pixmaps[pkey] = pix
        painter.drawPixmap(rectf.topLeft(), pix)

    _icon_mod.drawSvgIcon = _cached_draw_svg_icon
    _APPLIED = True
    return True
