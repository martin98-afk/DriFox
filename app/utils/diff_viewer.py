# -*- coding: utf-8 -*-
"""
Git Diff 差异对比模块 - Cursor/VS Code 风格差异审查工具（Web 侧）
特性:
  1. 并排对比（Split View）- 旧版/新版左右对照，同一行严格对齐
  2. 统一直列（Unified View）- 删除块→新增块 成片展示，不逐行配对
  3. 一键切换视图模式
  4. 自动合并相邻 hunk（间距 ≤ 5 行）
  5. 点击文件路径 → 系统编辑器打开
  6. 行内词级差异高亮
  7. 上下文智能折叠（连续未修改行自动收起）
  8. 深浅主题自动适配（跟随应用当前主题；generate_html_report 支持 light 参数显式指定）

纯 HTML 生成面（DiffHtmlGenerator / ToolPayloadHtmlGenerator / 主题 CSS 等）
已拆至 app/utils/diff_html.py（零 Qt 依赖），本模块只保留 QtWebEngine 相关部分。
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from loguru import logger
from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineView
from PyQt5.QtWidgets import QDialog, QHBoxLayout

from app.core.infra.webengine_profile import create_transient_web_profile

try:
    import psutil
except Exception:
    psutil = None

def _get_rss_mb() -> Optional[float]:
    if psutil is None:
        return None
    try:
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return None

# re-export 纯 HTML 面符号，保持存量引用与 tests 兼容（渲染逻辑见 diff_html.py）
from app.utils.diff_html import DiffHtmlGenerator, ToolPayloadHtmlGenerator



# ==========================================================================
# 5. WebView HTML 加载辅助（规避 setHtml 大内容限制）
# ==========================================================================
# Qt 的 setHtml() 对较大内容（实测约 100KB+ 即可能失败：页面能显示但 JS
# 不执行，或 loadFinished 不触发）不可靠。差异报告（大 diff 经 Pygments
# 高亮后膨胀数倍）很容易超限，表现为按钮回调报 "openFile/switchView is
# not defined"、文件列表点击无响应。统一改用临时文件 + setUrl 加载，
# 无大小限制且行为一致。
def _write_temp_html(html: str) -> Optional[str]:
    """把 HTML 写入系统临时文件，返回路径；失败返回 None"""
    try:
        fd, path = tempfile.mkstemp(suffix=".html", prefix="drifox_diff_", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
        return path
    except Exception as e:
        logger.warning(f"[DiffViewer] 写入临时 HTML 失败: {e}")
        return None


def _cleanup_temp_files(tmp_files: Optional[List[str]]) -> None:
    """清理临时 HTML 文件列表"""
    if not tmp_files:
        return
    for p in tmp_files[:]:
        try:
            os.unlink(p)
        except Exception:
            pass
    tmp_files.clear()


def _load_html_to_webview(webview: QWebEngineView, html_content: str, tmp_files: Optional[List[str]] = None) -> None:
    """把差异 HTML 加载进 webview（临时文件 + setUrl，规避 setHtml 大小限制）

    Args:
        webview: 目标 QWebEngineView
        html_content: 完整 HTML 报告
        tmp_files: 调用方持有的临时文件列表（追加新文件、供后续清理）；为 None 时使用 setHtml
    """
    html = html_content or ""
    if tmp_files is not None:
        path = _write_temp_html(html)
        if path is not None:
            tmp_files.append(path)
            webview.setUrl(QUrl.fromLocalFile(path))
            return
    # 临时文件写入失败（或调用方不管理临时文件）→ 回退 setHtml
    webview.setHtml(html)


# ==========================================================================
# 6. DiffViewerWindow（弹窗回退实现）
# ==========================================================================
class _DiffWebPage(QWebEnginePage):
    """自定义 QWebEnginePage，拦截 drifox:// 协议以打开文件。"""

    def acceptNavigationRequest(self, url: QUrl, _type, is_main_frame):
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
        try:
            p = Path(path)
            if not p.exists():
                logger.warning(f"[DiffViewer] 文件不存在: {path}")
                return
            if sys.platform == "win32":
                os.startfile(str(p))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
            logger.info(f"[DiffViewer] 已打开文件: {path}")
        except Exception as e:
            logger.error(f"[DiffViewer] 打开失败: {path} - {e}")


class DiffViewerWindow:
    _instances = []


    def __init__(self, parent=None, title: str = "文件差异对比"):
        self._disposed = False
        self._current_html = None
        self._tmp_files: List[str] = []
        self._window = QDialog(parent)
        self._window.setWindowTitle(title)
        self._window.resize(1300, 850)
        self._window.setAttribute(Qt.WA_DeleteOnClose, True)
        if parent:
            self._window.setWindowFlags(self._window.windowFlags() | Qt.WindowModal)
        layout = QHBoxLayout(self._window)
        layout.setContentsMargins(0, 0, 0, 0)
        self._webview = QWebEngineView()
        self._profile = create_transient_web_profile(self._window)
        self._page = _DiffWebPage(self._profile, self._webview)
        self._webview.setPage(self._page)
        layout.addWidget(self._webview)
        self._window.destroyed.connect(lambda: self._on_closed())
        self._instances.append(self)
        rss_mb = _get_rss_mb()
        if rss_mb is None:
            logger.debug(f"[DiffViewer] init title={title}")
        else:
            logger.debug(f"[DiffViewer] init title={title}, rss={rss_mb:.1f}MB")

    def _clear_web_content(self):
        if not hasattr(self, "_webview") or self._webview is None:
            return

        try:
            page = self._webview.page()
            if page:
                try:
                    page.runJavaScript("delete window._dm;delete window._lf;")
                except Exception:
                    pass
        except Exception:
            pass

        try:
            self._webview.setHtml("")
        except Exception:
            pass

        # 清理临时 HTML 文件
        _cleanup_temp_files(getattr(self, "_tmp_files", None))

    def _log_rss(self, stage: str):
        rss_mb = _get_rss_mb()
        if rss_mb is None:
            logger.debug(f"[DiffViewer] {stage}")
        else:
            logger.debug(f"[DiffViewer] {stage}, rss={rss_mb:.1f}MB")

    def _dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._log_rss("dispose_begin")
        self._clear_web_content()
        self._current_html = None
        if self in self._instances:
            self._instances.remove(self)

        try:
            if hasattr(self, "_webview") and self._webview is not None:
                self._webview.deleteLater()
        except Exception:
            pass
        try:
            if hasattr(self, "_profile") and self._profile is not None:
                self._profile.deleteLater()
        except Exception:
            pass
        try:
            if hasattr(self, "_window") and self._window is not None:
                self._window.deleteLater()
        except Exception:
            pass
        self._page = None
        self._profile = None
        self._webview = None
        self._window = None
        self._log_rss("dispose_end")

    def _on_closed(self):
        self._dispose()

    def load_html(self, html_content: str):
        html_len = len(html_content or "")
        self._log_rss(f"load_html_begin html_len={html_len}")
        _load_html_to_webview(self._webview, html_content or "", self._tmp_files)
        self._current_html = None
        self._log_rss(f"load_html_called html_len={html_len}")
        QTimer.singleShot(1000, lambda: self._log_rss(f"load_html_post_1s html_len={html_len}"))

    def show(self):
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def close(self):
        try:
            self._current_html = None
            self._clear_web_content()
            if self._window is not None:
                self._window.close()
            else:
                self._dispose()
        except Exception:
            pass

    @property
    def widget(self):
        return self._window
