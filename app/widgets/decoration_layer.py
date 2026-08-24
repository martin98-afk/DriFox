# -*- coding: utf-8 -*-
"""装饰件叠加层（PR3 — 多区域主题插件基础设施）

挂载点：作为 host（main_widget）的子 widget，撑满 host 整屏。
每个 decoration 按 anchor 字符串定位到 host 内 reference widget 的指定位置。

支持 anchor（用户友好字符串）：
  - scene        : 撑满 chat_container
  - scene_top    : chat_container 顶部居中
  - scene_bottom : chat_container 底部居中（= 输入框上方附近）
  - input_top    : host._input_card 顶部居中（输入卡顶部）
  - input_bottom : host._input_card 底部居中

每个 decoration cfg 字段：
  - id        : str  唯一标识（仅用于去重/调试）
  - image     : str  主题文件夹相对路径或 :qrc 路径
  - anchor    : str  见上方锚点约定
  - offset    : [int, int] 偏移像素 [x, y]
  - stretch_x : bool 是否水平拉伸（仅 anchor='scene' 有效）
  - opacity   : float 0.0~1.0
"""

from typing import Optional

from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QGraphicsOpacityEffect,
    QLabel,
    QWidget,
)


# anchor 字符串 → (reference attr name, vertical_alignment, horizontal_alignment)
#   valign/halign 取值: "fill" | "top" | "bottom" | "center"
ANCHOR_MAP = {
    "scene":        ("chat_container",   "fill",   "fill"),
    "scene_top":    ("chat_container",   "top",    "center"),
    "scene_bottom": ("chat_container",   "bottom", "center"),
    "input_top":    ("_input_card",      "top",    "center"),
    "input_bottom": ("_input_card",      "bottom", "center"),
}


class DecorationLayer(QWidget):
    """对话区装饰件叠加层"""

    def __init__(self, host: QWidget, ref_resolver=None):
        super().__init__(host)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._ref_resolver = ref_resolver or (lambda attr: getattr(host, attr, None))
        self._items: list[dict] = []
        self._host = host
        # 默认撑满 host
        self.setGeometry(host.rect())
        try:
            host.installEventFilter(self)
        except Exception:
            pass

    def apply_config(self, decorations: Optional[list[dict]], image_resolver=None) -> None:
        """应用主题配置（空列表或 None 时清除所有装饰件）

        Args:
            decorations: get_theme_backgrounds()["decorations"] 返回的列表
            image_resolver: callable(str) -> str，解析图片路径
        """
        self._clear()
        if not decorations:
            return

        for dec in decorations:
            image = dec.get("image")
            if not image or not image_resolver:
                continue
            anchor = dec.get("anchor", "scene")
            spec = ANCHOR_MAP.get(anchor)
            if spec is None:
                continue
            ref_attr, _valign, _halign = spec
            ref_widget = self._ref_resolver(ref_attr)
            if ref_widget is None:
                continue

            resolved = image_resolver(image)
            pix = QPixmap(resolved)
            if pix.isNull():
                continue

            label = QLabel(self)
            label.setPixmap(pix)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setAttribute(Qt.WA_NoSystemBackground, True)

            opacity = dec.get("opacity", 1.0)
            if opacity < 1.0:
                effect = QGraphicsOpacityEffect(label)
                effect.setOpacity(opacity)
                label.setGraphicsEffect(effect)

            self._items.append({
                "dec": dec,
                "label": label,
                "ref_widget": ref_widget,
            })

            # 监听 reference widget 的 resize
            try:
                ref_widget.installEventFilter(self)
            except Exception:
                pass

        self._sync_all()

    def _clear(self) -> None:
        """清除所有装饰件"""
        for item in self._items:
            item["label"].deleteLater()
        self._items.clear()

    def _sync_all(self) -> None:
        """同步所有装饰件几何"""
        for item in self._items:
            self._position_item(item)

    def _position_item(self, item: dict) -> None:
        """按 anchor 计算单个装饰件的位置"""
        dec = item["dec"]
        label = item["label"]
        ref = item["ref_widget"]
        anchor = dec.get("anchor", "scene")
        spec = ANCHOR_MAP.get(anchor)
        if spec is None:
            return
        _, valign, halign = spec

        pixmap = label.pixmap()
        if pixmap is None or pixmap.isNull():
            return
        pix_w, pix_h = pixmap.width(), pixmap.height()

        # anchor='scene' 撑满 ref_widget
        if anchor == "scene":
            stretch_x = dec.get("stretch_x", False)
            if stretch_x:
                # 拉伸水平方向，高度按图片比例
                ref_w = ref.width()
                if ref_w > 0 and pix_w > 0:
                    scaled_h = int(pix_h * ref_w / pix_w)
                    label.setFixedSize(ref_w, scaled_h)
                else:
                    label.setFixedSize(ref_w, pix_h)
                label.move(0, 0)
            else:
                label.setFixedSize(ref.size())
                label.move(0, 0)
            return

        # ref 在 self（DecorationLayer，撑满 host）坐标系下的 topleft
        ref_topleft = ref.mapTo(self, QPoint(0, 0))
        ref_w, ref_h = ref.width(), ref.height()

        offset_x, offset_y = dec.get("offset", [0, 0])

        # 水平
        if halign == "center":
            x = ref_topleft.x() + (ref_w - pix_w) // 2 + offset_x
        else:
            x = ref_topleft.x() + offset_x

        # 垂直
        if valign == "top":
            y = ref_topleft.y() + offset_y
        elif valign == "bottom":
            y = ref_topleft.y() + ref_h - pix_h + offset_y
        else:
            y = ref_topleft.y() + (ref_h - pix_h) // 2 + offset_y

        label.move(x, y)
        label.setFixedSize(pix_w, pix_h)

    def eventFilter(self, watched, event) -> bool:
        """监听 host.resize 和各 ref_widget.resize → 同步装饰件几何"""
        if event.type() != QEvent.Resize:
            return super().eventFilter(watched, event)
        if watched is self._host:
            # host resize → 同步 DecorationLayer 本身
            self.setGeometry(self._host.rect())
        for item in self._items:
            if item["ref_widget"] is watched:
                self._position_item(item)
                break
        return super().eventFilter(watched, event)

    def refresh_theme(self) -> None:
        """theme_manager.refresh_target 钩子（实际重新加载由 host._apply_chat_backgrounds() 触发）"""
        pass