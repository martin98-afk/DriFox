# -*- coding: utf-8 -*-
"""BranchChip — 标题栏分支标签 + 后台检测任务（迁移自 title_bar_module / main_widget）

★ 缺陷修复（随迁移落地，回归锚点见 tests/plugins/test_branch_chip.py）：
1. 检测失败（ok=False）不写缓存，杜绝「偶发失败 → 空结果进缓存 → 标签永久消失」
2. finished 信号携带任务 workdir，写对缓存槽 + 上屏前校验当前 workdir
3. 复制窗口继承时若源处于检测中（隐藏态），200ms 后兜底重检（见 service.copy_branch_state）
"""
from __future__ import annotations

import os
import subprocess
import sys

from loguru import logger
from PyQt5.QtCore import QObject, QRunnable, Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy

from app.utils.git_worktree import GitWorktreeDetector
from app.utils.utils import get_icon

_CREATION_FLAGS = 0
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NO_WINDOW


class BranchDetectSignals(QObject):
    """后台检测信号桥（后台线程 → 主线程）。

    ★ 修复缺陷2：携带任务 workdir；★ 修复缺陷1：ok=False=检测失败（区别于合法空分支）。
    """

    finished = pyqtSignal(int, str, str, bool)  # request_id, workdir, branch, ok


class BranchDetectTask(QRunnable):
    """异步 git 分支检测 worker（迁移自 main_widget._BranchDetectTask）。

    与旧实现差异：returncode==0 → (branch, True)；任何失败/异常 → ("", False)。
    旧实现所有失败一律返回空串且无差别写缓存，是「标签经常消失」的主因。
    """

    def __init__(self, workdir: str, request_id: int, signals: BranchDetectSignals):
        super().__init__()
        self._workdir = workdir
        self._request_id = request_id
        self._signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        branch, ok = "", False
        try:
            if self._workdir and os.path.isdir(self._workdir):
                git_root = GitWorktreeDetector.detect_git(self._workdir)
                if git_root:
                    r = subprocess.run(
                        ["git", "branch", "--show-current"],
                        capture_output=True,
                        text=True,
                        cwd=self._workdir,
                        timeout=3,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=_CREATION_FLAGS,
                    )
                    if r.returncode == 0:
                        branch, ok = r.stdout.strip(), True  # detached HEAD 合法空 → ok=True
        except Exception:
            logger.debug(f"[BranchChip] git 分支检测失败: {self._workdir}")
        try:
            self._signals.finished.emit(self._request_id, self._workdir, branch, ok)
        except Exception:
            pass  # 窗口销毁时 signals 已 GC，直接丢弃


class ThemeIconLabel(QLabel):
    """主题感知图标标签 — 不缓存 pixmap 快照。

    QLabel.setPixmap(icon.pixmap()) 会把当前主题的像素快照固化下来，主题切换后图标不刷新。
    本类改为持有 QIcon，每次 paintEvent 都重新取色，
    与项目里 TransparentToolButton(get_icon(...)) 的行为一致。
    （迁移自 title_bar_module._ThemeIconLabel）
    """

    def __init__(self, icon_name: str, size: int = 12, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._size = size
        self.setFixedSize(size, size)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, event):
        from PyQt5.QtGui import QPainter

        p = QPainter(self)
        get_icon(self._icon_name).paint(p, self.rect(), Qt.AlignCenter)
        p.end()


class BranchChip(QFrame):
    """Git 分支 chip — git 分支线稿 icon + 分支名 + hover 底色。

    设计目标：「一眼看出是工作树」—— git 分支线稿 icon 让用户立即知道这是
    git 分支而不是普通文字。QFrame 自绘，setText/text() 接口与原 PushButton 兼容
    （主程序 _refresh_branch_widget_style 门面仍调用 refresh_style）。

    迁移自 title_bar_module._BranchChip；检测状态（request_id/signals）
    挂在本 widget 上随窗口销毁回收，检测/缓存逻辑在 WorktreeService。
    """

    def __init__(self, text: str = "main", parent=None):
        super().__init__(parent)
        self.setObjectName("_branchWidget")
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(5, 2, 7, 2)
        lay.setSpacing(4)

        # git 分支线稿 icon（12px，主题感知：浅色 #333 / 深色 #fff）
        # 用 ThemeIconLabel 而非 setPixmap 快照，保证深浅主题切换后自动重取色
        self._icon_label = ThemeIconLabel("分支", 12, self)
        self._icon_label.setObjectName("_branchIcon")
        lay.addWidget(self._icon_label)

        # 文字
        self._text_label = QLabel(text, self)
        self._text_label.setObjectName("_branchText")
        self._text_label.setStyleSheet("background: transparent;")
        lay.addWidget(self._text_label)

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        # 检测状态（per-window：widget 随窗口销毁，状态随之回收）
        self._detect_signals: BranchDetectSignals | None = None
        self._detect_request_id = 0

        self._apply_style()

    def text(self) -> str:
        return self._text_label.text()

    def setText(self, t: str) -> None:
        self._text_label.setText(t)
        self._text_label.adjustSize()
        self.adjustSize()

    def _apply_style(self) -> None:
        from app.utils.design_tokens import Colors, font_size_css
        from app.utils.utils import get_font_family_css

        Colors.refresh()
        sheet = f"""
            #_branchWidget {{
                background: transparent;
                border: none;
                border-radius: 4px;
            }}
            #_branchWidget:hover {{
                background: {Colors.HOVER_BG};
            }}
            #_branchText {{
                color: {Colors.TEXT_SECONDARY};
                {get_font_family_css()}
                {font_size_css(12)};
            }}
            #_branchWidget:hover #_branchText {{
                color: {Colors.TEXT_PRIMARY};
            }}
        """
        # 批2：样式串缓存守卫——串未变化（重复刷新链）跳过 setStyleSheet
        if getattr(self, "_last_sheet", None) == sheet:
            return
        self._last_sheet = sheet
        self.setStyleSheet(sheet)
        # 主题切换时强制重绘 icon（ThemeIconLabel 的 paintEvent 会重新取色）
        self._icon_label.update()

    def refresh_style(self) -> None:
        """主程序 _refresh_branch_widget_style 门面调用入口（保持向后兼容）"""
        self._apply_style()

    def on_branch_detected(self, request_id: int, workdir: str, branch: str, ok: bool) -> None:
        """信号槽：转发宿主窗口 → WorktreeService.on_branch_detected。"""
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        mw = self._host()
        if mw is None:
            return
        svc = UIPluginRegistry.get_instance().get_service("worktree")
        if svc is not None:
            svc.on_branch_detected(mw, request_id, workdir, branch, ok)

    def _host(self):
        """向上找宿主窗口（持有 _resolve_project_workdir 的 MainWidget）。"""
        p = self.parent()
        while p is not None and not hasattr(p, "_resolve_project_workdir"):
            p = p.parent()
        return p

    def mousePressEvent(self, event) -> None:
        """点击打开工作台工作树页（原 main_widget._on_branch_label_clicked 迁入）。"""
        event.accept()
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None and hasattr(tm, "open_workbench_memory"):
                tm.open_workbench_memory("docs")
        except Exception:
            logger.exception("[BranchChip] 打开工作树页失败")
