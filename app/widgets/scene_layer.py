# -*- coding: utf-8 -*-
"""场景背景层（PR3 — 多区域主题插件基础设施）

挂载点：作为 TabManagerWindow._chat_frame 的子 widget（绝对定位，不参与布局）。
       撑满 _chat_frame.rect()——含 replace_tab_bar / 对话区 / 输入框 /
       LEFT/RIGHT/BOTTOM 停靠区 / UI 插件槽位等所有 _chat_frame 内的内容。
       是 aurora 等主题的「右侧整个区域」场景背景图。
渲染：QLabel + QPixmap，支持 image/opacity/blur/dim。
同步：监听 parent.resize 自动同步 geometry。

设计取舍：
- 作为 TabManagerWindow._chat_frame 子 widget（2026-08 改造）：
  scene 图片覆盖整个右侧圆角矩形（含 UI 插件槽位），而不仅是 OpenAIChatToolWindow
  对话区滚动区内层。
- SceneLayer 在 _chat_frame 最底层（.lower()），所有 _chat_frame 内的 UI 控件
  （replace_tab_bar / dock splitter / 对话区 / 输入框 / UI 插件槽位）都在它之上。
- SceneLayer 自身设 border-radius 8px 适配 _chat_frame 的圆角矩形，
  避免图片在 _chat_frame 圆角外溢出成方块。
- 整体（self）挂 QGraphicsOpacityEffect 控制 opacity。
- 内部 _image_label 单独挂 QGraphicsBlurEffect 控制 blur。
  Qt 限制一个 widget 只能挂一个 effect，所以拆开挂载。
- _dim_label 是纯 background-color，不挂 effect（叠在 _image_label 之上）。
"""

from typing import Optional

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsOpacityEffect,
    QLabel,
    QWidget,
)

# 适配 _chat_frame 的 8px 圆角；SceneLayer 自身和内部图片标签都按此设置
_CHAT_FRAME_RADIUS = 8

from typing import Optional

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsOpacityEffect,
    QLabel,
    QWidget,
)


class SceneLayer(QWidget):
    """对话区场景背景层

    支持的 cfg 字段（来自 yaml `backgrounds.scene`）：
      - image   : str  主题文件夹相对路径或 :qrc 路径
      - color   : str  纯色背景（CSS color，如 '#0f1626' / 'rgba(...)'），与 image 共存
      - opacity : float 0.0~1.0（<1.0 时挂 QGraphicsOpacityEffect）
      - blur    : int   0~30（>0 时挂 QGraphicsBlurEffect 到 _image_label）
      - dim     : str  半透明蒙版（CSS color），叠加在图片上压暗提升文字可读性
      - enabled : bool  默认 True
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

        self._image_label: Optional[QLabel] = None
        self._dim_label: Optional[QLabel] = None

    def apply_config(self, cfg: Optional[dict], image_resolver=None) -> None:
        """应用主题配置（None 或 enabled=False 时清除所有内容）

        Args:
            cfg: get_theme_backgrounds()["scene"] 返回的 dict 或 None
            image_resolver: callable(str) -> str，解析图片路径（注入避免循环依赖）
        """
        self._clear()

        if not cfg or not cfg.get("enabled", True):
            return

        # 1. 纯色底（color）—— 作为 self 的 background-color
        # SceneLayer 自身 + image_label/dim_label 都设 border-radius 8px 适配
        # _chat_frame 圆角矩形，避免图片在圆角外溢出成方块。
        color = cfg.get("color")
        if color:
            self.setAutoFillBackground(True)
            self.setStyleSheet(
                f"background-color: {color}; border-radius: {_CHAT_FRAME_RADIUS}px;"
            )

        # 2. 图片层（独立 widget，可挂 blur effect）
        image = cfg.get("image")
        if image and image_resolver:
            resolved = image_resolver(image)
            from PyQt5.QtGui import QPixmap

            pix = QPixmap(resolved)
            if not pix.isNull():
                self._image_label = QLabel(self)
                self._image_label.setPixmap(pix)
                self._image_label.setScaledContents(True)
                self._image_label.setGeometry(self.rect())
                # 图片也设圆角（避免溢出到 _chat_frame 圆角外）
                self._image_label.setStyleSheet(
                    f"border-radius: {_CHAT_FRAME_RADIUS}px;"
                )
                self._image_label.show()
                # blur 效果挂到 _image_label
                blur = cfg.get("blur", 0)
                if blur and blur > 0:
                    blur_effect = QGraphicsBlurEffect(self._image_label)
                    blur_effect.setBlurRadius(min(float(blur), 30.0))
                    self._image_label.setGraphicsEffect(blur_effect)

        # 3. dim 蒙版（半透明黑色，叠在 image_label 之上压暗）
        dim = cfg.get("dim")
        if dim:
            self._dim_label = QLabel(self)
            self._dim_label.setStyleSheet(
                f"background-color: {dim}; border-radius: {_CHAT_FRAME_RADIUS}px;"
            )
            self._dim_label.setGeometry(self.rect())
            self._dim_label.raise_()
            self._dim_label.show()

        # 4. 整体透明度（不影响 blur effect 的内部计算）
        opacity = cfg.get("opacity", 1.0)
        if opacity < 1.0:
            opacity_effect = QGraphicsOpacityEffect(self)
            opacity_effect.setOpacity(opacity)
            self.setGraphicsEffect(opacity_effect)

        self._sync_geometry()

    def _clear(self) -> None:
        """清除旧 widgets 和 effects"""
        if self._image_label is not None:
            self._image_label.deleteLater()
            self._image_label = None
        if self._dim_label is not None:
            self._dim_label.deleteLater()
            self._dim_label = None
        self.setGraphicsEffect(None)
        self.setStyleSheet("")
        self.setAutoFillBackground(False)

    def _sync_geometry(self) -> None:
        """同步到 parent 的几何"""
        parent = self.parent()
        if parent is None:
            return
        self.setGeometry(parent.rect())
        if self._image_label is not None:
            self._image_label.setGeometry(self.rect())
        if self._dim_label is not None:
            self._dim_label.setGeometry(self.rect())

    def eventFilter(self, watched, event) -> bool:
        """监听 parent.resize → 同步 SceneLayer geometry"""
        if event.type() == QEvent.Resize and watched is self.parent():
            self._sync_geometry()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._image_label is not None:
            self._image_label.setGeometry(self.rect())
        if self._dim_label is not None:
            self._dim_label.setGeometry(self.rect())

    def refresh_theme(self) -> None:
        """theme_manager.refresh_target 钩子：触发重新应用配置

        由调用方通过 host._apply_chat_backgrounds() 触发实际重新加载
        （SceneLayer 自身不直接访问 theme_manager，避免循环依赖）。
        """
        pass