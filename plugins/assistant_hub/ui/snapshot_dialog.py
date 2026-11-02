# -*- coding: utf-8 -*-
"""snapshot_dialog.py — 记忆快照恢复浮层（浏览 / 预览 / 恢复）。

继承 OverlayBase（遮罩 + 圆角卡片 + 标题栏，与查看记忆/上限编辑同款视觉）。
恢复本身可反悔：恢复前当前版自动入快照（store.restore_snapshot 语义），
故不设二次确认，底部以固定提示说明。
"""

from typing import Any, Dict, List

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QListWidget, QPlainTextEdit

from qfluentwidgets import ComboBox, SmoothScrollDelegate

from app.plugins.sdk import Colors, font_size_css
from app.plugins.sdk import get_font_family_css

from .overlays import OverlayBase, _dialog_btn


def _panel_qss() -> str:
    """列表/预览面板同款外观：弱背景 + 细边框 + 圆角（对齐 TextViewOverlay）。"""
    return f"""
        QPlainTextEdit, QListWidget {{
            background: {Colors.CARD_BG.format(alpha=120)};
            border: 1px solid {Colors.BORDER};
            border-radius: 8px;
            color: {Colors.TEXT_PRIMARY};
            {get_font_family_css()} {font_size_css(12)};
            padding: 6px;
        }}
        QListWidget::item {{ padding: 6px 8px; border: none; border-radius: 6px; }}
        QListWidget::item:selected {{ background: {Colors.HOVER_BG}; }}
    """


class SnapshotDialog(OverlayBase):
    """记忆快照恢复：顶部切换全局/项目，左列表右预览，一键恢复。"""

    restored = pyqtSignal()

    def __init__(self, mgr: Any, aid: str, workdir: str = "", parent=None):
        super().__init__("恢复记忆快照", parent, width=660, height=470)
        self._mgr = mgr
        self._aid = aid
        self._wd = workdir or ""

        top = QHBoxLayout()
        self._kind = ComboBox(self._wrap)
        self._kind.addItem("全局记忆", userData="global")
        if self._wd:  # 无工作目录时不放项目项：项目快照随 workdir 分组，缺 workdir 无从读取
            self._kind.addItem("项目记忆", userData="project")
        self._kind.currentIndexChanged.connect(lambda _: self._reload())
        top.addWidget(self._kind, 1)
        self._v.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(8)
        self._list = QListWidget(self._wrap)
        self._list.setMinimumWidth(240)
        self._list.currentRowChanged.connect(self._on_row)
        self._list.setStyleSheet(_panel_qss())
        SmoothScrollDelegate(self._list)
        body.addWidget(self._list, 2)
        self._preview = QPlainTextEdit(self._wrap)
        self._preview.setReadOnly(True)
        self._preview.setPlaceholderText("选中左侧快照预览内容")
        self._preview.setStyleSheet(_panel_qss())
        SmoothScrollDelegate(self._preview)
        body.addWidget(self._preview, 3)
        self._v.addLayout(body, 1)

        bottom = QHBoxLayout()
        tip = QLabel("恢复前当前版本自动存为快照，可再次恢复返回", self._wrap)
        tip.setWordWrap(True)
        tip.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        bottom.addWidget(tip, 1)
        self._restore_btn = _dialog_btn("恢复此版本", primary=True)
        self._restore_btn.setEnabled(False)
        self._restore_btn.clicked.connect(self._on_restore)
        bottom.addWidget(self._restore_btn)
        close = _dialog_btn("关闭")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        self._v.addLayout(bottom)

        self._snaps: List[Dict[str, Any]] = []
        self._reload()

    # ── 数据 ──

    def _kind_value(self) -> str:
        return str(self._kind.currentData() or "global")

    def _reload(self) -> None:
        kind = self._kind_value()
        try:
            self._snaps = list(self._mgr.memory_snapshots(self._aid, kind, self._wd))
        except Exception:
            self._snaps = []
        self._list.clear()
        for s in self._snaps:
            self._list.addItem(f"{s.get('ts', '')} · {int(s.get('chars') or 0)} 字")
        self._preview.setPlainText("（暂无快照）" if not self._snaps else "")
        self._restore_btn.setEnabled(False)

    def _on_row(self, row: int) -> None:
        if row < 0 or row >= len(self._snaps):
            self._restore_btn.setEnabled(False)
            return
        name = str(self._snaps[row].get("file") or "")
        try:
            self._preview.setPlainText(self._mgr.read_memory_snapshot(self._aid, self._kind_value(), name, self._wd))
        except Exception as e:
            self._preview.setPlainText(f"（读取失败: {e}）")
        self._restore_btn.setEnabled(True)

    def _on_restore(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._snaps):
            return
        name = str(self._snaps[row].get("file") or "")
        try:
            r = self._mgr.restore_memory_snapshot(self._aid, self._kind_value(), name, self._wd)
        except Exception as e:
            self._preview.setPlainText(f"（恢复失败: {e}）")
            return
        if not r.get("ok"):
            self._preview.setPlainText(f"（恢复失败: {r.get('error')}）")
            return
        self.restored.emit()
        self._reload()
