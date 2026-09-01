# -*- coding: utf-8 -*-
"""avatar_picker.py — 头像选择组件

三层选择：
1. **预置剪影库**：18 个不同风格 / 颜色的 SVG 头像（打包在 icons/avatars/）
2. **彩色色块**：DriFox _SquareAvatar 同款（按 initials 着色）
3. **本地上传**：从磁盘选择 png/jpg/webp

单一 ResultPanel：左侧"当前头像预览"，右侧"选择面板"（三 Tab）。
"""
from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, ToolButton, TransparentToolButton

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

from .assistant_avatar import RoundAvatar


_PREDEFINED_AVATAR_DIR = Path(__file__).resolve().parent.parent / "icons" / "avatars"


# ────────────────────────────────────────────────────────────────────
# 单个头像按钮（54×54，自适应缩放）
# ────────────────────────────────────────────────────────────────────


class _AvatarTile(QToolButton):
    """单个头像选项（用于网格），checked 高亮一圈"""

    def __init__(self, name: str, image_path: Optional[Path], color: str, parent=None):
        super().__init__(parent)
        self._name = name
        self._image_path = image_path
        self._color = color
        self.setCheckable(True)
        self.setFixedSize(64, 64)
        self.setIconSize(QSize(48, 48))
        self.setToolTip(name)
        self.setStyleSheet(
            f"""
            QToolButton {{
                background: transparent;
                padding: 6px;
                border: 2px solid transparent;
                border-radius: 8px;
            }}
            QToolButton:hover {{
                background: {Colors.HOVER_BG};
            }}
            QToolButton:checked {{
                border-color: {Colors.INFO};
                background: {Colors.SELECTED_BG.format(alpha=180) if isinstance(Colors.SELECTED_BG, str) else Colors.SELECTED_BG};
            }}
        """
        )
        # 计算图标 pixmap
        if image_path and image_path.exists():
            if image_path.suffix.lower() == ".svg":
                pm = _render_svg(str(image_path), 48, color)
                if pm is None:
                    pm = _fallback_pixmap(name, color, 48)
            else:
                pm = QPixmap(str(image_path)).scaled(
                    48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            self.setIcon(QIcon(pm))
        else:
            pm = _fallback_pixmap(name, color, 48)
            self.setIcon(QIcon(pm))

    def get_selection(self) -> dict:
        """返回选中信号携带的字典"""
        return {
            "kind": "predefined" if self._image_path else "color",
            "name": self._name,
            "image_path": str(self._image_path) if self._image_path else "",
            "color": self._color,
        }


def _render_svg(svg_path: str, size: int, color: str) -> Optional[QPixmap]:
    """渲染 SVG 为 QPixmap；把 stroke 颜色替换成传入的 color 简单覆盖"""
    try:
        text = Path(svg_path).read_text(encoding="utf-8")
        # 简单染色：把 fill='currentColor' 替换为目标色
        text = text.replace('fill="currentColor"', f'fill="{color}"')
        text = text.replace("fill='currentColor'", f"fill='{color}'")
        text = text.replace("stroke='currentColor'", f"stroke='{color}'")
        text = text.replace('stroke="currentColor"', f'stroke="{color}"')
        # 替换常见灰阶 stroke
        for g in ('stroke="#E8E8E8"', "stroke='#E8E8E8'"):
            text = text.replace(g, f'stroke="{color}"')
        for g in ('stroke="#202020"', "stroke='#202020'"):
            text = text.replace(g, f'stroke="{color}"')
        renderer = QSvgRenderer(text.encode("utf-8"))
        if not renderer.isValid():
            return None
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        renderer.render(p)
        p.end()
        return pix
    except Exception:
        return None


def _fallback_pixmap(text: str, color: str, size: int) -> QPixmap:
    """色块 + 缩写 pixmap（用作网格小图）"""
    widget = RoundAvatar(size=size, text=text, color=color)
    return widget.grab()


# ────────────────────────────────────────────────────────────────────
# 头像面板
# ────────────────────────────────────────────────────────────────────


class AvatarPicker(QWidget):
    """三栏头像面板：预置 / 上传 / 纯色"""

    avatarSelected = pyqtSignal(dict)  # {"kind", "name", "image_path", "color"}

    def __init__(self, assistant_id: str = "", parent=None):
        super().__init__(parent)
        self._assistant_id = assistant_id
        self._current_color = "#7C3AED"
        self._current_name = ""
        self._current_image = ""
        self._init_ui()

    def set_assistant(self, aid: str, color: str, name: str, image_path: str) -> None:
        self._assistant_id = aid
        self._current_color = color
        self._current_name = name
        self._current_image = image_path
        self._refresh_preview()
        # 选中项高亮
        for i, tile in enumerate(self._predefined_tiles):
            if tile._image_path and image_path and str(tile._image_path) == image_path:
                tile.setChecked(True)
            else:
                tile.setChecked(False)

    def _init_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # 左：当前预览
        preview_box = QFrame(self)
        preview_box.setStyleSheet(
            f"""
            QFrame {{
                background: {Colors.CARD_BG.format(alpha=180) if isinstance(Colors.CARD_BG, str) else Colors.CARD_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """
        )
        pv = QVBoxLayout(preview_box)
        pv.setContentsMargins(12, 12, 12, 12)
        pv.setSpacing(8)
        self._preview_label = QLabel("当前头像", preview_box)
        self._preview_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}"
        )
        pv.addWidget(self._preview_label)
        self._preview_widget = RoundAvatar(size=96, text="?", color=self._current_color)
        pv.addWidget(self._preview_widget, 0, Qt.AlignCenter)
        self._preview_name_label = QLabel("—", preview_box)
        self._preview_name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}"
        )
        self._preview_name_label.setAlignment(Qt.AlignCenter)
        pv.addWidget(self._preview_name_label, 0, Qt.AlignCenter)

        outer.addWidget(preview_box, 0, Qt.AlignTop)

        # 右：选择面板
        right = QFrame(self)
        right.setStyleSheet(
            f"""
            QFrame {{
                background: {Colors.CARD_BG.format(alpha=180) if isinstance(Colors.CARD_BG, str) else Colors.CARD_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """
        )
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 8, 8, 8)
        rv.setSpacing(6)

        tabs = QTabWidget(right)
        tabs.setDocumentMode(True)
        # 标签 1：预置剪影
        tabs.addTab(self._build_predefined_tab(), "🎭 预置剪影")
        # 标签 2：纯色色块
        tabs.addTab(self._build_color_tab(), "🎨 纯色")
        # 标签 3：本地上传
        upload_tab = QWidget()
        ul = QVBoxLayout(upload_tab)
        ul.setContentsMargins(8, 8, 8, 8)
        ul.setSpacing(8)

        up_btn = QPushButton(FluentIcon.FOLDER.icon(), "选择本地图片", upload_tab)
        up_btn.setFixedHeight(36)
        up_btn.clicked.connect(self._on_upload)
        ul.addWidget(up_btn)

        up_info = QLabel("支持 PNG / JPG / WebP / SVG。可上传多张，会自动覆盖旧头像。", upload_tab)
        up_info.setWordWrap(True)
        up_info.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}"
        )
        ul.addWidget(up_info)
        ul.addStretch()
        tabs.addTab(upload_tab, "📁 本地")

        rv.addWidget(tabs)
        outer.addWidget(right, 1)

    # ── 预置 ──

    def _build_predefined_tab(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        info = QLabel("预置头像库 — 选中即应用到当前助手（可放更多图片到插件 icons/avatars/）", wrap)
        info.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}"
        )
        v.addWidget(info)

        scroll = QScrollArea(wrap)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(4)

        # 优先读 icons/avatars/*.svg，没有则用纯色 18 个
        avatars = self._list_predefined()
        self._predefined_tiles = []
        for i, item in enumerate(avatars):
            tile = _AvatarTile(item["name"], item.get("image_path"), item["color"])
            tile.clicked.connect(lambda checked=False, t=tile: self._on_predefined_clicked(t))
            grid.addWidget(tile, i // 6, i % 6)
            self._predefined_tiles.append(tile)
        grid.setRowStretch(grid.rowCount(), 1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        return wrap

    def _list_predefined(self) -> list:
        """扫描 icons/avatars/ 目录（png/jpg/svg/webp 全部收录），不足 18 个补色块"""
        out = []
        files: list[Path] = []
        if _PREDEFINED_AVATAR_DIR.exists():
            for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.svg"):
                files.extend(sorted(_PREDEFINED_AVATAR_DIR.glob(ext)))
        palette = [
            ("#7C3AED", "紫"), ("#DB2777", "粉"), ("#DC2626", "朱"),
            ("#EA580C", "橙"), ("#D97706", "琥"), ("#CA8A04", "黄"),
            ("#65A30D", "苔"), ("#16A34A", "翠"), ("#059669", "碧"),
            ("#0891B2", "青"), ("#0284C7", "空"), ("#2563EB", "蓝"),
            ("#4F46E5", "群"), ("#6D28D9", "萄"), ("#9333EA", "紫"),
            ("#475569", "灰"), ("#0F766E", "tide"), ("#A21CAF", "fuchsia"),
        ]

        for i, f in enumerate(files[:24]):
            color = palette[i % len(palette)][0]
            out.append(
                {
                    "name": f.stem,
                    "image_path": f,
                    "color": color,
                }
            )
        # 不足 18 个补色块
        while len(out) < 18:
            i = len(out)
            color, name = palette[i % len(palette)]
            out.append({"name": f"色块-{name}", "image_path": None, "color": color})
        return out[:24]

    def _build_color_tab(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        info = QLabel("选一个纯色作为头像背景 + 缩写字符（按姓名生成）", wrap)
        info.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}"
        )
        v.addWidget(info)

        scroll = QScrollArea(wrap)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(4)

        palette = [
            "#7C3AED", "#DB2777", "#DC2626", "#EA580C", "#D97706", "#CA8A04",
            "#65A30D", "#16A34A", "#059669", "#0891B2", "#0284C7", "#2563EB",
            "#4F46E5", "#6D28D9", "#9333EA", "#475569", "#0F766E", "#A21CAF",
        ]
        self._color_tiles = []
        for i, color in enumerate(palette):
            tile = _AvatarTile(f"色块-{i+1}", None, color)
            tile.clicked.connect(lambda checked=False, t=tile: self._on_color_clicked(t))
            grid.addWidget(tile, i // 6, i % 6)
            self._color_tiles.append(tile)
        grid.setRowStretch(grid.rowCount(), 1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        return wrap

    # ── 选择回调 ──

    def _on_predefined_clicked(self, tile: _AvatarTile) -> None:
        # 取消其它 tile 选中态
        for t in self._predefined_tiles:
            if t is not tile:
                t.setChecked(False)
        for t in self._color_tiles:
            t.setChecked(False)
        tile.setChecked(True)
        sel = tile.get_selection()
        sel["kind"] = "predefined"
        # 直接应用
        self._apply_selection(
            name=sel["name"],
            color=sel["color"],
            image_path=sel.get("image_path") or "",
        )
        self.avatarSelected.emit(sel)

    def _on_color_clicked(self, tile: _AvatarTile) -> None:
        for t in self._color_tiles:
            if t is not tile:
                t.setChecked(False)
        for t in self._predefined_tiles:
            t.setChecked(False)
        tile.setChecked(True)
        sel = tile.get_selection()
        sel["kind"] = "color"
        self._apply_selection(
            name=self._current_name or tile._name,
            color=sel["color"],
            image_path="",
        )
        self.avatarSelected.emit(sel)

    def _on_upload(self) -> None:
        if not self._assistant_id:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择头像",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.svg)",
        )
        if not path:
            return
        # 拷贝到 assistant 头像目录
        try:
            from assistant_hub_manager import AssistantManager

            mgr = AssistantManager.get_instance()
            ext = Path(path).suffix.lstrip(".").lower()
            if ext == "jpeg":
                ext = "jpg"
            data = Path(path).read_bytes()
            saved = mgr.save_avatar_from_bytes(self._assistant_id, data, ext)
            if saved:
                image_path = str(saved)
                self._apply_selection(
                    name=self._current_name,
                    color=self._current_color,
                    image_path=image_path,
                )
                self.avatarSelected.emit(
                    {
                        "kind": "uploaded",
                        "name": self._current_name,
                        "image_path": image_path,
                        "color": self._current_color,
                    }
                )
        except Exception as e:
            from loguru import logger

            logger.warning(f"[assistant_hub] 上传头像失败: {e}")

    # ── 应用 ──

    def _apply_selection(self, name: str, color: str, image_path: str) -> None:
        self._current_name = name
        self._current_color = color
        self._current_image = image_path
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        old = self._preview_widget
        self._preview_widget = RoundAvatar(
            size=96,
            text=self._current_name or "?",
            color=self._current_color,
            image_path=self._current_image or None,
        )
        layout = old.parent().layout()
        idx = layout.indexOf(old)
        layout.removeWidget(old)
        old.deleteLater()
        layout.insertWidget(idx, self._preview_widget, 0, Qt.AlignCenter)
        self._preview_name_label.setText(self._current_name or "(未命名)")
