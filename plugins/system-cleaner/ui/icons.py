# -*- coding: utf-8 -*-
"""SVG 图标数据与渲染 — 线性风格图标，替代 emoji

设计约束（闭包）：
- 仅依赖 PyQt5（QtSvg）与 stdlib
- 图标源为 feather 风格 24x24 线性 path，stroke 色由渲染时注入
"""

from typing import Dict

from PyQt5.QtCore import QByteArray, Qt
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer

# 24x24 viewBox 线性图标 path 集（fill=none + stroke 风格）
_ICONS: Dict[str, str] = {
    # 双文档副本（编辑类备份）
    "copy": (
        '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>'
        '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    ),
    # 垃圾桶（删除类备份）
    "trash": (
        '<polyline points="3 6 5 6 21 6"/>'
        '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
        '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>'
    ),
    # 文档行（日志）
    "file": (
        '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
        '<polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/>'
        '<line x1="16" y1="17" x2="8" y2="17"/>'
    ),
    # 数据库（应用缓存）
    "database": (
        '<ellipse cx="12" cy="5" rx="9" ry="3"/>'
        '<path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/>'
        '<path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>'
    ),
    # 相机（截图）
    "camera": (
        '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>'
        '<circle cx="12" cy="13" r="4"/>'
    ),
    # 盒子（归档）
    "archive": (
        '<polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/>'
        '<line x1="10" y1="12" x2="14" y2="12"/>'
    ),
    # 上传（分享会话）
    "upload": (
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
        '<polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>'
    ),
    # 扳手（工具结果缓存）
    "wrench": (
        '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77'
        'a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>'
    ),
    # 波形（内存）
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    # 扫描/刷新态（缓存分区标题）
    "broom": ('<path d="M19 3l-7 7"/><path d="M11 6l7 7"/><path d="M4 20c3-1 5-3 6-6l4 4c-3 1-5 3-6 6"/>'),
}

_SVG_TPL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{px}" height="{px}" '
    'fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
)


def svg_pixmap(name: str, color: str, size: int = 16, dpr: float = 1.0) -> QPixmap:
    """把命名图标渲染为指定颜色的 QPixmap（透明底）

    Args:
        name: _ICONS 键；未知键返回空 pixmap
        color: stroke 颜色（任意 Qt 可解析色值，含 rgba()）
        size: 逻辑像素边长
        dpr: 设备像素比（高分屏渲染不糊）
    """
    body = _ICONS.get(name, "")
    pm = QPixmap()
    if not body:
        return pm
    physical = max(1, int(size * dpr))
    svg = _SVG_TPL.format(px=physical, color=color, body=body)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(physical, physical)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm
