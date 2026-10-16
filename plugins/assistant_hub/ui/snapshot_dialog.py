# -*- coding: utf-8 -*-
"""snapshot_dialog.py — 记忆快照恢复弹窗（浏览 / 预览 / 恢复）。

恢复本身可反悔：恢复前当前版自动入快照（store.restore_snapshot 语义），
故不设二次确认，底部以固定提示说明。
"""

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css


def _muted_style() -> str:
    return (
        f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
        f"{get_font_family_css()} {font_size_css(10)};"
    )


class SnapshotDialog(QDialog):
    """记忆快照恢复：顶部切换全局/项目，左列表右预览，一键恢复。"""

    restored = pyqtSignal()

    def __init__(self, mgr: Any, aid: str, workdir: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._mgr = mgr
        self._aid = aid
        self._wd = workdir or ""
        self.setWindowTitle("恢复记忆快照")
        self.resize(680, 460)

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self._kind = QComboBox()
        self._kind.addItem("全局记忆", "global")
        self._kind.addItem("项目记忆", "project")
        if not self._wd:
            self._kind.model().item(1).setEnabled(False)  # 无工作目录：项目侧禁用
        self._kind.currentIndexChanged.connect(lambda _: self._reload())
        top.addWidget(self._kind, 1)
        v.addLayout(top)

        body = QHBoxLayout()
        self._list = QListWidget()
        self._list.setMinimumWidth(230)
        self._list.currentRowChanged.connect(self._on_row)
        body.addWidget(self._list, 2)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setPlaceholderText("选中左侧快照预览内容")
        body.addWidget(self._preview, 3)
        v.addLayout(body, 1)

        bottom = QHBoxLayout()
        tip = QLabel("恢复前当前版本自动存为快照，可再次恢复返回")
        tip.setStyleSheet(_muted_style())
        bottom.addWidget(tip, 1)
        self._restore_btn = QPushButton("恢复此版本")
        self._restore_btn.setEnabled(False)
        self._restore_btn.clicked.connect(self._on_restore)
        bottom.addWidget(self._restore_btn)
        v.addLayout(bottom)

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
