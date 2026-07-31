# -*- coding: utf-8 -*-
"""
DiffViewerCard — 内嵌差异对比卡片，覆盖右侧对话区域（类似系统设置）

替代弹窗式 DiffViewerWindow（QDialog），以系统卡片形式嵌入全局卡片容器，
利用 TabManagerWindow 的覆盖层栈（QStackedWidget）切换对话区/差异面板。

HTML 加载统一走 diff_viewer._load_html_to_webview（临时文件 + setUrl），
规避 Qt setHtml() 对较大内容（实测约 100KB+）无法可靠执行 JS 的问题。
"""

from PyQt5.QtCore import QUrl
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineView

from app.core.webengine_profile import create_transient_web_profile
from app.utils.diff_viewer import _cleanup_temp_files, _load_html_to_webview
from app.widgets.cards.settings.base_settings_card import BaseSettingsCard


class _DiffViewerPage(QWebEnginePage):
    """自定义 QWebEnginePage，拦截 drifox:// 协议以打开文件。"""

    def acceptNavigationRequest(self, url, _type, is_main_frame):
        if url.scheme() == "drifox" and url.host() == "open-file":
            from urllib.parse import parse_qs, unquote

            qs = parse_qs(url.query())
            path = qs.get("path", [None])[0]
            if path:
                path = unquote(path)
                self._open_file(path)
            return False
        return super().acceptNavigationRequest(url, _type, is_main_frame)

    @staticmethod
    def _open_file(path: str):
        import os
        import subprocess
        import sys
        from pathlib import Path

        from loguru import logger

        try:
            p = Path(path)
            if not p.exists():
                logger.warning(f"[DiffViewerCard] 文件不存在: {path}")
                return
            if sys.platform == "win32":
                os.startfile(str(p))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
            logger.info(f"[DiffViewerCard] 已打开文件: {path}")
        except Exception as e:
            logger.error(f"[DiffViewerCard] 打开失败: {path} - {e}")


class DiffViewerCard(BaseSettingsCard):
    """内嵌差异对比卡片，用法类似系统设置面板覆盖右侧对话区域"""

    def __init__(self, parent=None):
        super().__init__("文件差异对比", "📄", parent=parent)
        self.setMinimumHeight(200)
        self.set_height_mode("proportional")
        self._current_html = None
        self._tmp_files = []

        # 构建 WebEngine 视图
        self._webview = QWebEngineView()
        self._profile = create_transient_web_profile(self)
        self._page = _DiffViewerPage(self._profile, self._webview)
        self._webview.setPage(self._page)

        # webview 填满内容区
        self.content_layout.addWidget(self._webview, 1)

    def load_html(self, html_content: str, title: str = "文件差异对比"):
        """加载差异 HTML 并更新卡片标题

        Args:
            html_content: DiffHtmlGenerator 生成的完整 HTML 报告
            title: 卡片标题
        """
        self._current_html = html_content
        self.set_title_text(title)
        # 替换旧内容前先清理上一份临时文件，避免残留
        _cleanup_temp_files(self._tmp_files)
        _load_html_to_webview(self._webview, html_content or "", self._tmp_files)

    def clear(self):
        """清除内容，释放 WebEngine 页面与临时文件"""
        self._current_html = None
        self._webview.setHtml("")
        _cleanup_temp_files(self._tmp_files)
