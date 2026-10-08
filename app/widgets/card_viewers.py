# -*- coding: utf-8 -*-
"""消息卡片查看器组件（自 message_card.py 拆出，纯搬移）。

ConsoleMonitorPage / _DialogEventFilter / CodeWebViewer /
PlainTextViewer / _ImagePreviewDialog 及图片预览辅助函数。
"""

import base64
import contextlib
import math
import os
import time
import re
import urllib.parse
import weakref
from datetime import datetime
from html import escape, unescape
from typing import Any, Dict, List, Optional
import orjson as json
import sip
from loguru import logger
from markdown import Markdown
from PyQt5.QtCore import (
    QByteArray,
    QEasingCurve,
    QElapsedTimer,
    QObject,
    QRect,
    QSize,
    QThread,
    Qt,
    QTimer,
    QTimerEvent,
    QUrl,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextDocument,
    QWheelEvent,
)
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineSettings, QWebEngineView
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    MaskDialogBase,
    TransparentToolButton,
)
from qfluentwidgets.components.widgets.scroll_bar import SmoothScrollDelegate
from app.core.infra.webengine_profile import get_shared_web_profile
from app.utils.design_tokens import (
    Animations,
    BorderRadius,
    Colors,
    _get_global_font,
    current_theme,
    fade_in_widget,
    font_size_css,
    get_unified_scrollbar_style,
    scale_font_size,
    scale_icon_size,
)
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.render_crash_queue import RenderCrashQueue
from app.widgets.card_render_core import (
    ACTION_COLOR_MAP,
    AUTO_SCROLL_THRESHOLD,
    FINISH_TIMING_ENABLED,
    INCREMENTAL_FINALIZE_ENABLED,
    RenderCrashQueue,
    _BORDER_RADIUS_CSS_VARS,
    _BUILTIN_FENCE_PERMS,
    _CHAR_COUNT_HTML,
    _CODE_FONT_SIZE,
    _CONTENT_AUTOSCROLL_JS,
    _FLIP_JS,
    _HISTORY_TOOL_CAPS,
    _ICON_PREFIX_CACHE,
    _MAX_CHART_PAYLOAD_B64,
    _PREVIEW_TYPEWRITER_JS,
    _PROJECT_ROOT,
    _RENDER_POOL,
    _RESET_CONTENT_FOR_REUSE_JS,
    _SENTENCE_END_CHARS,
    _SKELETON_CACHE_MAX,
    _SKELETON_CACHE_VERSION,
    _STREAMING_DOCK_CSS,
    _STREAMING_DOCK_JS,
    _TYPEWRITER_JS,
    _accent_rgba,
    _defer_emit,
    _dispatch_render_done,
    _extract_closed_segments,
    _extract_fenced_code,
    _extract_formulas,
    _get_echarts_vendor_urls,
    _get_katex_urls,
    _get_mermaid_vendor_urls,
    _has_unclosed_chart_fence,
    _has_unclosed_registered_tag,
    _has_unclosed_think,
    _has_unclosed_think_or_tool,
    _inject_context_links,
    _inject_hook_blocks,
    _inject_tag_cards,
    _inject_think_cards,
    _inject_tool_blocks,
    _iter_think_segments,
    _js_literal,
    _last_para_break_outside_fence,
    _mmd_theme_vars_js,
    _render_inline_tail,
    _render_markdown_to_html_cached,
    _render_markdown_to_html_worker,
    _render_stable_segment,
    _render_think_block,
    _resolve_image_src,
    _restore_fenced_code,
    _sanitize_incomplete_markdown,
    _skeleton_cache,
    _tail_before_unclosed_block,
    _unwrap_code_blocks_with_context_links,
    _update_icon_prefix,
    _wrap_code_blocks_with_copy_button_web,
    clear_global_render_cache,
    get_font_family_css,
    get_icon,
    get_markdown_instance,
    set_pygments_style,
)


class ConsoleMonitorPage(QWebEnginePage):
    codeActionRequested = pyqtSignal(str, str)
    contextActionRequested = pyqtSignal(str, str)
    heightReported = pyqtSignal(int)
    # 🐛 卡片内阅读标志：reportHeight 第 4 字段翻转时推送（见 javaScriptConsoleMessage）。
    # WebEngine 内滚动不动 Qt 滚动条，宿主 away 守卫对卡内阅读完全失明，
    # 外层滚底判定必须显式查询此状态让位。
    cardReadingChanged = pyqtSignal(bool)
    # 🐛 滚动判据修复：wheelEvent 原先只能用 page().scrollPosition()（文档级），
    # 但真正的滚动容器是 body（CSS: body{overflow-y:scroll}），文档级 scrollTop
    # 恒为 0 → at_top 恒真 / at_bottom 恒假 → 向下滚动永远被判为"内部处理"，
    # 而内部其实滚不动，事件被吞 → 卡片内滚动完全失效。
    # 故在 reportHeight 回传时顺带携带 body 的真实滚动几何：
    # (scrollHeight, scrollTop, clientHeight)。
    bodyGeometryReported = pyqtSignal(int, int, int)
    contentReady = pyqtSignal()
    toolDiffRequested = pyqtSignal(str)  # tool_call_id
    subAgentLogRequested = pyqtSignal(str)  # task_ids (comma-separated)
    saveFileRequested = pyqtSignal(str, str)  # code, lang
    chartExpandRequested = pyqtSignal(str, str)  # (chart_type, payload_b64) — echarts/mermaid/svg/html 放大查看
    saveChartPngRequested = pyqtSignal(str, str)  # (name_b64, png_b64) — 图表 PNG 导出回传
    saveWidgetFileRequested = pyqtSignal(str, str)  # (wtype, content_b64) — svg widget 源码保存回传
    previewImageRequested = pyqtSignal(str)  # (image_url) — 正文图片点击 → 内置预览
    renderCrashed = pyqtSignal()  # renderer 进程崩溃（GPU OOM/崩溃），宿主卡片自愈

    def __init__(self, profile=None, parent=None):
        """创建一个 ConsoleMonitorPage。

        Args:
            profile: QWebEngineProfile 实例。传入 None 则使用默认 profile。
            parent: 父 QObject。
        """
        if profile is not None:
            super().__init__(profile, parent)
        else:
            super().__init__(parent)
        # renderer 进程崩溃（多图 GPU 内存压力下 Chromium 渲染进程 OOM/崩溃）。
        # 此前只有 JS 侧 webglcontextlost 上报路径（console "context_lost"），
        # renderer 真崩溃时 JS 已死、信号永不来 → 卡片永久白屏无人管。
        self.renderProcessTerminated.connect(self._on_render_process_terminated)

    def _on_render_process_terminated(self, status, exit_code):
        """QWebEnginePage 内建信号转发：renderer 进程终止 → 通知宿主卡片自愈"""
        logger.warning(f"WebEngine renderer 崩溃: status={status} exit_code={exit_code}")
        self.renderCrashed.emit()

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        """拦截 file:// 链接点击：用系统默认程序打开（不导航）。

        用 PyQt 原生 navigation 钩子（不写 JS 拦截），符合"浏览器自带"语义。
        """
        from PyQt5.QtGui import QDesktopServices
        from PyQt5.QtWebEngineWidgets import QWebEnginePage

        if url.scheme() == "file" and nav_type == QWebEnginePage.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        """Chromium 控制台消息分发。

        ⚠️ 族⑤修复（T43）：本函数运行在 Chromium 回调栈内，函数内的动作类
        emit（用户点击 context-tag/推荐问题/图表等触发）会**同步**进入 Qt
        信号链——在「取消长请求 + 429 错误态 + 大批次虚拟滚动回收」窗口内，
        链上某跳 receiver 可能已析构但连接未断 → 悬空调用 → AV（崩点漂移
        但同一 Python 行，生命周期 bug）。动作类 emit 统一改为
        ``_defer_emit(self, ...)`` 延迟到事件循环下一拍派发：Chromium
        回调栈立即返回，掐断「回调栈内同步进 Qt 信号链」的必要条件。
        lambda 闭包参数用默认参固化，防迟到绑定。

        ⚠️ 族⑤-2：延迟回调必须绑定本 page 生命周期（见 :func:`_defer_emit`），
        否则延迟期间 page 被销毁后回调仍执行 → 对已释放 sender emit → AV。

        例外：pywebview_height / cardReadingChanged / contentReady 是流式
        布局高频信号，延迟会破坏帧时序，保持同步。
        """
        msg = message.strip()
        # [PERF] pywebview_height 是最高频信号（流式时每周期多次触发），
        # 放在首位快速短路，避免对每条 height 消息都做 startswith("pywebview_ready") 等冗余判断
        if msg.startswith("pywebview_height:"):
            # 协议：'pywebview_height:<scrollHeight>[|<scrollTop>|<clientHeight>]'
            # 后两字段由 body 几何上报新增；旧格式（仅高度）仍兼容——骨架在 JS
            # 尚未注入、或第三方/降级路径下可能只发高度，此时不发射几何信号，
            # wheelEvent 回退到保守策略。
            try:
                payload = msg.split(":", 1)[1]
                if "|" in payload:
                    # 第 4 字段（可选，旧格式兼容）：卡片内用户阅读标志。
                    # 翻转才发信号：reportHeight 高频（流式 ~30ms/条），布尔去重后
                    # 信号量与用户滚动行为同阶，宿主侧零轮询成本。
                    parts = payload.split("|", 3)
                    h = int(float(parts[0]))
                    if len(parts) >= 4:
                        _rd = parts[3] == "1"
                        if _rd != getattr(self, "_last_card_reading", False):
                            self._last_card_reading = _rd
                            self.cardReadingChanged.emit(_rd)
                    self.heightReported.emit(h)
                    self.bodyGeometryReported.emit(h, int(float(parts[1])), int(float(parts[2])))
                else:
                    self.heightReported.emit(int(float(payload)))
            except Exception:
                pass
        elif msg == "pywebview_ready":
            self.contentReady.emit()
        elif msg.startswith("pywebview_action:"):
            if "context|||" in msg:
                try:
                    parts = msg.split("|||")
                    # [T43] 延迟派发（族⑤）：默认参固化 unquote 结果
                    _defer_emit(
                        self,
                        lambda a1=urllib.parse.unquote(parts[1]), a2=urllib.parse.unquote(parts[2]): (
                            self.contextActionRequested.emit(a1, a2)
                        ),
                    )
                except Exception:
                    pass
            elif "context_lost" in msg:
                self._handle_context_lost()
            elif "fence_prompt:" in msg:
                # 插件 fence 的 sendPrompt 桥 → 复用 <ask> 追问管线
                # （contextActionRequested + "ask"，与 <ask> 标签同一条链路）
                try:
                    import base64 as _b64mod

                    _text = _b64mod.b64decode(msg.split("fence_prompt:", 1)[1]).decode("utf-8")
                    if _text.strip():
                        # [T43] 延迟派发（族⑤），与 context||| 同链路
                        _defer_emit(
                            self,
                            lambda a1=_text: self.contextActionRequested.emit(a1, "ask"),
                        )
                except Exception:
                    pass
            elif "preview_image:" in msg:
                # 正文图片点击 → 内置预览（可滚轮缩放）。原先走 open_url 直接交给
                # 系统默认程序：跳出应用、体验割裂，且 data:/qrc: 的 src 交给
                # openUrl 后实际无响应。预处理失败时由宿主回退 openUrl。
                try:
                    url_str = msg.split("preview_image:", 1)[1]
                    # [T43] 延迟派发（族⑤）
                    _defer_emit(self, lambda u=url_str: self.previewImageRequested.emit(u))
                except Exception:
                    pass
            elif "open_url:" in msg:
                try:
                    url_str = msg.split("open_url:", 1)[1]
                    from PyQt5.QtCore import QUrl
                    from PyQt5.QtGui import QDesktopServices

                    QDesktopServices.openUrl(QUrl(url_str))
                except Exception:
                    pass
            elif "open_file:" in msg:
                # 处理打开文件/文件夹请求
                try:
                    file_path = msg.split("open_file:", 1)[1]

                    import os
                    import subprocess

                    if os.name == "nt":
                        if os.path.isdir(file_path):
                            # 文件夹：直接在资源管理器中打开
                            subprocess.Popen(["explorer", file_path])
                        else:
                            # 文件：使用系统默认程序打开
                            os.startfile(file_path)
                    else:
                        # macOS/Linux
                        cmd = "open" if os.uname().sysname == "Darwin" else "xdg-open"
                        subprocess.Popen([cmd, file_path])
                except Exception:
                    pass
            elif "tool_diff:" in msg:
                # 处理工具差异对比请求
                try:
                    tool_call_id = msg.split("tool_diff:", 1)[1]
                    # [T43] 延迟派发（族⑤）
                    _defer_emit(self, lambda tid=tool_call_id: self.toolDiffRequested.emit(tid))
                except Exception:
                    pass
            elif "subagent_log:" in msg:
                # 处理子智能体日志查看请求
                try:
                    task_ids = msg.split("subagent_log:", 1)[1]
                    # [T43] 延迟派发（族⑤）
                    _defer_emit(self, lambda t=task_ids: self.subAgentLogRequested.emit(t))
                except Exception:
                    pass
            elif "save_file:" in msg:
                # 处理保存文件请求
                try:
                    parts = msg.split("save_file:", 1)[1]
                    # 格式: b64_code:lang
                    sub_parts = parts.rsplit(":", 1)
                    if len(sub_parts) == 2:
                        b64_code, lang = sub_parts
                        code = base64.b64decode(b64_code).decode("utf-8")
                        self.saveFileRequested.emit(code, lang)
                except Exception:
                    pass
            elif msg.startswith("pywebview_action:chart_expand:"):
                # 图表/Widget 放大查看请求：console.log('pywebview_action:chart_expand:<type>:<b64>')
                try:
                    rest = msg.split("pywebview_action:chart_expand:", 1)[1]
                    chart_type, payload = rest.split(":", 1)
                    if chart_type in ("echarts", "mermaid", "svg", "html") and len(payload) <= _MAX_CHART_PAYLOAD_B64:
                        # [T43] 延迟派发（族⑤）
                        _defer_emit(
                            self,
                            lambda ct=chart_type, pl=payload: self.chartExpandRequested.emit(ct, pl),
                        )
                except Exception:
                    pass
            elif msg.startswith("pywebview_action:save_widget_file:"):
                # Widget 源码保存请求：console.log('pywebview_action:save_widget_file:<type>:<b64>')
                try:
                    rest = msg.split("pywebview_action:save_widget_file:", 1)[1]
                    wtype, payload = rest.split(":", 1)
                    if wtype in ("svg", "html") and len(payload) <= _MAX_CHART_PAYLOAD_B64:
                        # [T43] 延迟派发（族⑤）
                        _defer_emit(
                            self,
                            lambda wt=wtype, pl=payload: self.saveWidgetFileRequested.emit(wt, pl),
                        )
                except Exception:
                    pass
            elif msg.startswith("pywebview_action:save_chart_png:"):
                # 图表 PNG 导出回传：console.log('pywebview_action:save_chart_png:<name_b64>:<png_b64>')
                try:
                    rest = msg.split("pywebview_action:save_chart_png:", 1)[1]
                    name_b64, png_b64 = rest.split(":", 1)
                    if len(png_b64) <= _MAX_CHART_PAYLOAD_B64:
                        # [T43] 延迟派发（族⑤）
                        _defer_emit(
                            self,
                            lambda nb=name_b64, pb=png_b64: self.saveChartPngRequested.emit(nb, pb),
                        )
                except Exception:
                    pass
            else:
                try:
                    p = msg.split(":")
                    # [T43] 延迟派发（族⑤）
                    _defer_emit(
                        self,
                        lambda code=base64.b64decode(p[2]).decode("utf-8"), act=p[1]: self.codeActionRequested.emit(
                            code, act
                        ),
                    )
                except Exception:
                    pass

    def _handle_context_lost(self):
        self.contentReady.emit()


class _DialogEventFilter(QObject):
    """全局对话框事件过滤器（模块级单例 + viewer 注册表）。

    原实现：每个 CodeWebViewer 都向 QApplication 安装一个全局事件过滤器，
    N 个 viewer = N 个过滤器，任意鼠标移动等事件都会触发 O(N) 次 eventFilter
    转发。本类合并为单实例：同一事件只经过一次 eventFilter，按 event.type()
    快速短路（仅关心 Show/FocusIn/Hide/Close/Destroy 5 类低频事件），再遍历
    注册表分发，高频事件路由降为 O(1)。
    """

    # 关注的事件类型（QEvent 枚举值）：
    # Show=17, FocusIn=8（弹窗出现）; Hide=18, Close=19, Destroy=52（弹窗关闭/销毁兜底恢复）
    _WATCHED_EVENT_TYPES = (17, 8, 18, 19, 52)
    # 弹窗类名关键词（与原每 viewer 独立过滤器的判定一致）
    _POPUP_KEYWORDS = (
        "Dialog",
        "Popup",
        "Flyout",
        "InfoBar",
        "Toast",
        "ComboBox",
        "Menu",
        "ToolTip",
    )

    def __init__(self):
        super().__init__()
        self._viewers = set()  # 已注册的 CodeWebViewer 集合（生命周期随 viewer 增删）
        self._attached = False  # 是否已安装到 QApplication（幂等 attach/detach 标志）

    def register(self, viewer):
        """注册 viewer：确保全局过滤器已安装（幂等）；销毁时自动注销防引用滞留"""
        # 每次注册都检查安装：QApplication 尚未创建（服务先行等时序）时
        # 本次警告跳过，后续 register 会再次尝试，时序问题可自愈
        self._attach_to_application()
        self._viewers.add(viewer)
        try:
            # 兜底：viewer 未走 cleanup（正常路径 deleteLater → cleanup）就销毁时，
            # 自动从注册表移除，避免单例过滤器滞留已销毁对象引用
            viewer.destroyed.connect(self._on_viewer_destroyed)
        except (RuntimeError, TypeError):
            pass

    def unregister(self, viewer):
        """注销 viewer：从注册表移除并断开销毁监听（幂等，销毁后调用亦安全）。

        最后一个 viewer 注销后从 QApplication 卸载过滤器（对称清理）。
        """
        self._viewers.discard(viewer)
        try:
            viewer.destroyed.disconnect(self._on_viewer_destroyed)
        except (RuntimeError, TypeError):
            pass
        if not self._viewers:
            self._detach_from_application()

    def _attach_to_application(self):
        """向 QApplication 安装本过滤器（幂等：已安装则直接返回）。

        QApplication.instance() 为 None（如服务先行创建）时输出警告，
        由后续 register 再次尝试补装，时序问题可自愈。
        """
        if self._attached:
            return
        try:
            from PyQt5.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None:
                logger.warning("[MessageCard] 全局事件过滤器未安装：QApplication 尚未创建")
                return
            app.installEventFilter(self)
            self._attached = True
        except Exception as e:
            logger.warning(f"[MessageCard] 全局事件过滤器安装异常: {e}")

    def _detach_from_application(self):
        """从 QApplication 卸载过滤器（最后一个 viewer 注销/销毁时对称清理）"""
        if not self._attached:
            return
        try:
            from PyQt5.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._attached = False
        except Exception as e:
            logger.warning(f"[MessageCard] 全局事件过滤器卸载异常: {e}")

    @staticmethod
    def _is_viewer_alive(viewer) -> bool:
        """判断 viewer 的 C++ 对象是否仍存活（sip 判活，销毁过程中调用安全）"""
        try:
            sip.unwrapinstance(viewer)
            return True
        except RuntimeError:
            return False

    def _on_viewer_destroyed(self, *_args):
        """任一 viewer 销毁：惰性清理注册表中 C++ 对象已删的条目。

        destroyed 信号在销毁过程中发射，其 QObject 参数可能被 PyQt 包装为
        新 wrapper（与原对象不等），故不依赖参数匹配，改用 sip 判活清理。
        """
        for v in tuple(self._viewers):
            if not self._is_viewer_alive(v):
                self._viewers.discard(v)
        if not self._viewers:
            self._detach_from_application()

    def eventFilter(self, obj, event):
        # 快速短路：非关注事件（鼠标移动/绘制/键盘等高频事件）立即返回，
        # 不再像旧实现那样对每个 viewer 转发一次
        event_type = event.type()
        if event_type not in self._WATCHED_EVENT_TYPES:
            return False
        # 关注事件为低频事件（弹窗显示/关闭），此时才遍历注册表分发
        for viewer in tuple(self._viewers):
            try:
                self._dispatch(viewer, obj, event_type)
            except RuntimeError as e:
                # 区分「viewer 已销毁」与「父链对象已删等瞬时异常」：
                # 前者惰性剔除防引用滞留；后者 viewer 仍存活，仅记录日志
                # 不剔除（误剔除会使 MaskDialog 防穿透静默失效且无法恢复）
                if self._is_viewer_alive(viewer):
                    logger.debug(f"[MessageCard] 对话框过滤分派异常（viewer 存活）: {e}")
                else:
                    self._viewers.discard(viewer)
        return False

    def _dispatch(self, viewer, obj, event_type):
        """对单个 viewer 执行过滤逻辑（与原每 viewer 独立过滤器的行为等价）"""
        if event_type in (17, 8):  # QEvent.Show, QEvent.FocusIn
            obj_class = obj.__class__.__name__
            if any(kw in obj_class for kw in self._POPUP_KEYWORDS):
                # 只对透明遮罩对话框（MaskDialogBase 等）隐藏 WebView 防穿透；
                # 普通对话框（QFileDialog 等）无需隐藏，仅降低层级即可
                if "Dialog" in obj_class and hasattr(obj, "winId") and viewer._is_mask_dialog(obj):
                    # 全屏 MaskDialog → 隐藏 WebView 防止原生 HWND 穿透遮罩
                    viewer._hide_for_dialog(obj)
                else:
                    # 小弹窗（Menu/ComboBox/ToolTip等）→ 降低 Qt 层级
                    viewer.lower()
                    parent = viewer.parent()
                    while parent:
                        parent.lower()
                        # 找到 MessageCard 或聊天容器为止
                        if hasattr(parent, "chat_layout") or parent.__class__.__name__ == "MessageCard":
                            break
                        parent = parent.parent()
                    if hasattr(obj, "raise_"):
                        obj.raise_()
        else:  # QEvent.Hide, QEvent.Close, QEvent.Destroy
            # 兜底恢复：对话框关闭/隐藏/销毁时，若它是导致 WebView 隐藏的对象则恢复
            hidden = getattr(viewer, "_hidden_dialogs", None)
            if hidden and obj in hidden:
                hidden.discard(obj)
                if not hidden:
                    viewer.show()
                    viewer._restore_chat_scroll_pos()


# 模块级单例：全局仅此一个 QApplication 级事件过滤器
_dialog_event_filter = _DialogEventFilter()

# D3D11/WARP 单纹理物理上限 16384px，留余量取 16000（见 CodeWebViewer.MAX_HEIGHT 注释）
_PHYSICAL_TEXTURE_LIMIT = 16000


def _logical_height_cap(dpr, physical_limit: int = _PHYSICAL_TEXTURE_LIMIT) -> int:
    """DPR → 不超物理纹理上限的逻辑高度（纯函数，供单测复用）。

    Chromium 离屏表面按「逻辑尺寸 × DPR」分配物理纹理，逻辑上限必须随本机
    缩放收缩。异常 DPR（0/负数）按 1.0 处理；结果保底 2000 保证可用性。
    """
    dpr = float(dpr) if dpr and float(dpr) > 0 else 1.0
    return max(2000, int(physical_limit / dpr))


class CodeWebViewer(QWebEngineView):
    contentHeightChanged = pyqtSignal(int)
    codeActionRequested = pyqtSignal(str, str)
    contextActionRequested = pyqtSignal(str, str)
    toolDiffRequested = pyqtSignal(str)  # tool_call_id
    subAgentLogRequested = pyqtSignal(str)  # task_ids (comma-separated)
    saveFileRequested = pyqtSignal(str, str)  # code, lang
    chartExpandRequested = pyqtSignal(str, str)  # (chart_type, payload_b64) — 图表放大查看
    saveChartPngRequested = pyqtSignal(str, str)  # (name_b64, png_b64) — 图表 PNG 导出回传
    saveWidgetFileRequested = pyqtSignal(str, str)  # (wtype, content_b64) — svg widget 源码保存回传
    previewImageRequested = pyqtSignal(str)  # (image_url) — 正文图片点击 → 内置预览
    # WebEngine 上下文丢失信号
    contextLost = pyqtSignal()
    contextRestored = pyqtSignal()
    needRecreate = pyqtSignal()  # 需要完全重建控件（恢复失败时）

    # [B3] 线程池渲染完成信号（worker 线程 emit → 主线程槽执行）：
    # 不能从 worker 线程直接调用 QTimer.singleShot(0, ...)（worker 无事件循环，
    # 定时器事件不会投递到主线程）；Qt 信号跨线程 emit 是线程安全的，
    # 自动 QueuedConnection 到主线程执行 _apply_render_result。
    renderDone = pyqtSignal(int, object)  # (seq, html)

    # WebEngine 最大尺寸限制，防止 GPU 内存溢出
    # 降低 MAX_HEIGHT 可大幅减少每个 Chromium 实例的离屏渲染缓冲区
    # 4000→2000 将单视图 GPU 缓冲区从 ~28.8MB 降至 ~14.4MB
    #
    # 🐛 滚动体验重构（2026-08-30）：原 3000px 会让长回复（多个代码块 + 工具结果）
    # **在卡片内部**出现滚动条 —— 消息列表于是变成「外层 1 个 QScrollArea + 每张卡
    # 各自 1 个内滚区」，滚轮要先问「里面还能滚吗」再决定转发，是滚动发黏的根因。
    # 现在把上限抬到 10000px：真实内容几乎不可能触及，卡内滚动条不再出现，
    # 滚动统一由外层 chat_scroll_area 承载。
    # 保留上限的原因（**不能删**）：这是 Chromium 合成表面的硬约束兜底 ——
    # 卡片宽度 ~700px 时 10000px 高 ≈ 28MB 合成表面，再往上会明显放大 GPU 内存。
    # 极端长内容仍会回退到内滚（wheel 转发逻辑因此必须保留），但那是安全网而非常态。
    MAX_WIDTH = 1800
    MAX_HEIGHT = 10000

    def __init__(self, parent=None, light=False):
        super().__init__(parent)
        # 🛡️ 物理纹理上限钳制：Chromium 离屏表面按「逻辑尺寸 × DPR」分配物理纹理，
        # D3D11/WARP 单纹理硬上限 16384px。225% 缩放（DPR 2.25）下 10000 逻辑
        # → 22500 物理 → ResizeOffscreenFramebuffer 分配失败 → GPU 上下文丢失
        # （gles2_cmd_decoder "excessive dimensions" → MakeCurrent failed for GetTextureQt）。
        # 逻辑上限随本机 DPR 收缩，保物理 ≤ 16000（留 384 余量）；
        # 超限内容回退内滚安全网（wheelEvent 内外转发，见 MAX_HEIGHT 注释）。
        # 实例属性覆盖类常量：下方 resize/setFixedHeight 钳制与骨架 CSS
        # max-height 均按 self.MAX_HEIGHT 取值，全部自动生效。
        self.MAX_HEIGHT = min(
            CodeWebViewer.MAX_HEIGHT, _logical_height_cap(self.devicePixelRatioF())
        )
        # [B4-强回收] renderer 进程 PID（强回收层 kill 离屏进程用；0 = 未就绪/已清理）
        self._renderer_pid: int = 0
        # [B3] 连接线程池渲染完成信号（worker 线程 emit → 本槽在主线程执行）
        self.renderDone.connect(self._on_render_done_signal)
        self._markdown_text = ""
        self._streaming = True
        self._is_history = False  # 历史会话标志（非流式加载的历史消息）
        self._is_js_ready = False
        # 下一次非流式渲染是否为"流式结束的终渲染"（决定能否走线程池，见
        # _perform_update）：仅在 CodeWebViewer.__init__ 初始化一次。
        self._final_render_pending = False
        self._last_rendered_html = ""
        self._last_rendered_markdown = ""
        # 流式渲染哈希缓存：避免对相同 processed_md 重复跑 6 轮正则 + md.convert()
        self._processed_md_hash = None
        self._cached_streaming_html = None
        self._cached_raw_md_hash = 0  # hash(self._markdown_text) 在缓存时的快照，供 finish_streaming 验证缓存有效性
        self._lazy_markdown_cb = None  # 懒回调：渲染时才生成 markdown，避免高频 content_to_markdown
        # [PERF] 工具结果 markdown 缓存：tool_call_id → <tool>...</tool> markdown 字符串
        # 已完成的工具块的 markdown 只計算一次，後續增量渲染跳過昂貴的
        # _sanitize_result + sorted() + JSON 序列化，直接拼接緩存結果。
        self._tool_md_cache: Dict[str, str] = {}
        # [B2] 工具 DOM 脏标记：MessageCard 层 JS 增量注入工具块（_inject_tool_streaming_html /
        # append_tool_result）时置 True，_perform_update 则必须走 save/restore 保护（否则
        # updateContent 整块替换会被 JS 注入的运行框/完成框抹掉）；无工具 DOM 注入时走裸
        # updateContent（省整页 save/restore JS 包装，MB 级 IPC 瘦身）。更新成功后置 False。
        # 🐛 修复（编辑工具框运行中消失）：清除时机延后到 JS 渲染回调执行完成后（runJavaScript
        # 异步），并带双重守卫——_injected_pending_tools 非空（仍有 JS 注入未完成的工具块在
        # DOM）或代际变化（_tool_dom_dirty_gen 递增，期间有新注入）时**不清除**，避免下一次
        # 全量渲染误判"无工具 DOM 需保护"→ 裸 updateContent 抹掉运行框。
        self._tool_dom_dirty: bool = False
        # [B2] 工具 DOM 脏标记代际：每次置 True 时递增，JS 回调清除时与捕获值比较，
        # 防止"旧渲染回调误清新注入的 dirty"（新注入已递增代际 → 旧回调放弃清除）。
        self._tool_dom_dirty_gen: int = 0
        # [B2] JS 注入但尚未完成（结果未 append_tool_result）的工具 id 集合。
        # 这些工具的运行框/预览块只存在于 DOM、不在 markdown 中，全量渲染必须
        # save/restore 保护；集合非空时禁止清除 _tool_dom_dirty。
        self._injected_pending_tools: set = set()
        # [B3] 异步渲染序号与防抖状态（渲染移出主线程）：
        # - _render_seq：递增序号，回调时校验，过期结果（新渲染已提交）直接丢弃
        # - _render_inflight：是否有在途线程池渲染任务（防抖：在途时只记 pending）
        # - _render_pending：在途期间积压的最新 (seq, md, compact) 快照，完成后续派
        self._render_seq: int = 0
        self._render_inflight: bool = False
        self._render_pending: Optional[tuple] = None
        # [V1] 可见性门控：隐藏 tab 期间被门控跳过的渲染请求标记，
        # 恢复可见时（showEvent）据此补渲，保证流式/工具结果最终完整性。
        self._render_deferred: bool = False
        # [PERF] 主题刷新期间不可见 → 跳过 JS 注入，恢复可见时补注入标记
        self._theme_css_pending: bool = False
        # [T6] 主题重渲分帧队列脏标记：refresh_theme 置 True + 入队，队列消费时清除
        self._theme_render_pending: bool = False
        # [T24] 图表存在标记（只置不清）：全量渲染产物含图表容器任一标记即置位。
        # 不清的理由：流式期间新增图表块的保守防护（首渲后有图必然置位）；
        # 复用/新内容残留 True 仅导致多一次无害 IPC，不影响正确性。
        self._has_charts: bool = False
        # [B1] 差量渲染状态：
        # - _stable_html：已追加到 DOM 的稳定格式化 HTML 累积
        # - _stable_md_len：已差量消费的 markdown 偏移（后续 _extract_closed_segments 从这扫描）
        # - _needs_full_render：需要全量渲染（初值/主题/字体/状态切换/流式结束/缓存清理）
        self._stable_html: str = ""
        self._stable_md_len: int = 0
        # [B1] 尾部行内渲染哈希缓存：安全定时器重复触发时 tail 未变则跳过渲染
        self._tail_html_hash: int = 0
        self._needs_full_render: bool = True
        self._light_skeleton = light  # 轻量骨架标志（去掉 echarts CDN 等）
        # [PERF] 最小渲染间隔 80ms：降低 WebEngine setHtml 调用频率
        # 每 80ms 合并一次渲染比 50ms 减少 37.5% 的 Chromium 重排版次数，
        # 对用户感知的流式流畅度影响极小（人眼无法分辨 50ms 与 80ms 的渲染间隔差异）
        self._min_render_interval = 80
        self._height_report_pending = False
        self._context_lost = False  # 上下文丢失标志
        # [T11] 恢复计数按信号源拆分：renderCrashed 与 JS webglcontextlost 各自累加，
        # 互不叠加（单 renderer 崩溃会引发全量 viewer 的 webgl 集中上报，共享计数
        # 会把第一次 renderCrashed 直接推过阈值 → 误入整卡重建风暴）。
        self._render_crash_count = 0  # renderer 崩溃次数（阈值 >2 → needRecreate）
        self._webgl_ctx_lost_count = 0  # JS webgl 上下文丢失次数（阈值 >1 → needRecreate）
        # 注：原 CodeWebViewer 的 _resize_debounce_timer(100ms) 与 _resize_timer(100ms)
        # 只被定义/连接、从未 start()，属死代码且误导排查，已移除。
        # resize 期间真正生效的防抖只剩下面的 _resize_unlock_timer(150ms)。
        # 性能优化：resize 锁，防止 resize 期间频繁报告高度
        self._resize_locked = False
        self._resize_unlock_timer = QTimer(self)
        self._resize_unlock_timer.setSingleShot(True)
        self._resize_unlock_timer.setInterval(150)  # resize 结束后 150ms 再报告高度
        self._resize_unlock_timer.timeout.connect(self._on_resize_unlock)

        # 思考已完成标志：工具调用开始时置 True，阻止 _render_markdown_to_html 继续剥离 </think>
        self._thinking_finalized = False
        # 流式思考首 chunk 标志：首 chunk 渲染"深度思考中..." spinner，后续静默累积不更新 DOM
        self._reasoning_streaming_started = False
        # <think> 标签文本流式思考标志：与 _reasoning_streaming_started 对应，
        # 用于 text 块中包含 <think> 标签时的静默累积策略
        self._think_text_streaming_started = False

        # [PERF] 流式速度跟踪：用于自适应安全渲染间隔
        self._last_chunk_time = 0.0  # 上次 append_chunk 的时间戳（monotonic ns）
        self._current_adaptive_interval = self._SAFETY_RENDER_INTERVAL  # 当前自适应间隔
        # [PERF] 上次 _perform_update 的时刻（monotonic 秒），供软边界合并窗口判断
        self._last_render_ts = 0.0

        # 内部文档高度跟踪（用于 wheelEvent 判断内部是否可滚动）
        self._document_height = 0
        # 🐛 滚动判据修复：body 的真实滚动几何（由 reportHeight 顺带回传）。
        # _body_client_height: body 可视高度；_body_scroll_top: body 已滚动距离。
        # 真实可滚动量 = _document_height - _body_client_height，
        # 该值是"卡片内部能否滚动"的唯一正确判据。
        self._body_client_height = 0
        self._body_scroll_top = 0
        self._body_geom_valid = False
        # 自愈计数：连续判定为"内部处理"但 body.scrollTop 纹丝不动的滚轮次数。
        # 达到阈值说明内部其实滚不动（几何缓存滞后/内容未溢出），强制转发外部，
        # 彻底消除"怎么滚都没反应"的粘性失效。
        self._wheel_stuck_streak = 0
        self._wheel_last_scroll_top = -1
        self._wheel_last_ts = 0.0
        self._wheel_delegated_inner = False

        # 1. 渲染定时器
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._perform_update)

        # 2. Resize 定时器
        # （原 _resize_timer(100ms → _safe_report_height) 从未 start()，死代码已移除；
        #   resize 期高度上报统一由 _resize_unlock_timer(150ms) 兜底。）

        # 共享全局 profile：所有消息卡片复用同一 Chromium 进程池，
        # 避免每个卡片独立匿名 profile 触发独立进程组初始化（加载慢的根因）。
        self._profile = get_shared_web_profile()
        self._page = ConsoleMonitorPage(self._profile, self)
        self.setPage(self._page)

        # 启用本地文件访问，支持 markdown 图片显示
        ws = self.settings()
        ws.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        ws.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)

        # 透明背景
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.page().setBackgroundColor(Qt.transparent)
        # 使用自定义右键菜单（不是浏览器默认的）
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(40)

        # ⚠️ 转发一律 signal-to-signal 直连（.connect(self.xxxRequested)），
        # 禁止 .connect(self.xxxRequested.emit)：bound .emit 对 PyQt 是普通
        # Python callable，不绑定 receiver 生命周期（receiver 销毁后连接残留），
        # 且 disconnect(bound.emit) 永远抛 TypeError（每次访问 .emit 都是新
        # 对象，匹配不上）——2026-09-18 代码框按钮闪退
        # （Qt5Core!QObject::signalsBlocked AV READ 0x0）根因。
        self._page.codeActionRequested.connect(self.codeActionRequested)
        self._page.contextActionRequested.connect(self.contextActionRequested)
        self._user_reading_inside = False
        self._page.heightReported.connect(self._on_height_reported)
        self._page.bodyGeometryReported.connect(self._on_body_geometry_reported)
        self._page.cardReadingChanged.connect(self._on_card_reading_changed)
        self._page.contentReady.connect(self._on_js_ready)
        self._page.toolDiffRequested.connect(self.toolDiffRequested)
        self._page.subAgentLogRequested.connect(self.subAgentLogRequested)
        self._page.saveFileRequested.connect(self.saveFileRequested)
        self._page.chartExpandRequested.connect(self.chartExpandRequested)
        self._page.saveChartPngRequested.connect(self.saveChartPngRequested)
        self._page.saveWidgetFileRequested.connect(self.saveWidgetFileRequested)
        self._page.previewImageRequested.connect(self.previewImageRequested)
        self._page.renderCrashed.connect(self._on_render_crashed)

        self._load_skeleton()

        # ── 对话框层级管理 ──
        # _hidden_dialogs: set，记录当前导致 WebView 隐藏的对话框对象
        self._hidden_dialogs = set()
        # 遮罩对话框隐藏期间记录的外层滚动位置（-1 = 无记录），恢复显示后回设
        self._saved_dialog_scroll_pos = -1

    # ──────────────────────────────────────────────
    # 对话框 HWND 穿透防护
    # ──────────────────────────────────────────────
    # QWebEngineView 在 Windows 上创建原生 HWND 子窗口，
    # 遇到 WA_TranslucentBackground 的 MaskDialog 分层窗口时，
    # Chromium GPU 合成表面可能穿透遮罩渲染在对话框之上。
    # 策略：检测到透明遮罩对话框显示时隐藏 WebView，
    #       对话框关闭（finished）或销毁（destroyed）后恢复；
    #       额外用 eventFilter 监听 Hide/Close/Destroy 事件兜底，
    #       避免原生对话框（无 Qt 信号）导致永久隐藏。

    def _find_chat_scroll_area(self):
        """沿 Qt 父链找到外层聊天滚动区（宿主窗口的 chat_scroll_area 属性）"""
        try:
            widget = self.parentWidget()
            while widget is not None:
                area = getattr(widget, "chat_scroll_area", None)
                if area is not None:
                    return area
                widget = widget.parentWidget()
        except RuntimeError:
            pass
        return None

    def _hide_for_dialog(self, dialog):
        """对话框显示时隐藏 WebView，防止原生 HWND 穿透遮罩"""
        hidden = getattr(self, "_hidden_dialogs", None)
        if hidden is None:
            hidden = set()
            self._hidden_dialogs = hidden
        if dialog in hidden:
            return  # 同一对话框重复 Show/FocusIn 不叠加计数
        # 首个 viewer 隐藏前记录外层滚动位置：viewer 隐藏令卡片高度塌缩、
        # chat_scroll_area 内容总高骤减，滚动条 value 被 Qt 自动 clamp，
        # 恢复显示后无人回设 → 滚动位置丢失（跳到底部/顶部）
        if not hidden:
            try:
                area = self._find_chat_scroll_area()
                self._saved_dialog_scroll_pos = area.verticalScrollBar().value() if area is not None else -1
            except RuntimeError:
                self._saved_dialog_scroll_pos = -1
        hidden.add(dialog)
        self.hide()
        # finished + destroyed 双信号：dismiss 即恢复，销毁兜底
        for sig_name in ("finished", "destroyed"):
            try:
                sig = getattr(dialog, sig_name, None)
                if sig is not None:
                    sig.connect(self._restore_from_dialog)
            except (TypeError, RuntimeError, AttributeError):
                pass

    def _restore_from_dialog(self, _result=None):
        """对话框关闭后恢复 WebView 显示（_result 为 QDialog.finished 的 result code）"""
        hidden = getattr(self, "_hidden_dialogs", None)
        if not hidden:
            return
        sender = self.sender()
        if sender is not None:
            hidden.discard(sender)
        if not hidden:
            self.show()
            self._restore_chat_scroll_pos()

    def _restore_chat_scroll_pos(self):
        """恢复 hide 前记录的外层滚动位置。

        show() 触发的布局重排经 posted LayoutRequest 事件完成，Qt 事件循环
        中 posted 事件先于 timer 处理，故 singleShot(0) 时 maximum 已恢复。
        """
        pos = getattr(self, "_saved_dialog_scroll_pos", -1)
        self._saved_dialog_scroll_pos = -1
        if pos < 0:
            return

        def _apply():
            try:
                area = self._find_chat_scroll_area()
                if area is None:
                    return
                bar = area.verticalScrollBar()
                bar.setValue(max(bar.minimum(), min(pos, bar.maximum())))
            except RuntimeError:
                pass

        QTimer.singleShot(0, _apply)

    @property
    def _tool_compact_mode(self) -> bool:
        try:
            from app.utils.config import Settings

            return Settings.get_instance().ui_compact_tool_area.value
        except Exception:
            return True

    @property
    def _tool_target_id(self) -> str:
        return "tool-content" if self._tool_compact_mode else "content-placeholder"

    def _handle_context_lost(self):
        """JavaScript 报告上下文丢失"""
        if not self._context_lost:
            self._context_lost = True
            self._webgl_ctx_lost_count += 1
            self.contextLost.emit()

            # 如果已经丢失超过1次，直接请求重建
            if self._webgl_ctx_lost_count > 1:
                self.needRecreate.emit()
                return

            # 尝试恢复上下文
            self._schedule_context_restore()

    def _schedule_context_restore(self):
        """延迟恢复 WebEngine 上下文"""
        QTimer.singleShot(500, self._try_restore_context)

    def _try_restore_context(self):
        """尝试恢复 WebEngine 上下文"""
        try:
            # 重新加载骨架 HTML
            self._is_js_ready = False
            self._load_skeleton()
            self._context_lost = False
            self.contextRestored.emit()
            # 重新渲染内容
            if self._markdown_text:
                self._schedule_render(immediate=True)
        except Exception as e:
            logger.warning(f"Context restore failed: {e}")
            # 恢复失败，请求重建
            self.needRecreate.emit()

    def _on_render_crashed(self):
        """renderer 进程崩溃自愈：错峰排队恢复；连续崩溃（≥3 次）交重建。

        计数专用 ``_render_crash_count``（不与 JS webgl 路径共享）：单次崩溃
        重载骨架成本远低于整卡重建；反复崩溃说明环境级问题（如显存枯竭），
        重建兜底。重载骨架后 vault/队列等 JS 状态随页面重置，补渲时图表按当前
        内容重新 init 一次。

        [T11 错峰] 单 renderer 承载全部卡片时，一次崩溃让所有卡片同帧收到本回调；
        若各自即刻 ``_try_restore_context``，N 张卡的重载齐发会把刚重启的 Chromium
        线程再次压垮（→ 0xC0000409 进程级死亡）。改交 ``RenderCrashQueue`` 排队，
        每 500ms 只恢复一张（可见优先）。JS webgl 路径若先恢复（置
        ``_context_lost=False``），队列下一拍自动剔除该条目，不会重复恢复。
        """
        logger.warning("WebEngine renderer 崩溃，触发卡片自愈")
        self._render_crash_count += 1
        if self._render_crash_count > 2:
            self.needRecreate.emit()
            return
        self._context_lost = True
        RenderCrashQueue.get_instance().enqueue(self)

    def event(self, event):
        """拦截 WebEngine 事件"""
        # 处理上下文丢失
        if event.type() == QTimerEvent and hasattr(self, "_context_lost_timer"):
            pass
        return super().event(event)

    def setFixedSize(self, *args, **kwargs):
        """限制最大尺寸，防止 GPU 内存溢出"""
        # 计算安全尺寸
        w = args[0] if len(args) > 0 else kwargs.get("width", self.MAX_WIDTH)
        h = args[1] if len(args) > 1 else kwargs.get("height", self.MAX_HEIGHT)

        # 限制最大尺寸
        safe_w = min(w, self.MAX_WIDTH) if isinstance(w, int) else w
        safe_h = min(h, self.MAX_HEIGHT) if isinstance(h, int) else h

        super().setFixedSize(safe_w, safe_h)

    def resize(self, *args, **kwargs):
        """限制 resize 尺寸，防止过大导致 GPU 内存溢出"""
        w = args[0] if len(args) > 0 else kwargs.get("width", self.MAX_WIDTH)
        h = args[1] if len(args) > 1 else kwargs.get("height", self.MAX_HEIGHT)

        # 限制最大尺寸
        safe_w = min(w, self.MAX_WIDTH) if isinstance(w, int) else w
        safe_h = min(h, self.MAX_HEIGHT) if isinstance(h, int) else h

        super().resize(safe_w, safe_h)

    def setFixedHeight(self, height):
        """限制最大高度，防止 GPU 内存溢出"""
        safe_h = min(height, self.MAX_HEIGHT)
        super().setFixedHeight(safe_h)

    def setFixedWidth(self, width):
        """限制最大宽度，防止 GPU 内存溢出"""
        safe_w = min(width, self.MAX_WIDTH)
        super().setFixedWidth(safe_w)

    def _install_dialog_filter(self):
        """注册到全局单例事件过滤器（注册表方式，不再每 viewer 安装一个过滤器）"""
        _dialog_event_filter.register(self)

    def reset_for_reuse(self):
        """归还 ``WebViewPool`` 前的重置：**保留骨架**，只清空内容与卡片状态。

        ⚠️ 这里刻意**不用** ``setHtml("")`` 清页。原因（2026-09-09 内存回归）：
        清空文档后复用方必须重新 ``_load_skeleton()``，等于每次复用都新建一份
        ~54KB 文档 + 重新执行骨架 JS（setInterval/ResizeObserver/事件监听全套
        重新注册）。加载"消息数很多"的会话时批次反复卸载/重建，文档频繁新建，
        Chromium 侧内存与 CPU 占用明显上升。
        改成"原地清空内容容器"后：骨架与 JS 上下文存活，复用成本只剩一次
        轻量 DOM 清理，既无文档重建，也不会出现白屏。

        只清理 viewer 自身。**与卡片的信号连接不在这里断开** —— 那部分由
        ``MessageCard.detach_viewer()`` 成对维护（连接/断开写在一起，新增信号
        时不易漏）。

        注意 ``_renderer_pid`` **不清零**：复用时 renderer 进程通常被 Chromium
        保留，池中 viewer 的 PID 要交给 B4 强回收护栏做「在用」判定，
        清零会让该进程被误杀。
        """
        try:
            if self.page() and self._is_js_ready:
                self.page().runJavaScript(_RESET_CONTENT_FOR_REUSE_JS)
        except RuntimeError:
            pass
        # 骨架仍在 → JS 就绪态保持 True（复用方无需重载骨架）。
        # 仅当骨架根本没加载成功时把它置 False，由取用侧补一次 _load_skeleton。
        if not self._is_js_ready:
            self._is_js_ready = False
        # 🐛 高度残留：卡片通过 _commit_viewer_height 用 setFixedHeight 钉死高度
        # （长消息可达数千 px）。归还时不清，复用方在骨架/JS 就绪前（或任何
        # 未上报高度的异常路径）会顶着上一张消息的陈旧高度 → 空白巨高卡片。
        # 复位到最小高度，由新内容首次 reportHeight 重新收敛。
        try:
            self.setMinimumHeight(40)
            self.setFixedHeight(40)
        except RuntimeError:
            pass
        self._streaming = False
        self._is_history = False
        self._stable_html = ""
        self._stable_md_len = 0
        self._needs_full_render = True
        self._tail_html_hash = 0
        self._lazy_markdown_cb = None
        self._restore_finished_ids = None
        self._resize_locked = False
        self._height_report_pending = False
        # [T11] 复用前复位崩溃/上下文状态：计数器是「本 viewer 生命周期内」语义，
        # 不清零会让崩溃过的 viewer 复用后被陈旧计数误判 → 无谓重建；
        # 从崩溃队列移除（本页已重置，无需再自愈）。这是排队器正确性前置条件。
        self._context_lost = False
        self._render_crash_count = 0
        self._webgl_ctx_lost_count = 0
        RenderCrashQueue.get_instance().discard(self)
        self._document_height = 0
        self._body_client_height = 0
        self._body_scroll_top = 0
        self._body_geom_valid = False
        # 🐛 内容态必须一并清：_markdown_text / _cached_streaming_html 若残留，
        # 复用后 _on_js_ready 会拿旧文本立即渲染一次（与新卡片内容无关），
        # 多一次全量渲染 = 多一份内存与主线程开销（大会话加载时逐卡叠加）。
        self._markdown_text = ""
        self._last_rendered_markdown = ""
        self._cached_streaming_html = None
        self._processed_md_hash = 0
        self._cached_raw_md_hash = 0
        self._last_rendered_html = None
        self._render_deferred = False
        # [T6] 复用前摘出主题补渲队列：复用后本 viewer 已属新卡片，残留的
        # 旧主题补渲会打到新内容上（或空转打断新卡的流式节拍）
        _theme_rerender_queue.discard(self)
        self._theme_render_pending = False
        if hasattr(self, "_tool_md_cache"):
            with contextlib.suppress(Exception):
                self._tool_md_cache.clear()

    def _is_mask_dialog(self, obj) -> bool:
        """判断是否为透明遮罩对话框（WA_TranslucentBackground，需防穿透）"""
        try:
            return bool(obj.testAttribute(Qt.WA_TranslucentBackground))
        except Exception:
            return False

    def lower_for_popup(self):
        """降低控件层级，让弹出窗口可以显示在前面"""
        self.lower()
        # 降低父级
        parent_card = self.parent()
        if parent_card:
            parent_card.lower()

    # 安全的高度上报函数
    def _safe_report_height(self):
        try:
            # 再次检查 page 是否存在，避免 C++ 对象已删除错误
            if self.page():
                self._height_report_pending = False
                self.page().runJavaScript("reportHeight();")
        except RuntimeError:
            # 捕获可能的 "wrapped C/C++ object has been deleted"
            pass

    def _do_resize_check(self):
        # 如果处于 resize 锁定状态，登记一次补报后返回。
        # ⚠️ 不能直接 return：外层 resize 恢复路径（set_resize_preview_mode(False)
        # → viewer.update_height()）会调到这里，静默丢弃意味着这次高度上报永久
        # 丢失且无任何重试 → 卡片高度停在旧值，表现为「有的卡片跟上、有的没跟上」。
        # 标记由 _on_resize_unlock 消费补发。
        if self._resize_locked:
            self._height_report_pending = True
            return
        self._height_report_pending = False
        try:
            if self.page():
                self.page().runJavaScript("reportHeight();")
        except RuntimeError:
            pass

    def _on_resize_unlock(self):
        """resize 结束后触发高度报告（含锁定期内被推迟的补报）"""
        self._resize_locked = False
        if self._height_report_pending:
            self._height_report_pending = False
        self._do_resize_check()

    def _on_card_reading_changed(self, reading: bool):
        """记录「用户正在卡片内部滚动阅读」状态（reportHeight 第 4 字段翻转时推送）

        宿主（MessageCard / main_widget）经 is_user_reading_inside() 查询。
        """
        self._user_reading_inside = reading

    def _on_height_reported(self, h):
        # 🐛 打点：结束这一拍的"JS 落地 + 布局"耗时 = 渲染派发 → 首个 reportHeight。
        if getattr(self, "_finish_t0", 0.0) > 0.0:
            _el = (time.perf_counter() - self._finish_t0) * 1000
            self._finish_t0 = 0.0
            if FINISH_TIMING_ENABLED or _el >= 120:
                logger.info(f"[finish-render] js_land+layout={_el:.1f}ms height={h}")
        self._height_report_pending = False
        self._document_height = h  # 跟踪文档高度用于 wheelEvent 边界判断
        final_h = h + 2
        if abs(self.height() - final_h) > 2:
            self.contentHeightChanged.emit(final_h)

    def _on_body_geometry_reported(self, scroll_height: int, scroll_top: int, client_height: int):
        """缓存 body 的真实滚动几何（reportHeight 顺带回传）。

        body 是唯一的滚动容器（CSS: body{overflow-y:scroll; max-height}），
        因此"卡片内部能否滚动"只能由这三个值判定：
            可滚动量 = scrollHeight - clientHeight
        而 Qt 侧的 page().scrollPosition()/contentsSize() 是文档级指标，
        在 body-scroller 架构下恒为 0 / 不含 body 内部溢出，不能作为判据。
        """
        self._document_height = scroll_height
        self._body_scroll_top = scroll_top
        self._body_client_height = client_height
        self._body_geom_valid = client_height > 0

    def _inner_scroll_range(self) -> tuple:
        """返回 (scroll_top, max_scroll)：body 当前滚动位置与最大可滚动距离。

        无有效几何缓存时返回 (0, 0)，调用方据此走保守策略。
        """
        if not self._body_geom_valid:
            return 0, 0
        max_scroll = max(0, self._document_height - self._body_client_height)
        return self._body_scroll_top, max_scroll

    def update_height(self):
        """主动触发一次高度/几何上报（供外部宽度同步后驱动）。

        sync_width 原本只对 PlainTextViewer 生效（CodeWebViewer 无此方法），
        导致 resize 恢复后完全被动等待 JS 的 ResizeObserver + rAF×3 才上报，
        卡片高度收敛慢（表现为"窗口变大后内容迟迟不适配"）。补此方法后，
        宽度同步可主动驱动一次上报，无需等 ResizeObserver 的三帧延迟。
        """
        self._do_resize_check()

    def showEvent(self, event):
        """[V1] 可见性恢复：隐藏 tab 期间被门控的渲染请求在此补渲。

        tab 切回时 Qt 会向子 widget 传播 Show 事件（QStackedWidget 隐藏页
        isVisible()=False，切回后重新可见触发本事件）。若隐藏期间积压了
        渲染请求（_render_deferred），恢复可见后按 _schedule_render 现有
        调度机制补渲，保证流式输出/工具结果的最终完整性。
        """
        super().showEvent(event)
        # [PERF] 主题刷新期间不可见 → 跳过 JS 注入，恢复可见时补注入
        # CSS 变量（updateContent 不重载骨架，旧主题色会残留）
        if getattr(self, "_theme_css_pending", False):
            self._theme_css_pending = False
            try:
                from app.utils.theme_manager import theme_manager as _tm

                _is_light = _tm.is_light_theme()
                _theme = current_theme()
                from app.utils.theme_refresh import ThemeRefreshCoordinator

                _js = ThemeRefreshCoordinator.get_or_build_js(_theme, _is_light)
                if self.page():
                    self.page().runJavaScript(_js)
            except Exception:
                pass
        if getattr(self, "_render_deferred", False):
            if self._is_js_ready:
                self._render_deferred = False
                self._schedule_render(immediate=True)
            # 🛡️ F2：JS 未就绪时保留 deferred（不清标志）——先清标志再调
            # _schedule_render 会因 JS 未就绪直接 return，积压请求被清但永不
            # 补渲；保留后由 _on_js_ready 统一补渲（可以延迟，不能丢失）。

    def _on_js_ready(self):
        self._is_js_ready = True
        # 同步简洁模式标志到 JS
        try:
            from app.utils.config import Settings

            compact = "true" if Settings.get_instance().ui_compact_tool_area.value else "false"
            self.page().runJavaScript(f"window._toolCompactMode = {compact};")
            # 历史会话：先折叠工具区（设置 data-collapsed="true"，dock sync 需要读取此值）
            if getattr(self, "_is_history", False):
                self.page().runJavaScript(
                    "var _ts=document.getElementById('tool-section');"
                    "var _sep=document.getElementById('tool-separator');"
                    "if(_ts){if(typeof _beginToolSectionTransition==='function')_beginToolSectionTransition();"
                    "_ts.setAttribute('data-collapsed','true');}"
                    "if(_sep)_sep.setAttribute('aria-expanded','false');"
                )
            # 坞态同步：判定用 Python 端真值 _streaming，弃用旧「未折叠 → 开坞」推导。
            # 🐛 旧 `_setStreamingDock(!!_act||!_co)` 在已结束卡片重建（新骨架 DOM 无
            # data-collapsed 属性 → _co=false）时误开坞态 → 正文限矮、工具区沉底，
            # 是「后台标签页切回后卡片重现流式结构」的直接来源之一。
            # 此 JS 在上方 collapse 之后执行，保证历史卡 data-collapsed 已更新。
            # S2 语义保留：DOM 中存在运行中工具块（data-streaming="true"）时仍强制
            # dock on，覆盖「JS 就绪晚于工具流式注入」的竞态窗口。
            # 🛡️ 欢迎卡片（light 骨架）跳过：坞态会限死正文高度，欢迎页长内容被截断。
            if not self._light_skeleton:
                _dock_on = "true" if self._streaming else "false"
                self.page().runJavaScript(
                    "var _act=document.querySelector('#tool-content [data-tool-call-id][data-streaming=\"true\"]');"
                    f"if(typeof _setStreamingDock==='function')_setStreamingDock({_dock_on}||!!_act);"
                )
        except RuntimeError:
            pass
        # 🐛 修复：流式内容可能在 JS 就绪前通过 _lazy_markdown_cb 缓存，
        # 仅检查 _markdown_text 会遗漏这些内容，导致卡片永久空白。
        # 当 _lazy_markdown_cb 存在时也触发渲染，_perform_update 会消费它。
        # 🛡️ F2：_render_deferred 积压（JS 未就绪期间 / 隐藏期间门控的渲染请求）
        # 在 JS 就绪时统一补渲——保证 viewer 创建后未显示 + JS 未加载期间的
        # 积压渲染在恢复后必然补上（可以延迟，不能丢失）。
        if self._render_deferred or self._markdown_text or self._lazy_markdown_cb:
            self._render_deferred = False
            self._schedule_render(immediate=True)
        # [B4-强回收] 记录 renderer 进程 PID（强回收层 kill 离屏进程用）
        try:
            self._renderer_pid = self.page().renderProcessPid()
        except Exception:
            self._renderer_pid = 0

    def _load_skeleton(self):
        # 获取系统字体
        font_family = "Segoe UI, sans-serif"
        try:
            from app.utils.config import Settings

            settings = Settings.get_instance()
            font_family = settings.llm_font_family.value
            if not font_family:
                font_family = settings.canvas_font_selected.value or "Segoe UI, sans-serif"
        except Exception:
            pass

        self._viewer_font_family = font_family
        self._viewer_font_css = (
            f"{get_font_family_css()} font-family: {font_family}, sans-serif; font-size: {scale_font_size(14)}px;"
        )

        # ── 骨架全局缓存：多张卡片共享同一份 HTML 模板 ──
        # 缓存键由主题色 + 字体 + light 模式组成，最多 ~8 条 × 54KB ≈ 432KB
        theme = current_theme()
        body_font_size = scale_font_size(14)
        code_font_size = scale_font_size(13)
        tag_font_size = scale_font_size(12)
        small_font_size = scale_font_size(11)
        tiny_font_size = scale_font_size(10)
        font_family_global = _get_global_font()

        theme_fp = json.dumps({k: theme[k] for k in sorted(theme)}, option=json.OPT_SORT_KEYS).decode("utf-8")
        # mermaid vendor URL 进 key：热替换 vendor 文件后能让旧骨架缓存失效
        _mmd_polyfill_url, _mmd_lib_url = _get_mermaid_vendor_urls()
        # KaTeX vendor URL 进 key：与 mermaid 同策略，热替换后骨架缓存失效
        _katex_css_url, _katex_js_url = _get_katex_urls()
        # ECharts vendor URL 进 key：同策略。echarts 改懒加载后只在真正有图表时才
        # 加载，但 URL 变化仍需让旧骨架失效（否则旧骨架里的旧 URL 被复用）。
        _echarts_lib_url, _echarts_wordcloud_url = _get_echarts_vendor_urls()
        # 插件 fence 渲染器的 assets 表（file:// URL + 权限）内联进骨架；
        # 签名（路径 + mtime）进 key，插件 assets 热替换后旧骨架自动失效。
        _fence_assets_js, _fence_perms_js, _fence_assets_sig = _fence_assets_for_skeleton()
        _widget_grants_js = _js_literal({_p: True for _p in _BUILTIN_FENCE_PERMS.get("widget", [])})
        # mermaid 主题联动：取 Colors 而非硬编码，避免浅色主题下白叠白
        # （MEMORY.md 记录的反复出现的缺陷模式）。Colors 大写属性由主题 YAML 自动填充。
        mmd_text_color = Colors.TEXT_PRIMARY
        mmd_line_color = Colors.TEXT_SECONDARY
        mmd_node_bg = Colors.CONTENT_BG
        mmd_border = Colors.BORDER
        cache_key = (
            # 🆕 方案 A（#33）：骨架缓存版本号——骨架 JS/DOM 结构变更时递增，
            # 防止旧版骨架缓存与新代码混合导致 JS 行为不一致（卡片空白根因之一）。
            _SKELETON_CACHE_VERSION,
            self._light_skeleton,
            theme_fp,
            font_family,
            font_family_global,
            body_font_size,
            code_font_size,
            tag_font_size,
            small_font_size,
            tiny_font_size,
            _mmd_polyfill_url,
            _mmd_lib_url,
            _katex_css_url,
            _katex_js_url,
            _echarts_lib_url,
            _echarts_wordcloud_url,
            _fence_assets_sig,
            mmd_text_color,
            mmd_line_color,
            mmd_node_bg,
            mmd_border,
        )
        cached = _skeleton_cache.get(cache_key)
        if cached is not None:
            _skeleton_cache.move_to_end(cache_key)  # LRU：命中提升为最新
            self.setHtml(cached, QUrl.fromLocalFile(_PROJECT_ROOT + "/"))
            return

        tag_css = []
        for act, col in ACTION_COLOR_MAP.items():
            tag_css.append(
                f'.context-tag[data-type="{act}"] {{ background: {col}15; border-color: {col}60; color: {col}; }}'
            )
            tag_css.append(f'.context-tag[data-type="{act}"]:hover {{ background: {col}30; border-color: {col}; }}')

        # vendor JS 全部改为懒加载（mermaid / KaTeX / ECharts 同策略）：骨架不再
        # 常驻任何 vendor script 标签。原先 echarts.min.js(1MB) + wordcloud 被写进
        # 骨架并被 _skeleton_cache 缓存 → 每条消息都背上 1MB，哪怕整篇没有图表。
        # 现由 JS 侧首次遇到对应 fence 时动态加载（_echartsEnsure / _mmdEnsure /
        # _katexEnsure），欢迎卡片等 light 骨架场景同样走这条路径（_initEchartsIn
        # 内部会先 ensure 再扫描，不依赖 window.echarts 预先存在）。
        cdn_libs = ""

        # 检测浅色/深色模式，用于滚动条和行内差异框主题适配
        try:
            from app.utils.theme_manager import theme_manager

            _is_light = theme_manager.is_light_theme()
        except Exception:
            _is_light = False

        if _is_light:
            # 浅色模式滚动条 — 半透明灰色，在不同浅色主題背景上都自然
            scrollbar_css = """
                ::-webkit-scrollbar {
                    width: 6px;
                    height: 6px;
                }
                ::-webkit-scrollbar-track {
                    background: transparent;
                    border-radius: 3px;
                    margin: 2px 0;
                }
                ::-webkit-scrollbar-track:hover {
                    background: rgba(0, 0, 0, 0.04);
                }
                ::-webkit-scrollbar-thumb {
                    background: rgba(0, 0, 0, 0.18);
                    border-radius: 3px;
                    min-height: 24px;
                }
                ::-webkit-scrollbar-thumb:hover {
                    background: rgba(0, 0, 0, 0.28);
                }
                ::-webkit-scrollbar-thumb:active {
                    background: rgba(0, 0, 0, 0.35);
                }
                ::-webkit-scrollbar-corner {
                    background: transparent;
                }
                /* Firefox 滚动条 */
                * {
                    scrollbar-width: thin;
                    scrollbar-color: rgba(0, 0, 0, 0.18) transparent;
                }
            """
        else:
            # 深色模式滚动条 — 保留原有的精致深色风格
            scrollbar_css = """
                ::-webkit-scrollbar {
                    width: 6px;
                    height: 6px;
                }
                ::-webkit-scrollbar-track {
                    background: #1a1f2e;
                    border-radius: 3px;
                    margin: 2px 0;
                }
                ::-webkit-scrollbar-track:hover {
                    background: #1e2435;
                }
                ::-webkit-scrollbar-thumb {
                    background: #3a3f50;
                    border-radius: 3px;
                    min-height: 24px;
                }
                ::-webkit-scrollbar-thumb:hover {
                    background: #4a4f62;
                }
                ::-webkit-scrollbar-thumb:active {
                    background: #5a5f72;
                }
                ::-webkit-scrollbar-corner {
                    background: #1a1f2e;
                }
                /* Firefox 滚动条 */
                * {
                    scrollbar-width: thin;
                    scrollbar-color: #3a3f50 #1a1f2e;
                }
            """
        _is_light_diff = _is_light
        mono_font = f"{font_family_global}, Consolas, monospace"

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            {cdn_libs}
            <style>
                :root {{
                    --bg: transparent;
                    --panel: {theme["card_bg_solid"]};
                    --panel-elevated: {theme["card_bg_solid"]};
                    --panel-soft: {theme["content_bg"]};
                    --border: {theme["border"]};
                    --border-strong: {theme["border_accent"]};
                    --text: {theme["text_primary"]};
                    --text-secondary: {theme["text_secondary"]};
                    --text-muted: {theme["text_muted"]};
                    --accent: {theme["accent"]};
                    --accent-warm: {theme["accent_warm"]};
                    --code-bg: {"var(--panel-soft)" if _is_light_diff else "transparent"};
                    --code-toolbar: {"rgba(0,0,0,0.03)" if _is_light_diff else "rgba(255, 255, 255, 0.03)"};
                    --code-border: {"var(--border)" if _is_light_diff else "#2a3447"};
                    --success: #5fd18c;
                    --danger: #ff7b7b;
                    /* 语义派生层：欢迎卡/表格等组件用，浅/深主题通吃（P0 去硬编码） */
                    --accent-text: {theme["accent"]};
                    --accent-soft: {theme["hover_bg"]};
                    --accent-soft-strong: {theme["selected_bg"]};
                    --accent-border-weak: {_accent_rgba(theme["accent"], 0.22)};
                    --accent-glow: {_accent_rgba(theme["accent"], 0.10)};
                    --row-alt: {"rgba(15, 23, 42, 0.03)" if _is_light else "rgba(255, 255, 255, 0.02)"};
                    --row-hover: {"rgba(15, 23, 42, 0.05)" if _is_light else "rgba(255, 255, 255, 0.05)"};
                    /* 表头底色：比 --row-hover 更实一层，浅色主题下用深色叠加而非白叠加，
                       否则白底上叠白 = 表头与表体完全无分界（此前的硬编码缺陷）。 */
                    --row-header: {"rgba(15, 23, 42, 0.06)" if _is_light else "rgba(255, 255, 255, 0.04)"};
                    /* 圆角节奏（与 design_tokens.BorderRadius 同源，4/6/10/14/18/全圆） */
                    {_BORDER_RADIUS_CSS_VARS}
                }}
                html {{
                    overflow: hidden;
                    /* 🛡️ 阻止 Chromium 视口创建滚动条。
                       卡片内容完全展开，由父级滚动容器处理滚动。
                       流式渲染期间内容可能暂时超出视口，
                       但 opacity transition 遮盖了短暂裁剪。 */
                }}
                html, body {{
                    background: var(--bg) !important;
                    color: var(--text);
                    {self._viewer_font_css}
                    margin: 0; 
                    padding: 0;
                }}
                body {{
                    /* 右侧 8px = 14px 视觉边距 - 6px 常驻滚动轨道：轨道占位使
                       内容右视觉边距比左侧多 6px，扣减后左右对称 */
                    padding: 6px 8px 0 14px;
                    /* ⚠️ 安全网，非常态：MAX_HEIGHT 已抬到 10000px（见类常量注释），
                       真实内容几乎不会触及 → body 不会溢出 → 不出现内滚条 →
                       滚动统一由外层 chat_scroll_area 承载。
                       触及上限时（极端长内容）才回退为卡内滚动，此时 wheelEvent 的
                       内外转发逻辑仍然生效，是最后一道兜底。 */
                    max-height: {self.MAX_HEIGHT}px;
                    /* 🛡️ 稳定性修复：滚动条轨道常驻，内容可用宽度恒定。
                       overflow-y:auto 时滚动条出现/消失会使内容宽度 ±6px 波动 →
                       长行换行变化 → scrollHeight 波动 → 高度报告 → setFixedHeight
                       → 滚动条再切换 → 反馈振荡（流式"一抖一抖"的主因之一）。
                       scroll 常驻轨道后宽度恒定，斩断反馈环。track 透明无视觉噪点。 */
                    overflow-y: scroll;
                    overflow-x: hidden;
                    overflow-anchor: auto;
                }}
                /* body 滚动轨道常驻但视觉隐形（覆盖全局 6px 滚动条样式的 track 底色） */
                body::-webkit-scrollbar-track {{
                    background: transparent;
                }}
                /* 内层滚动容器（工具区/思考体/工具结果）轨道同样隐形：
                   常驻轨道(scroll) + 右 padding 扣减 6px，消除滚动条带来的右侧加宽 */
                #tool-content::-webkit-scrollbar-track,
                .think-content::-webkit-scrollbar-track,
                .result-content::-webkit-scrollbar-track {{
                    background: transparent;
                }}
                {scrollbar_css}

                #content-placeholder {{
                    color: var(--text);
                    /* 平滑过渡：全量渲染时内容以轻微透明度淡入替代生硬闪烁 */
                    transition: opacity 150ms ease;
                    will-change: opacity;
                }}
                #content-placeholder * {{ color: inherit; }}
                /* 图片自适应卡片宽度 */
                #content-placeholder img {{
                    max-width: 100%;
                    height: auto;
                    border-radius: var(--r-md);
                    display: block;
                    margin: 8px 0;
                    object-fit: contain;
                }}
                /* 工具/思考块内的图标小图不应用圆角裁剪，保持原样显示 */
                #content-placeholder .tool-block img,
                #content-placeholder .think-block img,
                #content-placeholder .think-compact img,
                #content-placeholder .think-streaming img {{
                    border-radius: 0;
                    display: inline;
                    margin: 0;
                    max-width: none;
                }}
                /* 排版优化：标题改用负字距（-0.01em）——字号越大越需收紧字距，
                   这是通行排版实践；正字距会让大标题显得松散。
                   同时统一标题行高 1.3，避免大字号下默认行高过松。 */
                h1, h2, h3, h4, h5, h6 {{
                    color: var(--text) !important;
                    font-weight: 700;
                    letter-spacing: -0.01em;
                    line-height: 1.3;
                }}
                /* 上边距 > 下边距：标题在视觉上归属于其后的内容（格式塔接近原则） */
                h1 {{ font-size: 1.45em; margin: 18px 0 8px; }}
                h2 {{ font-size: 1.25em; margin: 16px 0 6px; }}
                h3 {{ font-size: 1.1em; margin: 14px 0 4px; }}
                /* 正文行高 1.65：中英混排下兼顾可读性与密度（浏览器默认 ~1.2 过挤） */
                p {{ margin: 8px 0; color: var(--text-secondary); line-height: 1.65; }}
                a {{ color: var(--accent) !important; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
                ul, ol {{ margin: 8px 0; padding-left: 24px; line-height: 1.65; }}
                li {{ margin: 4px 0; color: var(--text-secondary); }}
                strong {{ color: var(--text) !important; font-weight: 600; }}
                em {{ color: var(--text-secondary) !important; font-style: italic; }}
                code:not(.code-content *):not(pre code) {{ 
                    background: var(--accent-glow) !important; 
                    color: var(--accent-text) !important;
                    padding: 2px 6px; 
                    border-radius: var(--r-sm); 
                    font-family: {mono_font};
                    font-size: {code_font_size}px;
                }}
                hr {{ border: none; border-top: 1px solid var(--border); margin: 14px 0; }}

                /* 优化：移除首尾元素的边距，彻底消除多余空白 */
                #content-placeholder > :first-child {{ margin-top: 0 !important; }}
                #content-placeholder > :last-child {{ margin-bottom: 0 !important; }}
                /* 解决 Chromium 滚动容器 padding-bottom 不生效的 bug */
                #content-placeholder::after {{
                    content: '';
                    display: block;
                    height: 5px;
                }}

                /* 优化：紧凑的段落间距 */
                p {{ margin: 8px 0; }}

                /* ── 原生 <table> 样式（保留 display:table，自动拉伸填满） ── */
                table:not(.code-table):not(.layout-table) {{
                    width: 100%;
                    border-collapse: collapse;
                    margin: 10px 0;
                    background: transparent;
                    border: 1px solid var(--border);
                    border-radius: var(--r-md);
                    overflow: hidden;
                    font-family: '{font_family}', sans-serif;
                    font-size: {body_font_size}px;
                }}
                table:not(.code-table):not(.layout-table) th {{
                    /* 原为硬编码 rgba(255,255,255,0.04)：浅色主题下白叠白，表头
                       与表体完全无分界。改用主题感知的 --row-header。 */
                    background: var(--row-header);
                    padding: 8px 12px;
                    text-align: left;
                    font-weight: 600;
                    color: var(--text) !important;
                    border-bottom: 1px solid var(--border-strong);
                }}
                table:not(.code-table):not(.layout-table) td {{
                    padding: 8px 12px;
                    border-bottom: 1px solid var(--border);
                    color: var(--text-secondary) !important;
                }}
                /* 原为硬编码白色叠加，浅色主题不可见 → 改用已定义的语义变量 */
                table:not(.code-table):not(.layout-table) tr:nth-child(even) {{ background: var(--row-alt); }}
                table:not(.code-table):not(.layout-table) tr:hover {{ background: var(--row-hover); }}
                /* 表体行 hover 时文字提亮，增强可扫描性 */
                table:not(.code-table):not(.layout-table) tr:hover td {{ color: var(--text) !important; }}

                /* ── 表格滚动容器（JS 在 updateContent 中自动包裹每个 <table>） ── */
                .table-scroll-wrapper {{
                    overflow-x: auto;
                    overflow-y: hidden;
                    margin: 10px 0;
                    border: 1px solid var(--border);
                    border-radius: var(--r-md);
                }}
                .table-scroll-wrapper::-webkit-scrollbar {{
                    height: 8px;
                }}
                .table-scroll-wrapper::-webkit-scrollbar-thumb {{
                    background: var(--border);
                    border-radius: var(--r-xs);
                }}
                .table-scroll-wrapper::-webkit-scrollbar-thumb:hover {{
                    background: var(--border-strong);
                }}
                .table-scroll-wrapper::-webkit-scrollbar-track {{
                    background: transparent;
                }}
                .table-scroll-wrapper > table {{
                    width: 100%;
                    border-collapse: collapse;
                    background: transparent;
                    font-family: '{font_family}', sans-serif;
                    font-size: {body_font_size}px;
                    margin: 0;
                    border: none !important;
                    border-radius: 0 !important;
                }}
                .table-scroll-wrapper > table th,
                .table-scroll-wrapper > table td {{
                    white-space: normal;
                    word-break: break-word;
                }}
                /* 继承 wrapper 内部表格的行样式 */
                .table-scroll-wrapper > table th {{
                    /* 与上方 table th 同步：改用主题感知变量，修复浅色主题白叠白 */
                    background: var(--row-header);
                    padding: 8px 12px;
                    text-align: left;
                    font-weight: 600;
                    color: var(--text) !important;
                    border-bottom: 1px solid var(--border-strong);
                }}
                .table-scroll-wrapper > table td {{
                    padding: 8px 12px;
                    border-bottom: 1px solid var(--border);
                    color: var(--text-secondary) !important;
                    max-height: 3.8em;
                    overflow-y: auto;
                    vertical-align: top;
                }}
                .table-scroll-wrapper > table tr:nth-child(even) {{ background: var(--row-alt); }}
                .table-scroll-wrapper > table tr:hover {{ background: var(--row-hover); }}

                .context-tag {{
                    display: inline-block;
                    padding: 2px 8px;
                    margin: 0 2px;
                    border: 1px solid transparent;
                    border-radius: 999px;
                    font-size: {tag_font_size}px;
                    font-weight: 700;
                    cursor: pointer;
                    transition: 0.18s ease;
                    vertical-align: middle;
                }}
                {"".join(tag_css)}
                /* SVG 图形节点（<g> / <rect> / <circle> …）复用 .context-tag
                   点击链。SVG 不支持 background / border，悬停反馈只能用
                   opacity —— 没有它用户看不出图节点可点。 */
                svg .context-tag {{
                    cursor: pointer;
                }}
                svg .context-tag:hover {{
                    opacity: 0.78;
                }}

                /* 追问区块：正文里的 <ask> 由 _inject_context_links 摘除去重后，
                   统一渲染在文末。整行 hover 点亮 + 右侧「发送」提示，弱化原来
                   高饱和红胶囊的存在感。 */
                .ask-suggest {{
                    margin: 16px 0 2px;
                    padding-top: 12px;
                    border-top: 1px solid var(--border);
                }}
                .ask-suggest-title {{
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    margin-bottom: 8px;
                    font-size: {small_font_size}px;
                    font-weight: 600;
                    color: var(--text-muted);
                }}
                .ask-suggest-dot {{
                    width: 6px;
                    height: 6px;
                    border-radius: 50%;
                    background: var(--accent);
                    flex: none;
                }}
                .ask-suggest-list {{
                    display: flex;
                    flex-direction: column;
                    gap: 2px;
                }}
                .ask-suggest .context-tag {{
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    gap: 10px;
                    padding: 6px 10px;
                    margin: 0;
                    border: 1px solid transparent;
                    border-radius: 8px;
                    background: transparent;
                    color: var(--text-secondary);
                    font-weight: 400;
                    transition: 0.18s ease;
                }}
                .ask-suggest .context-tag::after {{
                    content: "发送 ›";
                    flex: none;
                    font-size: {tiny_font_size}px;
                    color: var(--accent-text);
                    opacity: 0;
                    transform: translateX(-4px);
                    transition: 0.18s ease;
                }}
                .ask-suggest .context-tag:hover {{
                    background: var(--accent-soft);
                    border-color: var(--accent-border-weak);
                    color: var(--accent-text);
                }}
                .ask-suggest .context-tag:hover::after {{
                    opacity: 1;
                    transform: translateX(0);
                }}

                /* session 历史会话标签样式（胶囊按内容宽度自然展开） */
                .session-tag {{
                    background: var(--accent-soft);
                    border-color: var(--accent-border-weak);
                    color: var(--accent-text);
                    margin: 4px 6px 4px 0;
                    max-width: 100%;
                }}
                .session-tag:hover {{
                    background: var(--accent-soft-strong);
                    border-color: var(--accent);
                }}
                /* session 时间显示在标题下方 */
                .session-tag .session-time {{
                    display: block;
                    font-size: {tiny_font_size}px;
                    font-weight: normal;
                    opacity: 0.6;
                    margin-top: 4px;
                    color: var(--accent-text);
                }}

                /* 欢迎卡片历史会话：分区标题 + 卡片行列表 */
                .session-section {{
                    margin: 4px 0 14px;
                }}
                .session-header {{
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    margin-bottom: 8px;
                }}
                .session-header-title {{
                    font-size: {tag_font_size}px;
                    font-weight: 600;
                    color: var(--text);
                    letter-spacing: 0.02em;
                }}
                /* 分区右侧「全部」快捷按钮：打开工作台历史会话页（复用 context-tag 点击链） */
                .session-header-more {{
                    margin-left: auto;
                    margin-right: 0;
                    font-size: {tiny_font_size}px;
                    font-weight: 500;
                    color: var(--accent-text);
                    background: var(--accent-soft);
                    border: 1px solid var(--accent-border-weak);
                    padding: 0 10px;
                    border-radius: 999px;
                    line-height: 1.7;
                    cursor: pointer;
                    transition: 0.18s ease;
                }}
                .session-header-more:hover {{
                    background: var(--accent-soft-strong);
                    border-color: var(--accent);
                }}
                .session-list {{
                    display: grid;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                    gap: 6px 8px;
                }}
                /* 会话卡片行：复用 .context-tag 点击事件链，覆盖胶囊默认外观 */
                .session-item.context-tag {{
                    display: flex;
                    align-items: center;
                    gap: 10px;
                    width: 100%;
                    box-sizing: border-box;
                    padding: 8px 12px;
                    margin: 0;
                    border-radius: 10px;
                    border: 1px solid var(--border);
                    background: var(--accent-soft);
                    font-weight: 500;
                    color: var(--text);
                    cursor: pointer;
                    transition: background 0.18s ease, border-color 0.18s ease,
                                transform 0.18s ease, box-shadow 0.18s ease;
                    max-width: 100%;
                }}
                .session-item.context-tag:hover {{
                    background: var(--accent-soft-strong);
                    border-color: var(--accent);
                    transform: translateX(2px);
                    box-shadow: 0 2px 10px var(--accent-glow);
                }}
                .session-item-body {{
                    flex: 1;
                    min-width: 0;
                    display: flex;
                    flex-direction: column;
                    gap: 2px;
                }}
                .session-item-title {{
                    font-size: {tag_font_size}px;
                    font-weight: 600;
                    color: var(--text);
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }}
                .session-item-meta {{
                    font-size: {tiny_font_size}px;
                    color: var(--text-muted);
                    opacity: 0.85;
                }}
                .session-item-arrow {{
                    flex: 0 0 auto;
                    font-size: 15px;
                    color: var(--text-muted);
                    opacity: 0;
                    transform: translateX(-4px);
                    transition: opacity 0.18s ease, transform 0.18s ease;
                    line-height: 1;
                }}
                /* 会话卡右侧状态 tag：最近=相对时间 / 最活跃=消息数，同一套中性色 */
                .session-item-tag {{
                    flex: 0 0 auto;
                    font-size: {tiny_font_size}px;
                    font-weight: 600;
                    color: var(--accent-text);
                    background: var(--accent-soft-strong);
                    padding: 2px 8px;
                    border-radius: 6px;
                    line-height: 1.4;
                    white-space: nowrap;
                }}
                .session-item.context-tag:hover .session-item-arrow {{
                    opacity: 1;
                    transform: translateX(0);
                }}
                /* 卡片进入动画：逐行 stagger fade-in（backwards 保证延迟期隐藏，
                   播完恢复自然样式，不锁死 transform，hover 位移不受影响） */
                @keyframes session-item-in {{
                    from {{ opacity: 0; transform: translateY(6px); }}
                    to {{ opacity: 1; transform: translateY(0); }}
                }}
                .session-item.context-tag {{
                    animation: session-item-in 0.32s ease backwards;
                }}
                @media (prefers-reduced-motion: reduce) {{
                    .session-item.context-tag {{ animation: none; }}
                }}
                .welcome-empty {{
                    opacity: 0.55;
                    font-size: {tag_font_size}px;
                    padding: 8px 0;
                }}

                /* 代码块通用样式 */
                .code-table {{ width: 100%; border-collapse: collapse; }}
                .code-table td {{ padding: 0; vertical-align: top; }}
                .lineno {{ width: 32px; text-align: right; padding-right: 8px !important; color: #606060; border-right: 1px solid #404040; user-select: none; font-size: {
            small_font_size
        }px; line-height: 1.5; }}
                /* 优化后的代码块布局：行号固定，代码可横向滚动 */
                .code-container {{
                    display: flex;
                    overflow-x: auto;
                    overflow-y: hidden;
                    background: transparent;
                    font-family: {mono_font};
                    font-size: {code_font_size}px;
                    line-height: 1.5;
                    padding: 0 10px 8px 0;
                    margin: 0;
                }}
                .line-numbers {{
                    flex: 0 0 auto;
                    text-align: right;
                    padding-right: 12px;
                    color: var(--text-muted);
                    border-right: 1px solid var(--code-border);
                    user-select: none; /* 关键：禁止复制行号 */
                    white-space: pre;
                    min-width: 32px;
                    overflow: hidden;
                }}
                .code-content {{
                    flex: 1;
                    overflow-x: auto;
                    overflow-y: hidden;
                    padding-left: 12px;
                }}
                .code-content pre {{
                    margin: 0 !important;
                    white-space: pre;
                    word-wrap: normal;
                    overflow: visible;
                    background: transparent !important;
                    font-family: {mono_font} !important;
                    font-size: {code_font_size}px !important;
                    line-height: 1.5 !important;
                }}
                .code-line {{ padding-left: 12px !important; white-space: pre; font-family: {mono_font}; }}

                /* 代码工具按钮 hover：原为硬编码白色叠加，浅色主题下几乎不可见。
                   改为按主题取反色叠加，保证两种主题下都有明确反馈。 */
                .code-btn:hover {{
                    background: {"rgba(15, 23, 42, 0.08)" if _is_light_diff else "rgba(255,255,255,0.08)"} !important;
                }}

                .cm-collapsible {{
                    overflow: hidden;
                    transform: translateZ(0);
                    backface-visibility: hidden;
                    contain: layout style;
                }}
                .cm-collapsible__summary {{
                    width: 100%;
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    background: transparent;
                    border: none;
                    text-align: left;
                    cursor: pointer;
                    outline: none;
                    -webkit-tap-highlight-color: transparent;
                }}
                .cm-collapsible__summary:focus-visible {{
                    box-shadow: inset 0 0 0 1px rgba(102, 198, 255, 0.28);
                }}
                .cm-collapsible__chevron {{
                    flex: 0 0 auto;
                    width: 6px;
                    height: 6px;
                    border-right: 1.5px solid currentColor;
                    border-bottom: 1.5px solid currentColor;
                    transform: rotate(45deg);
                    transform-origin: center;
                    transition: transform 180ms ease;
                    margin-left: 2px;
                    opacity: 0.85;
                }}
                .cm-collapsible[data-expanded="true"] .cm-collapsible__chevron {{
                    transform: rotate(225deg);
                }}
                .cm-collapsible__body {{
                    height: 0;
                    opacity: 0;
                    overflow: hidden;
                    will-change: height, opacity;
                    transition: height 250ms cubic-bezier(0.4, 0, 0.2, 1), opacity 200ms ease;
                }}
                .cm-collapsible[data-expanded="true"] .cm-collapsible__body {{
                    opacity: 1;
                }}

                .think-block {{
                    margin: 4px 0;
                    background: transparent;
                    border: none;
                    border-radius: 6px;
                }}
                .think-compact {{
                    background: transparent;
                    border: none;
                }}
                .think-block[data-expanded="true"] {{
                    border: none;
                }}
                .think-block__summary {{
                    padding: 5px 10px;
                    color: var(--text-secondary);
                    font-weight: 600;
                }}
                /* 流式思考纯文本块（无折叠UI）— 金色圆环 + 背景 */
                .think-streaming {{
                    margin: 4px 0;
                    background: transparent;
                    border: none;
                    border-radius: 6px;
                    padding: 8px 10px;
                    color: var(--text-secondary);
                    font-style: italic;
                    transition: border-color 220ms ease, background 220ms ease;
                }}
                .think-streaming[data-streaming="true"] {{
                    background: transparent;
                }}
                /* 思考轮播提示文字 — 从左到右脉冲渐变色动画 */
                .think-streaming-tip {{
                    background: linear-gradient(
                        90deg,
                        var(--text-secondary) 0%,
                        var(--accent) 45%,
                        var(--accent-warm) 55%,
                        var(--text-secondary) 100%
                    );
                    background-size: 200% 100%;
                    background-clip: text;
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    animation: think-tip-sweep 2.5s ease-in-out infinite;
                }}
                @keyframes think-tip-sweep {{
                    0% {{ background-position: 200% 0; }}
                    100% {{ background-position: -200% 0; }}
                }}
                /* 工具运行卡片的参数预览 — 流式态脉冲渐变色动画 */
                .tool-streaming-block[data-streaming="true"] .tool-streaming-preview {{
                    background: linear-gradient(
                        90deg,
                        var(--text-secondary) 0%,
                        var(--accent) 45%,
                        var(--accent-warm) 55%,
                        var(--text-secondary) 100%
                    );
                    background-size: 200% 100%;
                    background-clip: text;
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    animation: think-tip-sweep 2.5s ease-in-out infinite;
                }}
                .think-content {{
                    /* 右 4px = 10px - 6px 滚动轨道：轨道常驻后内容宽度稳定，右侧视觉边距与左对称 */
                    padding: 8px 4px 8px 10px;
                    border-top: 1px solid var(--border);
                    background: transparent;
                    color: var(--text-secondary) !important;
                    font-style: italic;
                    font-size: {code_font_size + 2}px;
                    font-family: '{font_family}', sans-serif;
                    line-height: 1.6;
                    max-height: 500px;
                    overflow-y: scroll;
                    /* 🐛 修复（偶发横向滚动条）：CSS 规范规定一轴非 visible 时另一轴 visible
                       会被自动计算为 auto，未显式声明会导致内部 .code-container / .table-scroll-wrapper
                       等 overflow-x:auto 的子容器在内容超宽时撑出整个折叠框的横向滚动条 */
                    overflow-x: hidden;
                    transition: opacity 200ms ease;
                }}
                /* 思考内容加载骨架屏动画
                   原为硬编码白色渐变：浅色主题下白叠白，骨架屏完全看不见
                   （用户会误以为内容没加载）。改为按主题选择反色叠加。 */
                .think-content.loading {{
                    background-image: linear-gradient(
                        90deg,
                        {"rgba(15, 23, 42, 0.03)" if _is_light_diff else "rgba(255, 255, 255, 0.02)"} 25%,
                        {"rgba(15, 23, 42, 0.07)" if _is_light_diff else "rgba(255, 255, 255, 0.05)"} 50%,
                        {"rgba(15, 23, 42, 0.03)" if _is_light_diff else "rgba(255, 255, 255, 0.02)"} 75%
                    );
                    background-size: 200% 100%;
                    animation: think-shimmer 1.5s ease-in-out infinite;
                }}
                @keyframes think-shimmer {{
                    0% {{ background-position: 200% 0; }}
                    100% {{ background-position: -200% 0; }}
                }}
                /* 思考流式预览 — 默认静态色 */
                .think-streaming-preview {{
                    position: relative;
                    color: var(--text-secondary);
                }}
                /* 流式状态：::after 伪元素叠加流动光效，不触碰文字层 */
                .think-block[data-streaming="true"] .think-streaming-preview::after {{
                    content: '';
                    position: absolute;
                    inset: 0;
                    pointer-events: none;
                    background: linear-gradient(
                        90deg,
                        transparent 0%,
                        rgba(255, 200, 50, 0.05) 45%,
                        rgba(255, 200, 50, 0.10) 50%,
                        rgba(255, 200, 50, 0.05) 55%,
                        transparent 100%
                    );
                    background-size: 250% 100%;
                    animation: think-shimmer 3s ease-in-out infinite;
                }}
                /* 思考中蛇形爬行动画 */
                .think-block .think-block__summary {{
                    transition: background-color 220ms ease;
                }}
                /* 流式思考中的标题底色：原为硬编码白色叠加，浅色主题下不可见
                   （用户感知不到"思考中"的状态反馈）。改为主题感知反色叠加。 */
                .think-block[data-streaming="true"] .think-block__summary {{
                    background: {"rgba(15, 23, 42, 0.05)" if _is_light_diff else "rgba(255, 255, 255, 0.04)"};
                }}
                .think-snake {{
                    display: inline-block;
                    vertical-align: middle;
                    margin-right: 2px;
                }}
                .think-snake-arc {{
                    transform-origin: 12px 12px;
                }}

                /* 工具流式调用块 — 金色圆环动画背景 */
                .tool-streaming-block .tool-block__summary {{
                    transition: background-color 220ms ease;
                }}
                .tool-streaming-block[data-streaming="true"] .tool-block__summary {{
                    background: rgba(255, 200, 50, 0.05);
                }}
                /* spinner 和状态文字的平滑过渡 */
                .tool-streaming-spinner {{
                    transition: opacity 220ms ease, transform 220ms ease;
                }}
                .tool-streaming-block[data-streaming="false"] .tool-streaming-spinner {{
                    opacity: 0;
                    transform: scale(0.7);
                }}
                .tool-streaming-block[data-streaming="true"] .tool-streaming-spinner {{
                    opacity: 1;
                    transform: scale(1);
                }}

                .tool-block {{
                    margin: 4px 0;
                    background: transparent;
                    border: none;
                    border-radius: 6px;
                    box-shadow: none;
                }}
                .tool-block[data-expanded="true"] {{
                    border: none;
                }}
                .tool-block__summary {{
                    padding: 5px 10px;
                    color: var(--accent);
                    font-weight: 600;
                    font-size: {code_font_size}px;
                    font-family: '{font_family}', sans-serif;
                    white-space: normal;
                }}
                .tool-expanded-content {{
                    padding: 0;
                }}
                .tool-diff-stats {{
                    display: inline-flex;
                    align-items: center;
                    gap: 3px;
                    margin-left: 4px;
                    padding: 1px 6px;
                    border: 1px solid rgba(139, 148, 158, 0.2);
                    border-radius: 999px;
                    background: rgba(139, 148, 158, 0.08);
                    font-weight: 700;
                    white-space: nowrap;
                }}
                .tool-diff-stats__add {{
                    color: #3fb950;
                }}
                .tool-diff-stats__del {{
                    color: #ff7b72;
                }}
                .tool-diff-stats__sep {{
                    color: {"var(--text-muted)" if _is_light_diff else "#6e7681"};
                }}
                .tool-diff-inline {{
                    margin: 0;
                    background: {"var(--panel-soft)" if _is_light_diff else "rgba(13,17,23,0.40)"};
                    border: 1px solid {"var(--border)" if _is_light_diff else "rgba(48,54,61,0.25)"};
                    border-radius: 8px;
                    overflow: hidden;
                }}
                .tool-diff-inline__header {{
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    min-width: 0;
                    padding: 4px 10px;
                    background: {"rgba(0,0,0,0.03)" if _is_light_diff else "rgba(22,27,34,0.40)"};
                    border-bottom: 1px solid {"var(--border)" if _is_light_diff else "rgba(48,54,61,0.25)"};
                    color: {"var(--text-secondary)" if _is_light_diff else "#8b949e"};
                    font-size: {small_font_size}px;
                    font-weight: 600;
                }}
                .tool-diff-inline__title {{
                    flex: 0 0 auto;
                    color: {"var(--text)" if _is_light_diff else "#d0d7de"};
                    letter-spacing: 0;
                }}
                .tool-diff-inline__file {{
                    flex: 1 1 auto;
                    min-width: 0;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                    color: {"var(--text-secondary)" if _is_light_diff else "#8b949e"};
                    font-weight: 500;
                }}
                .tool-diff-inline__summary {{
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    flex: 0 0 auto;
                    padding: 2px 7px;
                    border-radius: 999px;
                    /* 早先写死 rgba(13,17,23,.42)，浅色主题下是一枚深色药丸；与相邻规则一样按主题取色 */
                    background: {"rgba(0,0,0,0.04)" if _is_light_diff else "rgba(13,17,23,0.42)"};
                    border: 1px solid rgba(139, 148, 158, 0.18);
                    font-weight: 800;
                }}
                .tool-diff-inline__add {{
                    color: #56d364;
                }}
                .tool-diff-inline__del {{
                    color: #ff7b72;
                }}
                .tool-diff-inline__body {{
                    line-height: 1.55;
                    overflow-x: auto;
                }}
                .tool-diff-inline .diff-line {{
                    display: flex;
                    align-items: stretch;
                    min-height: 23px;
                    font-size: {tag_font_size}px;
                    line-height: 1.55;
                    border-bottom: 1px solid transparent;
                }}
                .tool-diff-inline .diff-ctx:hover {{
                    background: {"rgba(0,0,0,0.04)" if _is_light_diff else "rgba(255,255,255,0.035)"};
                }}
                .tool-diff-inline .diff-add:hover {{
                    background-color: {"rgba(63, 185, 80, 0.15)" if _is_light_diff else "rgba(63, 185, 80, 0.18)"};
                }}
                .tool-diff-inline .diff-del:hover {{
                    background-color: {"rgba(248, 81, 73, 0.15)" if _is_light_diff else "rgba(248, 81, 73, 0.18)"};
                }}
                .tool-diff-inline .line-num {{
                    flex: none;
                    min-width: 38px;
                    padding: 0 8px;
                    text-align: right;
                    color: {"var(--text-muted)" if _is_light_diff else "#6e7681"};
                    user-select: none;
                    font-size: {tag_font_size - 1}px;
                    box-sizing: border-box;
                    background: {"rgba(0,0,0,0.03)" if _is_light_diff else "rgba(13,17,23,0.18)"};
                    border-right: 1px solid {"var(--border)" if _is_light_diff else "rgba(139,148,158,0.16)"};
                }}
                .tool-diff-inline .line-sign {{
                    flex: none;
                    width: 20px;
                    text-align: center;
                    color: #6e7681;
                    user-select: none;
                    font-weight: 700;
                }}
                .tool-diff-inline .line-code {{
                    flex: 1;
                    padding: 0 10px;
                    white-space: pre-wrap;
                    min-width: 0;
                }}
                .tool-diff-inline .diff-add {{
                    background-color: rgba(63, 185, 80, 0.095);
                    box-shadow: inset 3px 0 0 rgba(63, 185, 80, 0.65);
                }}
                .tool-diff-inline .diff-add .line-sign {{
                    color: #56d364;
                }}
                .tool-diff-inline .diff-add .line-code {{
                    color: {"#1a7f37" if _is_light_diff else "#aff5b4"};
                }}
                .tool-diff-inline .diff-del {{
                    background-color: rgba(248, 81, 73, 0.095);
                    box-shadow: inset 3px 0 0 rgba(248, 81, 73, 0.62);
                }}
                .tool-diff-inline .diff-del .line-sign {{
                    color: #ff7b72;
                }}
                .tool-diff-inline .diff-del .line-code {{
                    color: {"#cf222e" if _is_light_diff else "#ffdcd7"};
                }}
                .tool-diff-inline .diff-ctx {{
                    color: {"var(--text-secondary)" if _is_light_diff else "#adbac7"};
                }}
                .tool-diff-inline .diff-hunk {{
                    color: {"var(--text)" if _is_light_diff else "#79c0ff"};
                    background: {"rgba(37, 99, 235, 0.06)" if _is_light_diff else "rgba(56, 139, 253, 0.075)"};
                }}
                .tool-diff-inline .diff-hunk .line-code {{
                    color: {"var(--text)" if _is_light_diff else "#79c0ff"};
                }}
                .tool-diff-inline .diff-file-header .line-code {{
                    color: {"var(--text)" if _is_light_diff else "#c9d1d9"};
                    font-weight: 600;
                }}
                .tool-diff-inline .diff-truncated {{
                    color: {"var(--text-muted)" if _is_light_diff else "#6e7681"};
                    background: {"rgba(0,0,0,0.03)" if _is_light_diff else "rgba(139, 148, 158, 0.055)"};
                }}
                .tool-diff-inline .diff-truncated .line-code {{
                    text-align: center;
                }}
                /* 空白上下文行（源文件空行）：折叠为紧凑空隙，避免单列模式下
                   段落差异之间出现 bulky 空行；连续空行只渲染一条。 */
                .tool-diff-inline .diff-line.diff-ctx-blank {{
                    min-height: 0;
                    height: 9px;
                }}
                .tool-diff-inline .diff-line.diff-ctx-blank .line-code {{
                    color: transparent;
                }}
                /* === 差异段：单列默认为"删除→新增"分组，双列(split-view)为配对行左右对照 === */
                .tool-diff-inline .diff-segment {{
                    display: block;
                }}
                /* 单列模式（默认）：所有删除先、所有新增后，连续堆叠 */
                .tool-diff-inline .diff-seg-col {{
                    display: block;
                }}
                .tool-diff-inline .diff-seg-col > .diff-line {{
                    border-bottom: 1px solid transparent;
                }}
                /* 配对行视图（双列模式用）：默认隐藏 */
                .tool-diff-inline .diff-seg-paired {{
                    display: none;
                }}
                /* 双列模式：隐藏单列视图，显示配对视图 */
                .tool-diff-inline.split-view .diff-seg-col {{
                    display: none;
                }}
                .tool-diff-inline.split-view .diff-seg-paired {{
                    display: block;
                }}
                /* 配对行：左右对照（始终 row，因为仅在 split-view 中出现） */
                .tool-diff-inline .diff-seg-row {{
                    display: flex;
                    flex-direction: row;
                    align-items: stretch;
                }}
                .tool-diff-inline .diff-seg-row > .diff-line {{
                    flex: 1 1 50%;
                    width: auto;
                    border-bottom: 1px solid transparent;
                }}
                /* 空栏占位（纯删/纯增段在双列模式中的空白占位列） */
                .tool-diff-inline .diff-seg-empty {{
                    display: flex;
                    background: transparent !important;
                    box-shadow: none !important;
                }}
                .tool-diff-inline .diff-seg-empty .line-code {{
                    color: transparent;
                }}

                /* 元信息行（文件头/hunk头/截断）：行号列与符号列隐形，避免空列割裂视觉 */
                .tool-diff-inline .diff-meta .line-num {{
                    background: transparent;
                    border-right-color: transparent;
                    min-width: 0;
                    padding: 0;
                }}
                .tool-diff-inline .diff-meta .line-sign {{
                    width: 0;
                }}
                .tool-diff-inline .word-add {{
                    background: rgba(63, 185, 80, 0.28);
                    border-radius: 3px;
                    box-shadow: inset 0 -1px 0 rgba(63, 185, 80, 0.65);
                }}
                .tool-diff-inline .word-del {{
                    background: rgba(248, 81, 73, 0.28);
                    border-radius: 3px;
                    box-shadow: inset 0 -1px 0 rgba(248, 81, 73, 0.65);
                }}
                .tool-params-section,
                .tool-result-section {{
                    padding: 0;
                }}
                .tool-section-label {{
                    color: var(--text-muted);
                    font-size: {small_font_size}px;
                    font-weight: 500;
                    padding: 8px 12px 4px;
                    text-transform: uppercase;
                    letter-spacing: 0.5px;
                }}
                .args-table {{
                    display: flex;
                    flex-direction: column;
                    gap: 0;
                    margin: 0;
                }}
                .args-row {{
                    display: flex;
                    align-items: flex-start;
                    padding: 6px 12px;
                    border-bottom: 1px solid var(--border);
                    font-size: {tag_font_size}px;
                }}
                .args-row:last-child {{
                    border-bottom: none;
                }}
                .args-row.empty {{
                    color: var(--text-muted);
                    font-style: italic;
                    padding: 8px 12px;
                }}
                .args-key {{
                    flex: 0 0 auto;
                    min-width: 80px;
                    max-width: 120px;
                    color: var(--text-secondary);
                    font-weight: 500;
                    margin-right: 12px;
                    word-break: break-word;
                }}
                .args-row.result-success {{
                    border-top: 1px solid {"rgba(34, 197, 94, 0.3)" if _is_light_diff else "rgba(95, 209, 140, 0.3)"};
                    background: {"rgba(34, 197, 94, 0.05)" if _is_light_diff else "rgba(95, 209, 140, 0.05)"};
                }}
                .args-row.result-fail {{
                    /* 早先写死 rgba(244,67,54,.3)，浅色主题下过艳；与 result-success 一样按主题取色 */
                    border-top: 1px solid {"rgba(220, 38, 38, 0.3)" if _is_light_diff else "rgba(248, 81, 73, 0.3)"};
                    background: {"rgba(220, 38, 38, 0.05)" if _is_light_diff else "rgba(248, 81, 73, 0.05)"};
                }}
                .args-value {{
                    flex: 1 1 auto;
                    color: var(--text);
                    word-break: break-all;
                    font-family: {mono_font};
                    font-size: {small_font_size}px;
                }}
                .result-content {{
                    /* 右 6px = 12px - 6px 滚动轨道：同 .think-content，右侧视觉边距与左对称 */
                    padding: 6px 6px 10px 12px;
                    color: var(--text);
                    font-size: {tag_font_size}px;
                    line-height: 1.5;
                    word-break: break-word;
                    font-family: {mono_font};
                    max-height: 400px;
                    overflow-y: scroll;
                    /* 🐛 修复（偶发横向滚动条）：显式 hidden 避免一轴非 visible
                       导致另一轴 visible 被自动计算为 auto 而撑出横向滚动条 */
                    overflow-x: hidden;
                }}
                .result-empty {{
                    padding: 6px 12px 10px;
                    color: var(--text-muted);
                    font-style: italic;
                    font-size: {tag_font_size}px;
                }}
                .tool-content {{
                    padding: 10px 12px;
                    border-top: 1px solid var(--border);
                    background: transparent;
                }}
                .tool-content pre {{
                    margin: 0;
                    color: #d8b68d;
                    font-size: {tag_font_size}px;
                    font-family: {mono_font};
                    white-space: pre-wrap;
                    word-break: break-word;
                }}

                .hook-block {{
                    margin: 8px 0;
                    background: transparent;
                    border: 1px solid rgba(0, 188, 212, 0.2);
                    border-left: 3px solid #00BCD4;
                    border-radius: 10px;
                    box-shadow: none;
                    transition: border-color 220ms ease;
                }}
                .hook-block[data-expanded="true"] {{
                    border-color: rgba(0, 188, 212, 0.5);
                }}
                .hook-block__summary {{
                    padding: 8px 12px;
                    color: #00BCD4;
                    font-weight: 600;
                    font-size: {code_font_size}px;
                    font-family: '{font_family}', sans-serif;
                    white-space: normal;
                }}
                .hook-content {{
                    padding: 10px 12px;
                    border-top: 1px solid rgba(0, 188, 212, 0.2);
                    background: transparent;
                    font-family: {mono_font};
                    font-size: {tag_font_size}px;
                    color: #e0e0e0;
                    white-space: pre-wrap;
                    word-break: break-word;
                    line-height: 1.5;
                }}

                blockquote {{
                    border-left: 3px solid var(--accent-warm);
                    background: rgba(255,182,92,0.08);
                    margin: 10px 0;
                    padding: 8px 12px;
                    border-radius: 0 10px 10px 0;
                    color: var(--text-secondary) !important;
                }}

                /* ===== ECharts 图表容器 ===== */
                .echarts-container {{
                    position: relative;
                    width: 100%;
                    min-height: 300px;
                    height: auto;
                    margin: 12px 0;
                    border-radius: 10px;
                    background: {"rgba(255, 255, 255, 0.75)" if _is_light_diff else "rgba(22, 27, 34, 0.6)"};
                    border: 1px solid var(--code-border, rgba(58, 63, 71, 0.6));
                }}
                /* ===== 图表 hover 浮动工具栏（放大 / 导出）；按钮底色与 icon 目录由 _CHART_IS_DARK 同源驱动 ===== */
                .chart-toolbar {{
                    position: absolute;
                    top: 8px;
                    right: 24px;
                    display: flex;
                    gap: 6px;
                    opacity: 0;
                    transition: opacity 150ms ease;
                    z-index: 10;
                }}
                .echarts-container:hover .chart-toolbar,
                .mermaid-block:hover .chart-toolbar,
                .widget-toolbar-host:hover .chart-toolbar {{
                    opacity: 1;
                }}
                .chart-toolbar button {{
                    position: relative;
                    width: 28px;
                    height: 28px;
                    border-radius: 6px;
                    border: 1px solid var(--code-border, rgba(58, 63, 71, 0.6));
                    background: {"rgba(255, 255, 255, 0.92)" if _is_light_diff else "rgba(22, 27, 34, 0.85)"};
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 0;
                }}
                .chart-toolbar button:hover {{
                    background: {"rgba(228, 233, 240, 1)" if _is_light_diff else "rgba(40, 46, 56, 0.95)"};
                }}
                /* 自绘 tooltip（代替 HTML title，避免 Chromium 原生 tooltip 黑块）；向下弹避免被容器 overflow:hidden 裁剪 */
                .chart-toolbar button::after {{
                    content: attr(data-tooltip);
                    position: absolute;
                    top: calc(100% + 6px);
                    right: 0;
                    white-space: nowrap;
                    background: var(--panel, rgba(30,30,32,250));
                    color: var(--text, #ffffff);
                    font-size: 11px;
                    padding: 4px 8px;
                    border-radius: 6px;
                    border: 1px solid var(--border, rgba(128,128,128,0.15));
                    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
                    pointer-events: none;
                    z-index: 100;
                    line-height: 1.4;
                    opacity: 0;
                    transition: opacity 140ms ease;
                }}
                .chart-toolbar button:hover::after {{
                    opacity: 1;
                    z-index: 100;
                }}
                .chart-toolbar button img {{
                    width: 16px;
                    height: 16px;
                    pointer-events: none;
                }}

                /* ===== Mermaid 图表容器 ===== */
                .mermaid-block {{
                    position: relative;
                    width: 100%;
                    margin: 12px 0;
                    padding: 12px 10px;
                    border-radius: 10px;
                    background: transparent;
                    border: 1px solid var(--code-border, rgba(58, 63, 71, 0.6));
                    overflow-x: auto;
                    text-align: center;
                }}
                /* mermaid 输出的 svg 自带固定 width，需放开以便窄卡片内自适应 */
                .mermaid-block svg {{
                    max-width: 100%;
                    height: auto;
                }}
                .mermaid-block.mermaid-pending {{
                    min-height: 42px;
                    color: var(--text-muted, #8b949e);
                    font-size: 12px;
                }}
                /* 渲染失败：退回源码，保证内容不丢 */
                .mermaid-error {{
                    text-align: left;
                    margin: 0;
                    padding: 10px 12px;
                    white-space: pre-wrap;
                    word-break: break-word;
                    font-size: 12px;
                    color: var(--text-secondary, #c9d1d9);
                }}

                /* ===== 流式图表骨架 ===== */
                /* 半截 ```echarts / ```mermaid fence 期间占位。原实现把残缺 JSON 也包成
                   .echarts-container，JS 侧 JSON.parse 失败后只 console.error，容器留
                   400px 空洞 → 用户看到"图表区空一块"或上一版内容闪现。骨架用柱状图
                   拟态 + 扫光明确传达"图表正在生成"，且高度与真图一致（min-height 300px），
                   fence 闭合切换成真图时零布局跳动。 */
                .chart-skeleton {{
                    position: relative;
                    width: 100%;
                    min-height: 300px;
                    margin: 12px 0;
                    border-radius: 10px;
                    background: {"rgba(255, 255, 255, 0.75)" if _is_light_diff else "rgba(22, 27, 34, 0.6)"};
                    border: 1px solid var(--code-border, rgba(58, 63, 71, 0.6));
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    gap: 16px;
                    overflow: hidden;
                }}
                .chart-skeleton__bars {{
                    display: flex;
                    align-items: flex-end;
                    gap: 9px;
                    height: 104px;
                }}
                .chart-skeleton__bars i {{
                    display: block;
                    width: 15px;
                    border-radius: 3px;
                    background: var(--accent, #4C8DFF);
                    transform-origin: bottom;
                    animation: chartSkPulse 1.15s ease-in-out infinite;
                }}
                .chart-skeleton__bars i:nth-child(1) {{ height: 38%; animation-delay: 0ms; }}
                .chart-skeleton__bars i:nth-child(2) {{ height: 64%; animation-delay: 110ms; }}
                .chart-skeleton__bars i:nth-child(3) {{ height: 96%; animation-delay: 220ms; }}
                .chart-skeleton__bars i:nth-child(4) {{ height: 54%; animation-delay: 330ms; }}
                .chart-skeleton__bars i:nth-child(5) {{ height: 78%; animation-delay: 440ms; }}
                @keyframes chartSkPulse {{
                    0%, 100% {{ transform: scaleY(0.42); opacity: 0.32; }}
                    50%      {{ transform: scaleY(1);    opacity: 0.72; }}
                }}
                .chart-skeleton__label {{
                    font-size: 12px;
                    color: var(--text-muted, #8b949e);
                    letter-spacing: 0.3px;
                }}
                /* 顶部扫光：强化"持续生成中"的连续感，避免静态骨架看着像卡死 */
                .chart-skeleton::after {{
                    content: "";
                    position: absolute;
                    inset: 0;
                    background: linear-gradient(100deg, transparent 18%, rgba(128,128,128,0.10) 50%, transparent 82%);
                    animation: chartSkSweep 1.7s linear infinite;
                }}
                @keyframes chartSkSweep {{
                    from {{ transform: translateX(-100%); }}
                    to   {{ transform: translateX(100%); }}
                }}
                /* ── 减少动态效果：停掉所有**无限循环**的装饰性 CSS 动画 ──
                   这些动画（骨架脉冲/扫光、思考提示流光、工具预览流光）会一直
                   重绘直到元素被移除；流式期间与逐字渲染叠加会明显加剧掉帧与
                   图表闪烁。关掉后仍有静态骨架与文字，状态反馈不受影响。 */
                @media (prefers-reduced-motion: reduce) {{
                    .chart-skeleton__bars i,
                    .chart-skeleton::after,
                    .think-streaming-tip,
                    .tool-streaming-block[data-streaming="true"] .tool-streaming-preview {{ animation: none !important; }}
                }}
                /* option 解析失败兜底（fence 已闭合但 JSON 畸形）：收起空洞，给出提示。
                   成功渲染时 JS 移除该 class，容器恢复 300px。 */
                .echarts-container.echarts-failed {{
                    min-height: 120px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .echarts-container.echarts-failed::before {{
                    content: "图表数据暂不完整，等待生成…";
                    font-size: 12px;
                    color: var(--text-muted, #8b949e);
                }}
                '''

                /* 内容区图片可点击打开 */
                #content-placeholder img {{
                    cursor: pointer;
                }}
                /* 工具/思考块内的图标小图不应用圆角裁剪和指针样式 */
                #content-placeholder .tool-block img,
                #content-placeholder .think-block img,
                #content-placeholder .think-compact img,
                #content-placeholder .think-streaming img {{
                    border-radius: 0;
                    display: inline;
                    margin: 0;
                    max-width: none;
                    cursor: default;
                }}

                /* 工具/思考区域 - 高度自适应 + 可折叠（正文上方，背景+边框区分） */
                /* ── 性能优化：contain: layout paint 让浏览器把此容器视为独立渲染作用域，
                   父布局变化不会让其子树重排 ── */
                #tool-section {{
                    margin: 0 0 8px 0;
                    contain: layout paint;
                }}
                #tool-separator {{
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    font-size: 13px;
                    color: var(--text-muted);
                    user-select: none;
                    padding: 2px 2px 6px 2px;
                    cursor: pointer;
                    border-radius: 4px;
                    transition: background-color 120ms ease;
                }}
                #tool-separator:hover {{
                    background: var(--panel-soft);
                }}
                /* 自绘 tooltip：hover 时在分隔条下方显示说明 */
                #tool-separator {{
                    position: relative;
                }}
                .tool-separator-tooltip {{
                    position: absolute;
                    left: 50%;
                    top: 100%;
                    transform: translateX(-50%);
                    margin-top: 6px;
                    white-space: nowrap;
                    background: var(--panel, rgba(30,30,32,250));
                    color: var(--text, #ffffff);
                    font-size: 11px;
                    padding: 4px 8px;
                    border-radius: 6px;
                    border: 1px solid var(--border, rgba(128,128,128,0.15));
                    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
                    pointer-events: none;
                    z-index: 100;
                    line-height: 1.4;
                    opacity: 0;
                    transition: opacity 140ms ease;
                }}
                #tool-separator:hover .tool-separator-tooltip {{
                    opacity: 1;
                }}
                /* 子智能体日志按钮自绘 tooltip（代替 HTML title，避免 Chromium 原生 tooltip 在深色模式下显示为黑块） */
                .tool-subagent-log-btn::after {{
                    content: attr(data-tooltip);
                    position: absolute;
                    bottom: calc(100% + 6px);
                    left: 50%;
                    transform: translateX(-50%);
                    white-space: nowrap;
                    background: var(--panel, rgba(30,30,32,250));
                    color: var(--text, #ffffff);
                    font-size: 11px;
                    padding: 4px 8px;
                    border-radius: 6px;
                    border: 1px solid var(--border, rgba(128,128,128,0.15));
                    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
                    pointer-events: none;
                    z-index: 100;
                    line-height: 1.4;
                    opacity: 0;
                    transition: opacity 140ms ease;
                }}
                .tool-subagent-log-btn:hover::after {{
                    opacity: 1;
                }}
                /* 折叠时让 chevron 旋转 */
                #tool-section[data-collapsed="true"] #tool-separator .chevron {{
                    transform: rotate(-90deg);
                }}
                #tool-separator::before,
                #tool-separator::after {{
                    content: '';
                    flex: 1;
                    height: 1px;
                    background: var(--border);
                    opacity: 0.6;
                }}
                #tool-separator .chevron {{
                    display: inline-block;
                    transition: transform 160ms ease;
                    font-size: 9px;
                    opacity: 0.7;
                }}

                @keyframes _streamingPulse {{
                    0%, 100% {{ opacity: 0.3; transform: scale(0.85); }}
                    50% {{ opacity: 1; transform: scale(1.1); }}
                }}
                #tool-content {{
                    /* 固定最大高度，超出时显示滚动条。
                       不设动态大小（不依赖 body 高度比例）。 */
                    max-height: 600px;
                    /* 轨道常驻：出现/消失切换不再使内容宽度 ±6px 波动；
                       右 padding 扣减 6px，右侧视觉边距与左基本对称 */
                    overflow-y: scroll;
                    /* 🐛 修复（偶发横向滚动条）：CSS 规范规定一轴非 visible 时另一轴 visible
                       会被自动计算为 auto，未显式声明会让内部 .tool-diff-inline__body /
                       .code-container / .table-scroll-wrapper 等 overflow-x:auto 的子容器
                       在内容超宽时撑出整个"工具与思考"区的横向滚动条 */
                    overflow-x: hidden;
                    overflow-anchor: none;  /* 禁用 scroll anchoring，防止浏览器在 reorganizeContent 后调整 scrollTop 覆盖 JS 设置的滚底位置 */
                    background: transparent;
                    border: none;
                    border-radius: 6px;
                    padding: 2px 0 2px 4px;
                    /* 折叠过渡：高度 0 时禁用滚动，避免用户看到残留滚动条 */
                    transition: max-height 200ms ease, opacity 160ms ease;
                }}
                #tool-section[data-collapsed="true"] #tool-content {{
                    max-height: 0;
                    opacity: 0;
                    padding-top: 0;
                    padding-bottom: 0;
                    overflow: hidden;
                }}

                /* 新工具块入场动效 — 仅对"真正新"的块生效
                   （无 data-tool-call-id 且非 restore 的块）。
                   流式/恢复的块已有 data-tool-call-id 或 data-restored，跳过动画避免闪烁。 */
                @keyframes _toolBlockEnter {{
                    from {{ opacity: 0; transform: translateY(4px); }}
                    to {{ opacity: 1; transform: translateY(0); }}
                }}
                #tool-content > .tool-block:not([data-tool-call-id]):not([data-restored]),
                #tool-content > .think-block:not([data-restored]),
                #tool-content > .think-streaming:not([data-restored]) {{
                    animation: _toolBlockEnter 160ms ease-out;
                }}
                #tool-content > .tool-block:first-child,
                #tool-content > .think-block:first-child,
                #tool-content > .think-streaming:first-child {{
                    margin-top: 0;
                }}
                #tool-content > .tool-block:last-child,
                #tool-content > .think-block:last-child,
                #tool-content > .think-streaming:last-child {{
                    margin-bottom: 0;
                }}
                {_STREAMING_DOCK_CSS}
            </style>
        </head>
        <body>
            <div id="tool-section" style="display: none;" data-collapsed="false">
              <div id="tool-separator" role="button" tabindex="0" aria-expanded="true">
                <span class="chevron">▾</span>
                <span>⚙ 工具与思考</span>
                <span class="tool-separator-tooltip">点击折叠/展开工具与思考区</span>
              </div>
              <div id="tool-content"></div>
            </div>
            <div id="content-placeholder"></div>
            <script>
                const collapsibleState = new Map();
                // 简洁模式标志：由 Python 在 _load_skeleton 后通过 JS 同步更新
                window._toolCompactMode = true;

                function syncExpandedAttrs(block, expanded) {{
                    block.dataset.expanded = expanded ? 'true' : 'false';
                    const summary = block.querySelector('.cm-collapsible__summary');
                    if (summary) summary.setAttribute('aria-expanded', expanded ? 'true' : 'false');
                    const key = block.dataset.blockKey;
                    if (key) collapsibleState.set(key, expanded);
                }}

                function animateCollapsible(block, expand) {{
                    const body = block.querySelector('.cm-collapsible__body');
                    if (!body) return;

                    const ANIM_DURATION = 220;
                    const startTime = performance.now();
                    const startHeight = body.getBoundingClientRect().height;
                    const startOpacity = expand ? 0 : 1;
                    const endHeight = expand ? body.scrollHeight : 0;
                    const endOpacity = expand ? 1 : 0;

                    // 立即更新展开状态
                    syncExpandedAttrs(block, expand);

                    // 阻止 CSS transition 干扰
                    const isCollapsing = !expand;
                    body.style.transition = 'none';
                    body.style.height = startHeight + 'px';
                    body.style.opacity = startOpacity;
                    // 立即设置 overflow 防止内容泄漏
                    body.style.overflow = 'hidden';

                    // 强制重绘，确保第一帧从正确的 startHeight 开始
                    void body.offsetHeight;

                    // 取消之前的动画
                    if (window._collapsibleAnimId) {{
                        cancelAnimationFrame(window._collapsibleAnimId);
                    }}

                    function tick(now) {{
                        const elapsed = now - startTime;
                        const progress = Math.min(elapsed / ANIM_DURATION, 1);
                        // 使用 easeOutQuad 缓动
                        const eased = 1 - (1 - progress) * (1 - progress);

                        const currentHeight = isCollapsing 
                            ? startHeight * (1 - eased)  // 从 startHeight 减少到 0
                            : startHeight + (endHeight - startHeight) * eased;
                        const currentOpacity = startOpacity + (endOpacity - startOpacity) * eased;

                        body.style.height = currentHeight + 'px';
                        body.style.opacity = currentOpacity;

                        if (progress < 1) {{
                            window._collapsibleAnimId = requestAnimationFrame(tick);
                        }} else {{
                            // 动画结束：设置最终状态
                            body.style.height = expand ? 'auto' : '0px';
                            body.style.opacity = endOpacity;
                            // 折叠后保持 overflow hidden，防止内容溢出导致文档高度波动
                            if (!expand) body.style.overflow = 'hidden';
                            else body.style.overflow = '';
                            // 强制重排确保布局已稳定，然后立即报告最终高度
                            // 先 void body.offsetHeight 强制同步布局，再 reportHeight
                            void body.offsetHeight;
                            reportHeight();
                            // ⚠️ 最后释放高度报告抑制，避免 ResizeObserver 在布局计算期间
                            // 被 overflow 等属性变化触发二次报告（50ms 后 viewer 再跳一次）
                            _collapsibleHeightReporting = false;
                        }}
                    }}

                    window._collapsibleAnimId = requestAnimationFrame(tick);
                }}

                // 折叠动画期间暂停高度报告，避免卡片抖动
                let _collapsibleHeightReporting = false;
                function startCollapsibleAnimation() {{
                    _collapsibleHeightReporting = true;
                }}

                // ===== tool-section 折叠/展开过渡的高度报告抑制 =====
                // #tool-content 有 max-height 200ms CSS 过渡，过渡期间 ResizeObserver
                // 会上报中间态高度 → viewer setFixedHeight 连跳多次（流式抖动主因之二）。
                // 统一模式：切换属性前先抑制报告 → transitionend（260ms 兜底）终值单报。
                // _finishToolSectionTransition 带 guard：先到者生效，后到者 no-op
                // （同时修复旧 _toggleToolSection transitionend+setTimeout 双报告）。
                var _tsTransitionDone = false;
                var _tsTransitionToken = 0;
                function _finishToolSectionTransition() {{
                    if (_tsTransitionDone) return;
                    _tsTransitionDone = true;
                    reportHeight();          // 终值直报（不经 debounced，且此时抑制已可释放）
                    _collapsibleHeightReporting = false;
                }}
                function _beginToolSectionTransition() {{
                    _collapsibleHeightReporting = true;   // 抑制 ResizeObserver + reportHeightDebounced
                    _tsTransitionDone = false;
                    var token = ++_tsTransitionToken;     // 轮次令牌：连续切换时旧 timer 失效
                    var _tcEl = document.getElementById('tool-content');
                    var _onTsEnd = function(ev) {{
                        // 只认 max-height 过渡结束（同元素 opacity 过渡会额外触发一次）
                        if (ev && ev.propertyName && ev.propertyName !== 'max-height') return;
                        if (token !== _tsTransitionToken) return;
                        if (_tcEl) _tcEl.removeEventListener('transitionend', _onTsEnd);
                        _finishToolSectionTransition();
                    }};
                    if (_tcEl) _tcEl.addEventListener('transitionend', _onTsEnd);
                    // 兜底：display:none 等场景 transitionend 不触发
                    setTimeout(function() {{
                        if (token !== _tsTransitionToken) return;
                        if (_tcEl) _tcEl.removeEventListener('transitionend', _onTsEnd);
                        _finishToolSectionTransition();
                    }}, 260);
                }}

                // ===== 差量渲染的作用域查询 =====
                // 差量路径（updateContentAppend / updateTailHtml）原来每轮都对
                // 整个 #content-placeholder（甚至 document）做 querySelectorAll，
                // 随着正文增长是 O(n)；流式 N 轮累积成 O(n²) —— 长回复越到后面
                // 越卡、"图表更新慢"的机械性根源。差量语义下新内容只可能出现在
                // 新增的那几个顶层节点里，因此把扫描范围限定到新增区间即可：
                // 每轮代价从 O(全文) 降为 O(新增段)，与已渲染长度无关。
                // roots 为 null/undefined 时退化为全文档查询（全量渲染路径）。
                window._scopeQuery = function (roots, sel) {{
                    var out = [];
                    if (!roots) return document.querySelectorAll(sel);
                    if (!roots.length) roots = [roots];
                    for (var i = 0; i < roots.length; i++) {{
                        var r = roots[i];
                        if (!r) continue;
                        if (r.matches && r.matches(sel)) out.push(r);
                        if (r.querySelectorAll) {{
                            var sub = r.querySelectorAll(sel);
                            for (var j = 0; j < sub.length; j++) out.push(sub[j]);
                        }}
                    }}
                    return out;
                }};
                // 取容器中 [fromIdx, end) 区间的顶层节点——差量追加新增的部分。
                // reorganizeContent 可能搬走部分节点导致长度收缩，故取 min 保护。
                window._nodesSince = function (container, fromIdx) {{
                    var out = [];
                    var start = Math.min(fromIdx, container.children.length);
                    for (var i = start; i < container.children.length; i++) out.push(container.children[i]);
                    return out;
                }};
                // 表格包裹（差量路径只处理新增区间）
                window._wrapTablesIn = function (roots) {{
                    var tables = window._scopeQuery(roots, 'table:not(.code-table):not(.layout-table)');
                    for (var i = 0; i < tables.length; i++) {{
                        var table = tables[i];
                        if (table.parentNode && table.parentNode.classList.contains('table-scroll-wrapper')) continue;
                        var wrapper = document.createElement('div');
                        wrapper.className = 'table-scroll-wrapper';
                        table.parentNode.insertBefore(wrapper, table);
                        wrapper.appendChild(table);
                    }}
                }};

                function restoreCollapsibleStates(roots) {{
                    var blocks = window._scopeQuery(roots, '.cm-collapsible');
                    for (var _bi = 0; _bi < blocks.length; _bi++) {{
                        var block = blocks[_bi];
                        const key = block.dataset.blockKey;
                        const expanded = key && collapsibleState.has(key)
                            ? collapsibleState.get(key)
                            : block.dataset.expanded === 'true';
                        const body = block.querySelector('.cm-collapsible__body');
                        syncExpandedAttrs(block, !!expanded);
                        if (body) {{
                            body.style.transition = 'none';
                            if (expanded) {{
                                body.style.height = 'auto';
                                body.style.opacity = '1';
                            }} else {{
                                body.style.height = '0px';
                                body.style.opacity = '0';
                            }}
                            body.offsetHeight;
                            body.style.transition = '';
                        }}
                    }};
                }}

                // ===== Mermaid 渲染（Chromium 83 兼容：polyfill + mermaid 10.9.1 懒加载） =====
                // Qt 5.15.2 的 WebEngine 是 Chromium 83，缺 structuredClone / Object.hasOwn /
                // replaceAll / Array.prototype.at；mermaid 10 在模块顶层就会用到，缺一个即
                // 整体 undefined。故必须先加载 polyfill，再加载 mermaid。见 docs/mermaid-chromium83.md。
                var _MMD_POLYFILL = '{_mmd_polyfill_url}';
                var _MMD_LIB = '{_mmd_lib_url}';

                // ===== ECharts 懒加载（与 mermaid / KaTeX 同策略）=====
                // 原先 echarts.min.js(1MB) + wordcloud 常驻骨架 → 每张卡片都背上。
                // 改为首次遇到 .echarts-container 时才加载，见 _echartsEnsure。
                var _ECH_LIB = '{_echarts_lib_url}';
                var _ECH_WORDCLOUD = '{_echarts_wordcloud_url}';

                // ===== 插件 fence 渲染器：assets 与权限映射表 =====
                // 只是"有哪些可用"的清单，真正的加载按卡片实际出现的 fence lang
                // 按需触发（见 _runFenceAssets），未用到的插件零开销。
                window.__fenceAssets = {_fence_assets_js};
                window.__fenceBridgePerms = {_fence_perms_js};

                // ===== KaTeX 公式渲染（懒加载，与 mermaid 同策略；css/js 同源见 _get_katex_urls）=====
                var _KATEX_CSS = '{_katex_css_url}';
                var _KATEX_LIB = '{_katex_js_url}';

                function _mmdLoadScript(src, onOk) {{
                    if (!src) {{ onOk(); return; }}
                    var s = document.createElement('script');
                    s.src = src;
                    s.onload = onOk;
                    s.onerror = function () {{ console.error('[mermaid] load failed: ' + src); }};
                    document.head.appendChild(s);
                }}

                // mermaid 主题变量随主题取色，抽成可更新变量：原先 initialize 只在
                // 首次懒加载时跑一次且颜色是建卡时插值的常量，主题切换后新渲染的图
                // 沿用旧配色（浅色主题下白叠白）。refresh_theme 会注入新值并调
                // window._mmdApplyTheme() 重设。
                window._MMD_THEME_VARS = {{
                    primaryTextColor: '{mmd_text_color}',
                    lineColor: '{mmd_line_color}',
                    mainBkg: '{mmd_node_bg}',
                    nodeBorder: '{mmd_border}',
                    background: 'transparent',
                    fontSize: '{body_font_size}px'
                }};
                window._mmdApplyTheme = function () {{
                    try {{
                        if (window.mermaid && window.mermaid.initialize) {{
                            window.mermaid.initialize({{
                                startOnLoad: false,
                                // 内容来自 LLM，不可信：strict 会 sanitize 标签、禁用交互
                                securityLevel: 'strict',
                                theme: 'base',
                                themeVariables: window._MMD_THEME_VARS
                            }});
                        }}
                    }} catch (e) {{
                        console.error('[mermaid] initialize failed:', e);
                    }}
                }};

                function _mmdEnsure(cb) {{
                    if (window.mermaid && window.mermaid.render) {{ cb(); return; }}
                    if (!window._mmdQueue) window._mmdQueue = [];
                    window._mmdQueue.push(cb);
                    if (window._mmdLoading) return;          // 已在加载中，排队即可
                    window._mmdLoading = true;
                    _mmdLoadScript(_MMD_POLYFILL, function () {{
                        _mmdLoadScript(_MMD_LIB, function () {{
                            window._mmdApplyTheme();
                            window._mmdLoading = false;
                            var q = window._mmdQueue || [];
                            window._mmdQueue = [];
                            for (var qi = 0; qi < q.length; qi++) {{ q[qi](); }}
                        }});
                    }});
                }}

                // ===== mermaid 渲染并发限制：同时最多 2 个 render =====
                // mermaid render 含 layout 计算，多图（10+）同时全并发会形成长任务阻塞
                // 渲染主线程（"图表一多整卡卡死"）。信号量泵：_mmdActive < 2 时逐个取
                // job 执行，done() 回调释放名额并驱动下一轮。
                var _mmdWait = [];
                var _mmdActive = 0;
                function _pumpMmd() {{
                    while (_mmdActive < 2 && _mmdWait.length) {{
                        (function (job) {{
                            _mmdActive++;
                            job(function () {{ _mmdActive--; _pumpMmd(); }});
                        }})(_mmdWait.shift());
                    }}
                }}

                // roots：差量路径传入新增节点区间，只渲染新增的图（见 _scopeQuery 说明）。
                // 省略时退化为全文档扫描，供全量 updateContent 路径使用。
                function renderMermaidBlocks(roots) {{
                    var blocks = window._scopeQuery(roots, '.mermaid-block[data-mermaid-src]');
                    if (!blocks.length) return;
                    _mmdEnsure(function () {{
                        if (!window.mermaid || !window.mermaid.render) return;
                        for (var i = 0; i < blocks.length; i++) {{
                            (function (el) {{
                                if (el._mmdDone || el._mmdQueued) return;
                                el._mmdQueued = true;           // 入队防重（job 执行前的窗口期）
                                el._mmdDone = true;             // 流式追加不重复渲染
                                _mmdWait.push(function (done) {{
                                var decoded;
                                try {{
                                    // atob 按 ISO-8859-1 解码，直接用于 UTF-8 中文会 mojibake
                                    var bytes = Uint8Array.from(atob(el.getAttribute('data-mermaid-src')),
                                        function (c) {{ return c.charCodeAt(0); }});
                                    decoded = new TextDecoder('utf-8').decode(bytes);
                                }} catch (e) {{
                                    decoded = '';
                                }}
                                if (!decoded) {{ done(); return; }}
                                var rid = (el.id || 'mmd') + '-svg';
                                window.mermaid.render(rid, decoded).then(function (r) {{
                                    var svg = r && r.svg ? r.svg : String(r);
                                    if (svg.indexOf('<svg') < 0) throw new Error('no svg in result');
                                    el.classList.remove('mermaid-pending');
                                    el.innerHTML = svg;
                                    el.setAttribute('data-mermaid-src', '');   // 渲染完释放 b64
                                    if (window._attachChartToolbar) window._attachChartToolbar(el, 'mermaid');
                                    if (typeof _autoScrollAfterAsyncRender === 'function') _autoScrollAfterAsyncRender();
                                    if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                                    done();
                                }})['catch'](function (e) {{
                                    // 失败不吞内容：退回原始源码，用户仍可复制
                                    el.classList.remove('mermaid-pending');
                                    el.innerHTML = '<pre class="mermaid-error"></pre>';
                                    el.firstChild.textContent = decoded;
                                    // mermaid render 失败时会向 body 追加 error bomb SVG
                                    // （含「Syntax error in text」文案）。它不在卡片容器内，
                                    // innerHTML 全量重建清不掉，会逐轮流式累积，这里顺手移除。
                                    try {{
                                        var bombs = document.querySelectorAll('svg');
                                        for (var bi = 0; bi < bombs.length; bi++) {{
                                            if ((bombs[bi].textContent || '').indexOf('Syntax error in text') >= 0 &&
                                                !bombs[bi].closest('.mermaid-block')) {{
                                                bombs[bi].parentNode.removeChild(bombs[bi]);
                                            }}
                                        }}
                                    }} catch (ignored) {{ }}
                                    if (typeof _autoScrollAfterAsyncRender === 'function') _autoScrollAfterAsyncRender();
                                    if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                                    done();
                                }});
                                }});
                            }})(blocks[i]);
                        }}
                        _pumpMmd();
                    }});
                }}

                // ===== 图表节点暂存区（vault）：全量渲染时保全已渲染图表 =====
                // updateContent 用 innerHTML 整体重建正文，已渲染的 echarts/mermaid/katex
                // 节点被销毁，防重标记（_echartInited/_mmdDone）随节点丢失 → 所有图表重新
                // init/render（流式期间反复闪烁）+ 旧 echarts 实例不 dispose（孤儿实例泄漏，
                // 多图卡片 GPU 内存滚雪球 → 单卡白屏候选根因）。vault 机制：替换前按内容
                // key 把已渲染节点搬进 detached Map，替换后按 key 原节点回插——实例与渲染
                // 产物完整保留，零重建零闪烁。
                // key：ech 用 data-echarts-json（属性永久保留）；mmd/ktx 渲染成功后属性被
                // 清空，故首次 stash 时把 key 缓存在 el._chartKey 上，后续 stash 直接复用。
                window._chartKeyOf = function (el) {{
                    if (el._chartKey) return el._chartKey;
                    var b64 = el.getAttribute('data-echarts-json') || el.getAttribute('data-mermaid-src') || el.getAttribute('data-katex-src') || '';
                    if (!b64) return null;
                    var pfx = el.classList.contains('echarts-container') ? 'ech:' : (el.classList.contains('mermaid-block') ? 'mmd:' : 'ktx:');
                    el._chartKey = pfx + b64;
                    return el._chartKey;
                }};
                // ⚠️ 必须按节点类型分别判定。原实现
                //   `el._echartInited || el._mmdDone || !el.classList.contains('katex-pending')`
                // 对 echarts / mermaid 节点**恒为 true**：这两个 class 都不带
                // `katex-pending`，第三项 `!false` 直接短路为真。
                // 后果（两个 P0）：
                //   ① _restoreCharts 守卫把 innerHTML 重建后的**新**节点也当成
                //      "已回插"跳过 → vault 回插从未发生 → 每轮全量渲染后所有图表
                //      重新 init/render → 流式期间旧图表持续闪烁（用户可见现象）。
                //   ② _stashCharts 把未渲染节点也搬进 vault 并标 _chartStashed，
                //      从而跳过下方 dispose 兜底；节点随即被 innerHTML 销毁 →
                //      echarts 实例 + ResizeObserver 成孤儿。流式 N 轮 × M 图滚雪球
                //      → renderer 进程内存/GPU 资源耗尽 → 图表一多整卡白屏。
                // 仅 katex 路径（带 katex-pending）能走通，形成对照。
                window._chartReady = function (el) {{
                    var cl = el.classList;
                    if (cl.contains('echarts-container')) return !!el._echartInited;
                    if (cl.contains('mermaid-block')) return !!el._mmdDone;
                    // 流式骨架（chart-skeleton）：无内容可渲染，不入 vault
                    if (cl.contains('chart-streaming')) return false;
                    return !cl.contains('katex-pending');
                }};
                window._disposeChartNode = function (el) {{
                    if (!el) return;
                    try {{
                        if (el._chartRO) {{ el._chartRO.disconnect(); el._chartRO = null; }}
                    }} catch (e) {{ }}
                    try {{
                        if (el._chartInstance && typeof el._chartInstance.dispose === 'function') {{
                            el._chartInstance.dispose();
                        }}
                    }} catch (e) {{ }}
                    el._chartInstance = null;
                    el._echartInited = false;
                }};
                window._stashCharts = function (container) {{
                    if (!window.__chartVault) window.__chartVault = new Map();
                    var nodes = container.querySelectorAll('.echarts-container, .mermaid-block, .katex-block, .katex-inline');
                    for (var i = 0; i < nodes.length; i++) {{
                        var el = nodes[i];
                        var key = window._chartKeyOf(el);
                        if (!key) continue;
                        if (window._chartReady(el)) {{
                            var _prev = window.__chartVault.get(key);
                            if (_prev && _prev !== el) {{
                                window._disposeChartNode(_prev);   // 同内容覆盖前先释放旧实例
                            }}
                            el._chartStashed = true;
                            window.__chartVault.set(key, el);
                        }}
                    }}
                    // 未暂存的已 init echarts 节点将被 innerHTML 销毁：主动 dispose 堵孤儿实例泄漏
                    var _echs = container.querySelectorAll('.echarts-container');
                    for (var j = 0; j < _echs.length; j++) {{
                        var _e = _echs[j];
                        if (_e._echartInited && !_e._chartStashed) {{
                            window._disposeChartNode(_e);
                        }}
                    }}
                }};
                window._restoreCharts = function (container) {{
                    if (!window.__chartVault || !window.__chartVault.size) return;
                    var nodes = container.querySelectorAll('.echarts-container, .mermaid-block, .katex-block, .katex-inline');
                    for (var i = 0; i < nodes.length; i++) {{
                        var el = nodes[i];
                        // 已渲染（含本轮刚回插的 saved）→ 跳过；innerHTML 重建出的新节点
                        // 未渲染 → 走下方回插。原守卫对 echarts/mermaid 恒真，导致回插全失效。
                        if (window._chartReady(el)) continue;
                        var key = window._chartKeyOf(el);
                        if (!key || !window.__chartVault.has(key)) continue;
                        var saved = window.__chartVault.get(key);
                        if (!saved || !window._chartReady(saved)) continue;
                        el.parentNode.replaceChild(saved, el);
                        // 一次性回插：同内容多图（b64 相同）时后续节点走正常 init，
                        // 防同一 saved 节点被 replaceChild 挪位导致前一个位置留空白
                        window.__chartVault.delete(key);
                        // 回插的 echarts 实例适配新容器尺寸（detached 期间 RO 不触发，
                        // 重新入 DOM 后虽会恢复，但首帧尺寸可能仍是旧值，这里显式同步一次）
                        if (saved._chartInstance && typeof saved._chartInstance.resize === 'function') {{
                            try {{ saved._chartInstance.resize(); }} catch (e) {{ }}
                        }}
                    }}
                    // vault 上限控制：会话内图表反复改稿场景防 Map 无限涨。
                    // 裁剪必须连带 dispose——原实现只 delete 条目，被淘汰节点上的
                    // echarts 实例与 ResizeObserver 永久驻留（vault 是本进程主要泄漏点）。
                    if (window.__chartVault.size > 48) {{
                        var _vk = window.__chartVault.keys();
                        while (window.__chartVault.size > 24) {{
                            var _k = _vk.next().value;
                            var _drop = window.__chartVault.get(_k);
                            window.__chartVault.delete(_k);
                            window._disposeChartNode(_drop);
                        }}
                    }}
                }};

                // ===== echarts init 排队：rAF + 每帧时间预算，多图不同帧 init 不卡 JS 主线程 =====
                // 多图表回复（如可视化验证场景 10+ 图）同帧批量 init 会形成长任务阻塞渲染
                // 主线程，用户感知"图表一多整卡卡死"。rAF 队列把 init 摊到多帧。
                window._initOneEcharts = function (el) {{
                    try {{
                        var jsonB64 = el.getAttribute('data-echarts-json');
                        if (!jsonB64 || el._echartInited) return;
                        // atob() 默认按 ISO-8859-1 解码字节串，会破坏 UTF-8 中文。
                        // 用 TextDecoder('utf-8') 还原为正确字符串后再 JSON.parse，避免 mojibake。
                        var _bytes = Uint8Array.from(atob(jsonB64), function(c) {{ return c.charCodeAt(0); }});
                        var option = JSON.parse(new TextDecoder('utf-8').decode(_bytes));
                        // 复用同节点上的旧实例（vault 回插 / 重复扫描）：只 setOption，
                        // 不重复 init。echarts.init 对已初始化容器会抛 "There is a chart
                        // instance already initialized on the dom"，旧实现靠 try/catch 吞掉，
                        // 但那样每次都白跑一遍完整 init 开销。
                        var chart = el._chartInstance;
                        if (!chart || chart.isDisposed()) {{
                            chart = echarts.init(el, _CHART_IS_DARK ? 'dark' : undefined);
                            // RO 必须持引用：节点被 innerHTML 销毁时若不 disconnect，
                            // RO 连同 target 常驻（本卡历史上最大的泄漏源之一，见
                            // _disposeChartNode）。
                            var _ro = new ResizeObserver(function() {{
                                try {{ chart.resize(); }} catch (e) {{ }}
                            }});
                            _ro.observe(el);
                            el._chartRO = _ro;
                        }}
                        chart.setOption(option, true);   // notMerge：避免残留上一轮 series
                        el._echartInited = true;
                        el._chartInstance = chart;
                        el.classList.remove('chart-streaming', 'echarts-failed');
                        if (window._attachChartToolbar && !el._toolbarAttached) {{
                            window._attachChartToolbar(el, 'echarts');
                            el._toolbarAttached = true;
                        }}
                        // 高度回传：echarts 自带的 ResizeObserver 只监听容器自身尺寸，
                        // 不会带动 document.body 高度上报。多图卡片原先只靠
                        // updateContent 末尾 30~50ms 定时上报，而 _pumpEcharts 是 rAF
                        // 分帧的 —— 定时常在队列未排空时就触发，卡片高度偏小、底部
                        // 图表被裁。此处与 mermaid 对齐：单图完成即上报一次，
                        // _pumpEcharts 队列排空时再兜一次。
                        if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                    }} catch(e) {{
                        // JSON 不完整（流式半截）或 option 非法：保留占位骨架，内容补齐后
                        // 下一轮自然重试（b64 变 → key 变 → 新节点重新入队）。
                        // 原实现只 console.error，容器留 400px 空白 → 用户看到"图表区空一块"。
                        el.classList.add('echarts-failed');
                        console.error('ECharts init error:', e);
                        // 与 mermaid 失败路径对齐：降级态也要上报，避免高度停在旧值
                        if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                    }}
                }};
                window.__echQueue = [];
                window.__echPumpBusy = false;
                window._queueEcharts = function (el) {{
                    if (el._echQueued) return;
                    el._echQueued = true;
                    window.__echQueue.push(el);
                    if (!window.__echPumpBusy) window._pumpEcharts();
                }};
                // 每帧固定 1 个 → 每帧 8ms 时间预算：单图延迟不变，但模型连吐数个
                // ```echarts 时可在一帧内 init 完 2~4 个，消掉"图表逐个蹦出"的拖沓感。
                window._ECH_FRAME_BUDGET_MS = 8;
                window._pumpEcharts = function () {{
                    if (window.__echPumpBusy) return;   // 防多个入口并发触发重复 rAF
                    window.__echPumpBusy = true;
                    requestAnimationFrame(function () {{
                        window.__echPumpBusy = false;
                        var _t0 = (window.performance && performance.now) ? performance.now() : Date.now();
                        while (window.__echQueue.length) {{
                            var el = window.__echQueue.shift();
                            el._echQueued = false;
                            if (el.isConnected) window._initOneEcharts(el);  // 节点已被后续全量替换销毁则静默跳过
                            var _now = (window.performance && performance.now) ? performance.now() : Date.now();
                            if (_now - _t0 >= window._ECH_FRAME_BUDGET_MS) break;
                        }}
                        if (window.__echQueue.length) window._pumpEcharts();
                        else {{
                            if (typeof _autoScrollAfterAsyncRender === 'function') _autoScrollAfterAsyncRender();
                            if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                        }}
                    }});
                }};
                // ECharts 懒加载：骨架不再常驻 vendor，首次遇到图表块才加载，
                // 加载完成后回调重跑 _initEchartsIn（此时 window.echarts 已就绪）。
                function _echLoadScript(src, onOk) {{
                    if (!src) {{ onOk(); return; }}
                    var s = document.createElement('script');
                    s.src = src;
                    s.onload = onOk;
                    s.onerror = function () {{ console.error('[echarts] load failed: ' + src); }};
                    document.head.appendChild(s);
                }}
                function _echartsEnsure(cb) {{
                    if (window.echarts) {{ cb(); return; }}
                    if (!window._echLibQueue) window._echLibQueue = [];
                    window._echLibQueue.push(cb);
                    if (window._echLibLoading) return;   // 已在加载中，排队即可
                    window._echLibLoading = true;
                    // wordcloud 是 echarts 插件，必须在 echarts 本体之后加载
                    _echLoadScript(_ECH_LIB, function () {{
                        _echLoadScript(_ECH_WORDCLOUD, function () {{
                            window._echLibLoading = false;
                            var q = window._echLibQueue || [];
                            window._echLibQueue = [];
                            for (var qi = 0; qi < q.length; qi++) {{ q[qi](); }}
                        }});
                    }});
                }}
                window._initEchartsIn = function (container) {{
                    if (!window.echarts) {{
                        window._echartsEnsure(function () {{ window._initEchartsIn(container); }});
                        return;
                    }}
                    container.querySelectorAll('.echarts-container').forEach(function(el) {{
                        var jsonB64 = el.getAttribute('data-echarts-json');
                        if (!jsonB64 || el._echartInited || el._echQueued) return;
                        window._queueEcharts(el);
                    }});
                }};

                function _katexEnsure(cb) {{
                    if (window.katex && window.katex.render) {{ cb(); return; }}
                    if (!window._katexQueue) window._katexQueue = [];
                    window._katexQueue.push(cb);
                    if (window._katexLoading) return;   // 已在加载中，排队即可
                    window._katexLoading = true;
                    if (!document.getElementById('katex-css') && _KATEX_CSS) {{
                        var link = document.createElement('link');
                        link.id = 'katex-css';
                        link.rel = 'stylesheet';
                        link.href = _KATEX_CSS;
                        document.head.appendChild(link);
                    }}
                    var s = document.createElement('script');
                    s.src = _KATEX_LIB;
                    s.onload = function () {{
                        window._katexLoading = false;
                        var q = window._katexQueue || [];
                        window._katexQueue = [];
                        for (var qi = 0; qi < q.length; qi++) {{ q[qi](); }}
                    }};
                    s.onerror = function () {{
                        // 加载失败：清队列，占位保持 pending 原样（流式下一轮可重试）
                        window._katexLoading = false;
                        window._katexQueue = [];
                    }};
                    document.head.appendChild(s);
                }}

                function renderKatexBlocks(roots) {{
                    var nodes = window._scopeQuery(roots, '.katex-pending[data-katex-src]');
                    if (!nodes.length) return;
                    _katexEnsure(function () {{
                        if (!window.katex || !window.katex.render) return;
                        for (var i = 0; i < nodes.length; i++) {{
                            (function (el) {{
                                if (el._katexDone) return;
                                el._katexDone = true;             // 流式重建防重
                                var bytes = Uint8Array.from(atob(el.getAttribute('data-katex-src')),
                                    function (c) {{ return c.charCodeAt(0); }});
                                var decoded = new TextDecoder('utf-8').decode(bytes);
                                var display = el.classList.contains('katex-block');
                                try {{
                                    // throwOnError:false → 非法 LaTeX 输出红色错误样式源码（GitHub 风格）
                                    katex.render(decoded, el, {{ throwOnError: false, displayMode: display }});
                                    el.setAttribute('data-katex-src', '');   // 渲染完释放 b64
                                    el.classList.remove('katex-pending');
                                }} catch (e) {{
                                    // 环境级异常（katex 未定义等）：退纯文本源码
                                    el.textContent = decoded;
                                    el.classList.remove('katex-pending');
                                }}
                                if (typeof _autoScrollAfterAsyncRender === 'function') _autoScrollAfterAsyncRender();
                                if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                            }})(nodes[i]);
                        }}
                    }});
                }}

                function updateContent(newHtml) {{
                    const container = document.getElementById('content-placeholder');
                    if (container.innerHTML !== newHtml) {{
                        // 🐛 DOM 重建事务：必须在**任何** DOM 操作（_twReset / _stashCharts /
                        // _saveCharts）之前捕获锚点，否则捕获到的已是被钳制的位置。
                        _beginDomUpdate();
                        // 打字机：本次将整体替换增量节点，Python 侧 markdown 已含全部
                        // 文本（含尚未揭示部分）。[方案C] 用 _twFlush 替代 _twReset：
                        // 同一次 IPC 内先把未揭示缓冲立即上屏再整体替换，消除 flush 与
                        // 替换之间的帧间隙（替换前后文字量一致，高度不二段跳）。
                        if (typeof window._twFlush === 'function') window._twFlush();
                        // FLIP：替换前记录工具/思考区与正文容器的视口位置，
                        // 重排后用位移动画补间（消除"结束态弹到最顶上"的瞬移感）。
                        var _flipPrev = (typeof window._flipCapture === 'function') ? window._flipCapture() : null;
                        // 图表保全：替换前暂存已渲染图表节点（防闪烁 + 堵 echarts 孤儿实例泄漏）
                        window._stashCharts(container);
                        // 记录当前展开状态的思考块
                        // [PERF] 简洁模式：completed 思考块是 think-compact（无折叠），跳过 save
                        //       节省 querySelectorAll + Map 构造；非简洁模式行为不变
                        var expandedStates = null;
                        if (!window._toolCompactMode) {{
                            expandedStates = new Map();
                            container.querySelectorAll('.think-block').forEach(function(block) {{
                                expandedStates.set(block.dataset.blockKey, block.dataset.expanded === 'true');
                            }});
                        }}

                        // ── 冻结折叠框 CSS transition 避免 DOM 重建时边框闪烁 ──
                        // container.innerHTML = newHtml 会销毁所有已有 DOM 节点，
                        // 重建后 restoreCollapsibleStates 设置 data-expanded 会触发
                        // 220ms 的 border-color transition（灰色→蓝色），导致可见闪烁。
                        // 用 getElementById 复用已有元素，避免多次 updateContent 时残留重复 <style>
                        const _freezeEl = document.getElementById('_fz') || (function(){{
                            var _el = document.createElement('style');
                            _el.id = '_fz';
                            document.head.appendChild(_el);
                            return _el;
                        }})();
                        _freezeEl.textContent = '.cm-collapsible,.cm-collapsible *,.think-block,.think-block *,.tool-block,.tool-block *,.think-streaming,.think-streaming *,.tool-streaming-block,.tool-streaming-block *,.think-compact,.think-compact *{{transition:none!important}}';

                        // 🐛 修复：innerHTML 替换会重置 scrollTop=0 并触发 scroll 事件，
                        // 导致"置顶闪烁"和用户滚动后永久卡顶的问题。
                        // 解决方案：保存 scrollTop 前置位 + _userScrolledWithin 快照，
                        // innerHTML 后立即恢复滚动位置，避免 paint 间隙闪烁。
                        var _scrollThreshold = {AUTO_SCROLL_THRESHOLD};
                        var _prevScrollTop = document.body.scrollTop;
                        var _wasUserScrolled = window._userScrolledWithin;
                        // 🐛 修复（区域独立 II）：同步保存**正文容器**的 scrollTop——
                        // innerHTML 全量重写会把它重置为 0，而下方只恢复了 body 的。
                        // 思考/工具更新同样触发全量渲染，若不恢复，正文阅读位置
                        // 被抹成 0（上滚态卡顶）/被末尾置底拉到固定底部。
                        var _cpEl = document.getElementById('content-placeholder');
                        var _cpPrevTop = _cpEl ? _cpEl.scrollTop : 0;
                        // 🐛 修复（流式滚动位置重置）：同步保存工具区 scrollTop——
                        // reorganizeContent 增删/重排块导致 scrollHeight 变化时
                        // scrollTop 被钳制，位置丢失；恢复点见下方 _tcEl0 块。
                        var _tcEl0 = document.getElementById('tool-content');
                        var _tcPrevTop0 = _tcEl0 ? _tcEl0.scrollTop : 0;
                        // ── 平滑过渡：新内容以轻微透明度淡入，替代生硬闪烁 ──
                        // 在全量 DOM 替换前设 opacity 略低，替换后在 rAF 中恢复全透明，
                        // CSS transition 驱动平滑淡入效果，减轻 innerHTML 重建的视觉突兀感。
                        // 🛡️ 竞态防护：取消上轮残留的清理定时器
                        if (window._fadeCleanupTimer) {{
                            clearTimeout(window._fadeCleanupTimer);
                        }}
                        // 🐛 修复闪烁：有流式块或折叠框时跳过淡入淡出过渡。
                        // 原因：updateContent 全量重建 DOM 后，think-block / tool-block
                        // 短暂出现在 #content-placeholder（reorganizeContent 迁移前），
                        // opacity 0.88→1 的淡入会放大这个视觉跳变，产生闪烁。
                        // 任何"已有折叠框/工具块"的场景都应跳过淡入，保持视觉稳定。
                        var _hasStreaming = document.querySelector(
                            '#tool-content [data-streaming="true"], ' +
                            '#content-placeholder [data-streaming="true"]'
                        ) !== null;
                        var _hasCollapsible = document.querySelector(
                            '#tool-content .think-block, ' +
                            '#tool-content .tool-block, ' +
                            '#tool-content .think-streaming, ' +
                            '#tool-content .think-compact, ' +
                            '#content-placeholder .think-block, ' +
                            '#content-placeholder .tool-block, ' +
                            '#content-placeholder .think-streaming, ' +
                            '#content-placeholder .think-compact'
                        ) !== null;
                        if (_hasStreaming || _hasCollapsible) {{
                            container.style.opacity = '1';
                            container.style.transition = '';
                        }} else {{
                            // 先切 transition=none，强制 opacity 跳变到 0.88（避免上一轮 transition
                            // 未清理时产生 1→0.88 的淡出动画），再立即恢复 transition 用于后续淡入。
                            container.style.transition = 'none';
                            container.style.opacity = '0.88';
                            void container.offsetHeight;  // 强制同步样式，使跳变立即生效
                            container.style.transition = 'opacity 120ms ease';
                        }}
                        window._suppressScrollEvent = true;
                        try {{
                            container.innerHTML = newHtml;
                        }} catch(e) {{
                            // 🆕 方案 C（#33）：innerHTML 替换异常时**不再 throw**——
                            // 原实现 re-throw 会导致调用方 JS 中断（后续渲染逻辑不执行），
                            // 消息卡片呈现空白（P0 回归根因候选之一）。
                            // 回退为 textContent 纯文本兜底：保证正文永远显示（即使 JS
                            // 异常也不空白），同时恢复透明度避免半透明残影。
                            console.error('updateContent innerHTML failed, fallback to textContent:', e);
                            container.style.opacity = '1';
                            container.style.transition = '';
                            try {{
                                container.textContent = newHtml;
                            }} catch(e2) {{
                                console.error('updateContent textContent fallback also failed:', e2);
                            }}
                        }}
                        // 图表保全：按 key 回插暂存节点（带 _echartInited/_mmdDone，后续 init 扫描天然跳过）
                        window._restoreCharts(container);
                        // 立即恢复滚动位置，防止浏览器在下一次 paint 时呈现 scrollTop=0
                        var _maxScroll = Math.max(0, document.body.scrollHeight - document.body.clientHeight);
                        document.body.scrollTop = Math.min(_prevScrollTop, _maxScroll);

                        // 包裹所有 <table>（不含 .code-table）到可横向滚动的容器中
                        container.querySelectorAll('table:not(.code-table):not(.layout-table)').forEach(function(table) {{
                            // 已被包裹则跳过（如多次调用 updateContent）
                            if (table.parentNode && table.parentNode.classList.contains('table-scroll-wrapper')) return;
                            var wrapper = document.createElement('div');
                            wrapper.className = 'table-scroll-wrapper';
                            table.parentNode.insertBefore(wrapper, table);
                            wrapper.appendChild(table);
                        }});

                        // 恢复展开状态并移除骨架屏动画
                        container.querySelectorAll('.think-content, .think-streaming-preview').forEach(content => {{
                            content.classList.remove('loading');
                        }});

                        restoreCollapsibleStates(container);

                        // 恢复展开状态
                        // [PERF] 简洁模式：expandedStates 为 null，跳过整段（think-compact 无折叠）
                        if (expandedStates) {{
                            container.querySelectorAll('.think-block').forEach(function(block) {{
                                var savedState = expandedStates.get(block.dataset.blockKey);
                                if (savedState !== undefined) {{
                                    block.dataset.expanded = savedState ? 'true' : 'false';
                                    var body = block.querySelector('.cm-collapsible__body');
                                    if (body) {{
                                        body.style.height = savedState ? 'auto' : '0px';
                                        body.style.opacity = savedState ? '1' : '0';
                                    }}
                                }}
                            }});
                        }}

                        // ── 恢复 CSS transition（requestAnimationFrame 使浏览器在下一次
                        // 重绘前已发现元素处于 target 状态，不会触发过渡动画） ──
                        requestAnimationFrame(function() {{
                            var _fe = document.getElementById('_fz');
                            if (_fe) _fe.remove();
                        }});

                        // 初始化 ECharts 图表（rAF 排队：每帧 1 个 init，多图不同帧不卡主线程）
                        window._initEchartsIn(container);

                        // 渲染 Mermaid 图表（内部按需懒加载 polyfill + mermaid）
                        if (typeof renderMermaidBlocks === 'function') renderMermaidBlocks();
                        // 渲染 KaTeX 公式（内部按需懒加载 katex，css 一次性注入）
                        if (typeof renderKatexBlocks === 'function') renderKatexBlocks();

                        // SVG / HTML widget 工具栏挂载（与 mermaid 同时机：全量重建后）
                        if (typeof renderWidgetToolbars === 'function') renderWidgetToolbars();
                        // 插件 fence：按需注入 assets + 按权限装配桥
                        if (typeof window._runFenceAssets === 'function') window._runFenceAssets();
                    if (typeof window._initWidgets === 'function') window._initWidgets();

                        // 将工具/思考块分流到独立滚动容器（仅简洁模式）
                        // 必须在 _suppressScrollEvent=false 之前执行，
                        // 否则移动 DOM 触发的 scroll 事件会错误标记 _userScrolledWithin=true
                        if (window._toolCompactMode) reorganizeContent();

                        // 🐛 修复（区域独立 II）：所有影响正文高度的 DOM 操作（reorganize
                        // 搬移/折叠恢复/ECharts）完成后，恢复重建前的阅读位置。
                        // 钐到新 max（内容变短时不越界）；值实际变化才打 _progScroll
                        // （防 scroll 事件在 _suppressScrollEvent=false 后异步到达被
                        // 误判为用户滚动）；值未变不打标记（避免残留吞掉下次真实滚动）。
                        // 跟随态（_userScrolledUp=false）时下方 _autoScrollStreamingBody
                        // 置底会覆盖此值（正文有新内容需跟随）；上滚态则保持原位。
                        var _cpEl2 = document.getElementById('content-placeholder');
                        if (_cpEl2 && _cpPrevTop > 0) {{
                            var _cpMax = Math.max(0, _cpEl2.scrollHeight - _cpEl2.clientHeight);
                            var _cpTarget = Math.min(_cpPrevTop, _cpMax);
                            if (_cpEl2.scrollTop !== _cpTarget) {{
                                _progScroll(_cpEl2, _cpTarget);
                            }}
                        }}
                        // 🐛 修复（流式滚动位置重置）：恢复工具区滚动位置（钳制补偿）。
                        // save/restore 包裹场景下此处恢复的是中间态（流式块尚未
                        // restore 回来，scrollHeight 偏小），外层 save/restore 会做
                        // 最终恢复，两层取 min 不冲突；裸 updateContent 路径（无活跃
                        // 工具 DOM）此处即最终恢复。
                        if (_tcEl0 && _tcPrevTop0 > 0) {{
                            var _tcMax0 = Math.max(0, _tcEl0.scrollHeight - _tcEl0.clientHeight);
                            var _tcTarget0 = Math.min(_tcPrevTop0, _tcMax0);
                            if (_tcEl0.scrollTop !== _tcTarget0) {{
                                _progScroll(_tcEl0, _tcTarget0);
                            }}
                        }}

                        // 🐛 锚点复位（必须早于下方 auto-scroll）：跟随态由 auto-scroll
                        // 置底覆盖，上滚阅读态保持锚点位置，两者不互相打架。
                        _endDomUpdate();
                        // 🐛 修复：auto-scroll 延后到所有 DOM 操作（table 包裹、折叠框状态恢复、
                        // think-block 展开、ECharts 初始化、reorganizeContent）之后执行，
                        // 确保 scrollHeight 值反映最终渲染结果，避免因 collapsible 展开 /
                        // tool-block restore 等操作在 auto-scroll 后增加高度而导致的
                        // "滚不到底部"问题。
                        // 附加修复：打 auto-scroll 时间戳，让 scroll 事件回调识别
                        // 程序触发的滚动事件（解决 suppress=false 之后异步派发 scroll 的 race）。
                        // 此时 _suppressScrollEvent 仍为 true，所有 scroll 事件仍被抑制。
                        if (!_wasUserScrolled) {{
                            _autoScrollStreamingBody();
                            window._userScrolledWithin = false;
                        }} else {{
                            var _wasAtBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < _scrollThreshold;
                            if (_wasAtBottom) {{
                                _autoScrollStreamingBody();
                                window._userScrolledWithin = false;
                            }}
                        }}
                        // 同步 _prevScrollTop，让 delta 检测有正确基线
                        window._prevScrollTop = document.body.scrollTop;
                        window._autoScrollTime = performance.now();
                        window._suppressScrollEvent = false;

                        // FLIP Play：重排（reorganizeContent 搬移工具/思考块）完成后，
                        // 把位置突变补间成平滑位移；入队串行，避免与归位/折叠动画叠加。
                        if (_flipPrev && typeof window._flipPlay === 'function') window._flipPlay(_flipPrev, 220);

                        // 预览文字打字机：本次渲染新落地的思考/工具预览行逐字显现
                        if (typeof window._ptPlay === 'function') window._ptPlay();

                        // ── 恢复全透明度：在下一帧前 fade in，CSS transition 驱动平滑淡入 ──
                        // 🛡️ 竞态防护：递增 token + 定时器引用，防止连续 updateContent 时
                        // 上轮清理误清本轮 transition，或清理定时器残留导致 transition 提前消失。
                        // 🐛 修复闪烁：有流式块时跳过 fade-in transition（已在上述同步代码中跳过）
                        if (!_hasStreaming) {{
                            window._fadeToken = (window._fadeToken || 0) + 1;
                            var _thisFadeToken = window._fadeToken;
                            requestAnimationFrame(function() {{
                                container.style.opacity = '1';
                                // 动画完成后清理 transition，避免影响后续 resize 等操作
                                window._fadeCleanupTimer = setTimeout(function() {{
                                    if (window._fadeToken === _thisFadeToken) {{
                                        container.style.transition = '';
                                    }}
                                    window._fadeCleanupTimer = null;
                                }}, 130);
                                // 本轮清理定时器已注册，若下一轮 updateContent 在 130ms 内到达，
                                // 会在开头 clearTimeout 取消此定时器，同时 _fadeToken 递增使回调跳过。
                            }});
                        }}

                        // 使用延迟报告，确保浏览器布局完成
                        // [F5+B] 走防抖通道：坞态归位/折叠过渡期间由
                        // _collapsibleHeightReporting 抑制中间态，transitionend 终值
                        // 单报接管；16ms ≈ 1 帧，布局完成后即可上报。
                        setTimeout(() => reportHeightDebounced(), 16);
                    }}
                }}
                // ===== B1 差量渲染：追加闭合段到 DOM（不整块替换） =====
                // 移除所有 data-incremental="true" 的增量纯文本节点，
                // 再把新闭合的格式化 HTML 追加到 #content-placeholder 末尾。
                // 🐛 修复（正文尾部丢失）：tailHtml 参数——移除增量节点时
                // 会连带删除**未闭合的尾部文本**（尚未到 \\n\\n 的段落后半段），
                // 且新闭合段 HTML 不含它 → 尾部永久消失（用户可见"正文显示不全"）。
                // 因此 Python 端把未闭合尾部的**行内渲染 HTML**传进来，
                // 移除后重建增量节点保尾（innerHTML 注入，流式期间 markdown
                // 语法即时格式化，不再字面显示源码）。
                // ===== 流式图表骨架复用：保持 CSS 动画连续 =====
                // 未闭合的图表 fence 每轮都随 tail 重建，骨架节点被销毁又新建 →
                // CSS 动画每 150~500ms 重启一次，用户看到"骨架抖动"而非平滑的生成反馈。
                // 对策：移除增量节点前先把已有骨架摘出，重建后再挂回末尾——流式期间
                // 始终是同一个 DOM 节点，动画相位连续。
                window._takeSkeleton = function (container) {{
                    var sk = container.querySelector('.chart-skeleton');
                    if (!sk) return null;
                    if (sk.parentNode) sk.parentNode.removeChild(sk);
                    return sk;
                }};
                // 本轮 HTML 是否已产出真图（fence 闭合）→ 骨架该退场了
                window._hasRealChartIn = function (html) {{
                    return !!html && (html.indexOf('echarts-container') >= 0 || html.indexOf('mermaid-block') >= 0);
                }};
                // 复用骨架：清掉新渲染出的同款骨架（避免双骨架），把旧节点挂到末尾
                window._reattachSkeleton = function (container, sk, newHtml, tailHtml, tailDiv) {{
                    if (!sk) return;
                    if (window._hasRealChartIn(newHtml) || window._hasRealChartIn(tailHtml)) return;  // 图表已闭合，骨架自然丢弃
                    var _inner = (tailDiv || container).querySelector('.chart-skeleton');
                    if (_inner && _inner.parentNode) _inner.parentNode.removeChild(_inner);
                    container.appendChild(sk);
                }};

                function updateContentAppend(newHtml, tailHtml) {{
                    const container = document.getElementById('content-placeholder');
                    if (!container) return;
                    // 打字机：增量节点即将被移除并以格式化 HTML 重建（含未揭示文本），
                    // 丢弃揭示缓冲防重复追加。
                    if (typeof window._twReset === 'function') window._twReset();
                    // 骨架复用：必须在移除增量节点之前摘出（否则随 tail 一起被删）
                    var _skel = window._takeSkeleton(container);
                    // 段落分隔已由本次渲染的 HTML 表达，清掉挂起分段标记
                    container.removeAttribute('data-pending-break');
                    // 追加格式化 HTML（含 table 包裹等后续处理）
                    container.insertAdjacentHTML('beforeend', newHtml);
                    // 🐛 修复（正文尾部丢失）：未闭合尾部重建为增量渲染节点。
                    // ⚠️ 用 <div> 而非 <p> 包裹：tailHtml 是 md.convert 产物
                    // （含 <p>/<h1>/<ul>/<pre> 等块级元素），<p> 内嵌块级会触发
                    // HTML 解析器自动闭合/提升，导致 DOM 结构错乱。
                    if (tailHtml) {{
                        var tailDiv = document.createElement('div');
                        tailDiv.setAttribute('data-incremental', 'true');
                        // [B1] data-rendered 标记：_append_text_incremental 检测到
                        // 该标记时不再 textContent 原地追加（会抹掉已渲染的 HTML），
                        // 改为新建纯文本增量节点。
                        tailDiv.setAttribute('data-rendered', 'true');
                        tailDiv.innerHTML = tailHtml;
                        container.appendChild(tailDiv);
                    }}
                    // 🐛 修复（阅读位置被钳制）：旧增量节点改为**最后**删除。
                    // 「先删后插」会让容器 scrollHeight 瞬时塌陷，浏览器把 scrollTop 钳到
                    // 更小的 max —— 坞态正文内滚时每来一个 chunk 用户位置就漂一次。
                    // 先插新内容再删旧的，高度单调不减，scrollTop 没有钳制机会。
                    container.querySelectorAll('[data-incremental="true"]').forEach(function(el) {{
                        if (el !== tailDiv) el.remove();
                    }});
                    // 骨架挂回末尾（图表仍在生成中）→ CSS 动画相位连续，不抖动
                    window._reattachSkeleton(container, _skel, newHtml, tailHtml, typeof tailDiv !== 'undefined' ? tailDiv : null);
                    // 🐛 修复（思考块滞留正文）：与全量 updateContent 对齐——简洁模式下
                    // 差量追加的思考/工具块立即搬移到"工具与思考"区，否则滞留
                    // #content-placeholder，视觉上"思考内容在正文闪现，随后消失回折叠区"。
                    if (window._toolCompactMode) reorganizeContent();
                    // 包裹所有 <table>（不含 .code-table）到可横向滚动的容器中
                    container.querySelectorAll('table:not(.code-table):not(.layout-table)').forEach(function(table) {{
                        if (table.parentNode && table.parentNode.classList.contains('table-scroll-wrapper')) return;
                        var wrapper = document.createElement('div');
                        wrapper.className = 'table-scroll-wrapper';
                        table.parentNode.insertBefore(wrapper, table);
                        wrapper.appendChild(table);
                    }});
                    // 恢复展开状态
                    restoreCollapsibleStates(container);
                    // 同步滚动到底（流式期间通常期望跟到底部）
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody();
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody();
                            window._userScrolledWithin = false;
                        }}
                    }}
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    // 初始化 ECharts 图表（追加的闭合段可能含 echarts 代码块；rAF 排队）
                    window._initEchartsIn(container);
                    // 渲染 Mermaid 图表（追加的闭合段可能含 ```mermaid 代码块）
                    if (typeof renderMermaidBlocks === 'function') renderMermaidBlocks();
                    // 渲染 KaTeX 公式（追加的闭合段可能含公式）
                    if (typeof renderKatexBlocks === 'function') renderKatexBlocks();
                    // SVG / HTML widget 工具栏挂载（同上时机）
                    if (typeof renderWidgetToolbars === 'function') renderWidgetToolbars();
                    // 插件 fence：追加的闭合段可能带入新的插件 fence
                    if (typeof window._runFenceAssets === 'function') window._runFenceAssets();
                    if (typeof window._initWidgets === 'function') window._initWidgets();
                    // 预览文字打字机：差量段里新落地的思考/工具预览行逐字显现
                    if (typeof window._ptPlay === 'function') window._ptPlay();
                    // 使用延迟报告，确保浏览器布局完成
                    setTimeout(() => reportHeight(), 30);
                }}
                // ===== 差量收尾：流式思考块就地定稿 =====
                // 结束这一拍不再整页替换 innerHTML：只把仍处流式态的 .think-streaming
                // 按 data-flip-key 位置键（流式态与完成态共用同一 ordinal）替换成
                // 完成态折叠框。稳定区与正文段落完全不动 → 没有整体重排闪动。
                function finalizeStreamingBlocks(replacements) {{
                    try {{
                        var keys = Object.keys(replacements || {{}});
                        if (!keys.length) return;
                        var cp = document.getElementById('content-placeholder');
                        var tc = document.getElementById('tool-content');
                        for (var i = 0; i < keys.length; i++) {{
                            var idx = keys[i];
                            var el = document.querySelector('.think-streaming[data-streaming="true"][data-flip-key="think-' + idx + '"]');
                            if (!el) continue;
                            var tmp = document.createElement('div');
                            tmp.innerHTML = replacements[idx];
                            var nb = tmp.firstElementChild;
                            if (!nb) continue;
                            // 标记为恢复块：跳过 CSS 入场动画，避免“消失→重现”闪烁
                            nb.setAttribute('data-restored', 'true');
                            el.parentNode.replaceChild(nb, el);
                            // 简洁模式：完成态思考块归属“工具与思考”区（与
                            // _maybe_finish_thinking_for_tool 的处理保持一致）
                            if (window._toolCompactMode && tc && nb.parentNode === cp) {{
                                tc.appendChild(nb);
                            }}
                        }}
                        if (window._toolCompactMode && typeof reorganizeContent === 'function') reorganizeContent();
                        if (typeof reportHeight === 'function') setTimeout(function () {{ reportHeight(); }}, 30);
                    }} catch (e) {{
                        if (window.console) console.log('finalize-failed:' + e);
                    }}
                }}
                // ===== B1 差量渲染：未闭合尾部行内渲染（整体替换增量节点） =====
                // 无空行分隔的长段落（`\\n\\n` 缺失）没有闭合段可差量渲染，
                // 尾部长时间以纯文本显示 markdown 源码（**加粗**、`code`、[链接]）。
                // Python 端把尾部整体 convert 成行内 HTML 传入，替换所有增量节点
                // （纯文本 + 已渲染），DOM 尾部始终是**一个** data-rendered 渲染节点，
                // 后续 _append_text_incremental 在其后追加纯文本，差量/全量渲染再整体替换。
                function updateTailHtml(html) {{
                    const container = document.getElementById('content-placeholder');
                    if (!container || !html) return;
                    // 打字机：尾部将被整体行内重渲染（含未揭示文本），丢弃揭示缓冲。
                    if (typeof window._twReset === 'function') window._twReset();
                    // 骨架复用：必须在移除增量节点之前摘出（否则随 tail 一起被删）
                    var _skel = window._takeSkeleton(container);
                    // 段落分隔已由本次尾部 HTML 表达，清掉挂起分段标记
                    container.removeAttribute('data-pending-break');
                    // ⚠️ 用 <div> 而非 <p> 包裹：html 是 md.convert 产物（块级元素），
                    // <p> 内嵌块级会触发解析器自动闭合，结构错乱。
                    var tailDiv = document.createElement('div');
                    tailDiv.setAttribute('data-incremental', 'true');
                    tailDiv.setAttribute('data-rendered', 'true');
                    tailDiv.innerHTML = html;
                    container.appendChild(tailDiv);
                    // 🐛 修复（阅读位置被钳制）：同 updateContentAppend，先加后删。
                    container.querySelectorAll('[data-incremental="true"]').forEach(function(el) {{
                        if (el !== tailDiv) el.remove();
                    }});
                    // 骨架挂回末尾（图表仍在生成中）；闭合时 _reattachSkeleton 自动丢弃
                    window._reattachSkeleton(container, _skel, html, '', tailDiv);
                    // 与 updateContentAppend 对齐：表格包裹 + 折叠状态恢复 + 滚动
                    container.querySelectorAll('table:not(.code-table):not(.layout-table)').forEach(function(table) {{
                        if (table.parentNode && table.parentNode.classList.contains('table-scroll-wrapper')) return;
                        var wrapper = document.createElement('div');
                        wrapper.className = 'table-scroll-wrapper';
                        table.parentNode.insertBefore(wrapper, table);
                        wrapper.appendChild(table);
                    }});
                    restoreCollapsibleStates(container);
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody();
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody();
                            window._userScrolledWithin = false;
                        }}
                    }}
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    // 与 updateContentAppend 对齐：尾部整段替换同样可能带入刚闭合的
                    // 图表/公式 fence。原先缺这四连 → 走 updateTailHtml 路径（无空行
                    // 分隔的长段落）时 echarts / mermaid / katex / widget 工具栏
                    // 全部静默不初始化。
                    window._initEchartsIn(container);
                    if (typeof renderMermaidBlocks === 'function') renderMermaidBlocks();
                    if (typeof renderKatexBlocks === 'function') renderKatexBlocks();
                    if (typeof renderWidgetToolbars === 'function') renderWidgetToolbars();
                    // 插件 fence：尾部整段替换同样可能带入新的插件 fence
                    if (typeof window._runFenceAssets === 'function') window._runFenceAssets();
                    if (typeof window._initWidgets === 'function') window._initWidgets();
                    setTimeout(() => reportHeight(), 30);
                }}
                {_CONTENT_AUTOSCROLL_JS}
                function reportHeight() {{
                    // 用 body.scrollHeight 获取完整内容高度。
                    // getBoundingClientRect 在 html{{overflow:hidden}} 下
                    // 返回视口高度而非内容高度，导致卡片无法完全展开。
                    const _b = document.body;
                    if (!_b) return;
                    const h = _b.scrollHeight;
                    // 🐛 滚动判据修复：body 才是真正的滚动容器
                    // （CSS body{{overflow-y:scroll; max-height}}），而 Qt 侧的
                    // page().scrollPosition() 是文档级、恒为 0，无法用于边界判定。
                    // 故在高频回传中顺带携带 body 的 scrollTop / clientHeight，
                    // Python 侧据此算出真实可滚动量 = scrollHeight - clientHeight。
                    // 注意保持'|'分隔协议，旧解析器（仅高度）仍可工作。
                    // 🐛 第 4 字段「卡片内阅读标志」：body/cp/tc 任一被用户上滚
                    // 即为 1（语义与各容器自动滚底守卫同源，单一真相）。缺此字段时
                    // 流式每个高度变化都会把卡片拉回「底部对齐」固定姿态。
                    var _rd = (window._userScrolledWithin === true);
                    try {{
                        var _cpR = document.getElementById('content-placeholder');
                        if (_cpR && _cpR._userScrolledUp === true) _rd = true;
                        var _tcR = document.getElementById('tool-content');
                        if (_tcR && _tcR._userScrolledUp === true) _rd = true;
                    }} catch (_e) {{}}
                    console.log('pywebview_height:' + h + '|' + (_b.scrollTop|0) + '|' + (_b.clientHeight|0) + '|' + (_rd ? '1' : '0'));
                }}
                // 批量报告高度：合并同一帧内的多次请求，动画期间仍暂停报告。
                //
                // [T29] rAF ×3 → ×1：原本的"3 帧合并"是为「流式每 chunk 一次 IPC」
                // 设计的，但打字机揭示队列（_twStep）已把上报节流到 ≥80ms 一次
                // （约 12.5Hz），两层节流串联纯属重复 —— 白送 ~33ms 的高度延迟，
                // 与后续 Python 侧 80ms 防抖相加后总延迟达 130~210ms，表现为卡片
                // 高度"憋一下再整块蹦高"的顿挫感。保留单帧合并（同一帧内多次
                // 请求去重）即可，合并语义不变、延迟砍到 ~16ms。
                let _heightReportPending = false;
                function reportHeightDebounced() {{
                    if (_collapsibleHeightReporting) return;  // 动画期间暂停
                    if (_heightReportPending) return;
                    _heightReportPending = true;
                    requestAnimationFrame(function _batchTick() {{
                        reportHeight();
                        _heightReportPending = false;
                    }});
                }}

                // ===== 正文/非正文分区：将工具块/思考块从内容区移到独立可滚动容器 =====
                // 编辑类工具（write/edit/multi_edit）保留在正文中，不迁移到"工具与思考"区域
                // 子智能体/提问类工具（subagent_para/question）与编辑工具类似，
                // 属于 AI 与用户之间的直接交互结果，保留在正文中体验更连贯。
                // 工具名集合由 Python 渲染端派生（registry 声明），经 data-keep-in-content 属性传入，
                // JS 不再硬编码工具名。
                var _EDIT_TOOLS_SELECTOR = ':not([data-keep-in-content="true"])';

                // 更新"工具与思考"标题（总项数）
                function _updateToolSectionHeader() {{
                    var toolContent = document.getElementById('tool-content');
                    var separator = document.getElementById('tool-separator');
                    if (!separator) return;
                    var total = toolContent ? toolContent.children.length : 0;
                    var titleSpan = separator.querySelector(':scope > span:not(.chevron)');
                    if (titleSpan) {{
                        titleSpan.textContent = total > 0 ? '⚙ 工具与思考 · ' + total + ' 项' : '⚙ 工具与思考';
                    }}
                    // ── 自动展开：流式时有新工具且当前折叠 → 展开 ──
                    var _hasStreaming = document.querySelector('#tool-content [data-streaming="true"]');
                    var _tsEl = document.getElementById('tool-section');
                    if (_hasStreaming && _tsEl && _tsEl.getAttribute('data-collapsed') === 'true') {{
                        // 过渡期间抑制中间态高度报告，结束后终值单报
                        _beginToolSectionTransition();
                        _tsEl.setAttribute('data-collapsed', 'false');
                        separator.setAttribute('aria-expanded', 'true');
                    }}
                }}

                function reorganizeContent() {{
                    var container = document.getElementById('content-placeholder');
                    var toolSection = document.getElementById('tool-section');
                    var toolContent = document.getElementById('tool-content');
                    if (!container || !toolContent || !toolSection) return;
                    // 找出容器内所有需要迁移到工具区的块（编辑类工具保留在正文）
                    // 🆕 .think-compact：简洁模式下的思考纯文本行，非折叠框
                    var blocks = container.querySelectorAll(
                        '.tool-block' + _EDIT_TOOLS_SELECTOR + ', ' +
                        '.think-block, .think-streaming, .think-compact, ' +
                        '[data-tool-call-id]' + _EDIT_TOOLS_SELECTOR
                    );
                    if (blocks.length === 0) {{
                        // 容器没有需要迁移的块 —— 若 tool-content 空就隐藏整个区
                        // [#12 R1] display:none 会把节点移出渲染树（scrollTop 强制
                        // 归零），切 none 前快照、恢复 '' 后写回，避免折叠框内滚动
                        // 位置丢失（"折叠框内滚轮置顶"回归修复）。
                        var _r1WasHidden = toolSection.style.display === 'none';
                        if (!_r1WasHidden) toolSection._r1SavedTop = toolContent.scrollTop;
                        if (toolContent.children.length === 0) {{
                            toolSection.style.display = 'none';
                            return;
                        }}
                        // tool-content 仍有 data-tool-injected 流式块 / 旧搬移块
                        // （markdown 被缩短、块被删除），仍需刷新 header
                        toolSection.style.display = '';
                        if (_r1WasHidden) _progScroll(toolContent, toolSection._r1SavedTop || 0);
                        _updateToolSectionHeader();
                        // 坞态（流式中）：自动滚底显示最新活动（尊重用户上滚）
                        if (window._streamingActive && window._toolCompactMode) _scrollToolContentToBottom();
                        return;
                    }}
                    // ── [PERF v2] 单次扫描 blocks：posMap + thinkKeys + toolIds + thinkStreaming ──
                    // 原实现有 4 个独立 forEach 重复遍历，v2 合并为单次，O(n²) → O(n)
                    var posMap = Object.create(null);
                    var _currentThinkKeys = new Set();
                    var _currentToolIds = new Set();
                    // 🐛 修复（工具完成框残留/两份/沉底）：无 tool_call_id 的块也要登记身份。
                    // 【根因】`<tool>` 协议块解析不到 tool_call_id 时产物是 data-tool-call-id=""，
                    // 而下方过期清理分支用 `if (_etid && ...)` 判定 —— **空串为假**，整个条件
                    // 短路 → 这类块永不清理。内容一变 block_key 就变（sha1 of 工具名+参数+结果），
                    // 新块迁入工具区、旧块留在原地，渲染几次就几份（真实 DOM 复现：
                    // tests/debug/unclosed_tool_completed_linger.py 工具区块数 1→2→3→4）。
                    // 【修复】用 data-block-key 作为第二身份判据：无 id 的块按 bk 判过期。
                    // 与 _currentToolIds 同处单次扫描登记（保持 PERF v2 的 O(n) 单遍结构）。
                    var _currentToolBlockKeys = new Set();
                    var _hasNewThinkStreaming = false;
                    var _thinkStreamingEl = null;
                    for (var _bi = 0; _bi < blocks.length; _bi++) {{
                        var _el = blocks[_bi];
                        var _bk = _el.getAttribute('data-block-key');
                        var _tid = _el.getAttribute('data-tool-call-id');
                        if (_bk) posMap['bk:' + _bk] = _bi;
                        if (_tid) {{
                            posMap['tcid:' + _tid] = _bi;
                            _currentToolIds.add(_tid);
                        }}
                        if (_bk && (
                            _el.classList.contains('think-block')
                            || _el.classList.contains('think-streaming')
                            || _el.classList.contains('think-compact')
                        )) {{
                            _currentThinkKeys.add(_bk);
                        }}
                        // 工具块（含空 id）统一登记 block_key，供清理判据使用
                        if (_bk && !_tid && _el.classList.contains('tool-block')) {{
                            _currentToolBlockKeys.add(_bk);
                        }}
                        if (_el.classList.contains('think-streaming')) {{
                            _hasNewThinkStreaming = true;
                            _thinkStreamingEl = _el;
                        }} else if (!_bk && !_tid) {{
                            // 无稳定标识的块（备用扩展）—— 用 blocks 中的序号
                            _el._posIdx = _bi;
                        }}
                    }}
                    // ── [PERF v2] 单次遍历 toolContent 子节点：think-streaming + 过期清理 ──
                    var _oldThinkStreaming = null;
                    var _toolKids = toolContent.children;
                    for (var _ti = 0; _ti < _toolKids.length; _ti++) {{
                        var _tk = _toolKids[_ti];
                        if (_tk.classList.contains('think-streaming') && !_oldThinkStreaming) {{
                            _oldThinkStreaming = _tk;
                        }}
                    }}
                    if (!_hasNewThinkStreaming && _oldThinkStreaming) {{
                        _oldThinkStreaming.remove();
                        _oldThinkStreaming = null;
                    }}
                    // 清理过期 think-block / think-compact + 过期 tool-block
                    var _existingKids = Array.prototype.slice.call(toolContent.children);
                    for (var _ei = 0; _ei < _existingKids.length; _ei++) {{
                        var _eel = _existingKids[_ei];
                        if (!_eel || !_eel.parentNode) continue;
                        var _ebk = _eel.getAttribute('data-block-key');
                        var _etid = _eel.getAttribute('data-tool-call-id');
                        // 过期 think 块
                        if (
                            _ebk
                            && !_currentThinkKeys.has(_ebk)
                            && !_etid
                            && (
                                _eel.classList.contains('think-block')
                                || _eel.classList.contains('think-compact')
                            )
                        ) {{
                            _eel.remove();
                            continue;
                        }}
                        // 过期 tool 块（保留流式进行中的块）
                        // 🐛 修复（工具完成框残留/两份/沉底）：身份判据由「仅 tool_call-id」
                        // 扩为「id 或 block_key」。无 id 的块（data-tool-call-id=""）在旧条件下
                        // 整个短路 → 永不清理 → 每轮渲染追加一份、恒沉底（详见扫描处注释）。
                        // ⚠️ 两个判据互斥使用：有 id 用 id（同一工具多形态块共用 id），
                        // 无 id 才用 bk（避免 id 与 bk 双判据对同一块给出矛盾结论）。
                        if (
                            _eel.getAttribute('data-streaming') !== 'true'
                            && _eel.classList.contains('tool-block')
                            && (
                                (_etid && !_currentToolIds.has(_etid))
                                || (!_etid && _ebk && !_currentToolBlockKeys.has(_ebk))
                            )
                        ) {{
                            _eel.remove();
                            continue;
                        }}
                    }}
                    // 从正文移除已存在稳定标识的重叠块，其余搬移到工具区
                    // 🐛 修复吞内容 + 闪烁：think-streaming 用 replaceChild 原地替换
                    // （保 DOM 位置，更新内容）
                    var moved = false;
                    for (var _mi = 0; _mi < blocks.length; _mi++) {{
                        var _mel = blocks[_mi];
                        var _mbk = _mel.getAttribute('data-block-key');
                        var _mtid = _mel.getAttribute('data-tool-call-id');
                        var _dup = (_mbk && toolContent.querySelector('[data-block-key="' + _mbk + '"]'))
                                || (_mtid && toolContent.querySelector('[data-tool-call-id="' + _mtid + '"]'));
                        if (_dup) {{
                            if (_mel.parentNode === container) _mel.remove();
                        }} else if (_mel.classList.contains('think-streaming') && _oldThinkStreaming) {{
                            if (_mel.parentNode === container && _oldThinkStreaming.parentNode) {{
                                _oldThinkStreaming.parentNode.replaceChild(_mel, _oldThinkStreaming);
                            }}
                        }} else if (_mel.parentNode === container) {{
                            toolContent.appendChild(_mel);
                            moved = true;
                        }}
                    }}
                    // ▓▓ Bug B 方案 D+：把 markdown 渲染块的 data-order 补齐，统一排序/插入的尺度 ▓▓
                    // 根因：flow 结束时 save/restore 会按 data-order 把"仍在流式"的工具块插回，
                    // 但其插入循环只比较带 data-order 的子节点；而 think 块/完成工具块由 markdown
                    // 渲染，只有 posMap（btn-key/tool-call-id）没有 data-order → 插入循环找不到
                    // 目标 → appendChild 沉底 → 折叠框内"所有思考在前、所有工具在后"（容器 posMap
                    // 未含仍在流式、尚未进入 _content_data 的工具块，需用其 floor(data-order) 修正）。
                    // 此处为缺 data-order 的块补上 = posMap 位置 + 排在其前的流式工具数，使 sort
                    // 与 save/restore 插入共用同一把尺子（与 _count_think_tool_prefix 同尺度）。
                    // 🆕 方案 E：_streamFloors 初始化为 save 阶段暂存的流式块 data-order
                    // （window.__pendingStreamFloors）——save 会把所有 data-tool-call-id 块
                    // （含仍在流式的工具块）从 DOM 移除，导致下方从 toolContent.children 收集
                    // 时恒为空、修正失效；暂存数组补回这条信息，使"排在其前的流式工具数"
                    // 在坞态归位瞬间也能正确计入（否则思考块补齐的 data-order 偏小 → restore
                    // 插回的工具块找不到比它大的节点 → 全部 appendChild 沉底）。
                    var _streamFloors = (window.__pendingStreamFloors || []).slice();
                    var _allKids = Array.prototype.slice.call(toolContent.children);
                    for (var _sf = 0; _sf < _allKids.length; _sf++) {{
                        if (_allKids[_sf].getAttribute('data-streaming') === 'true') {{
                            var _sfOd = parseFloat(_allKids[_sf].getAttribute('data-order'));
                            if (!isNaN(_sfOd)) _streamFloors.push(Math.floor(_sfOd));
                        }}
                    }}
                    // 🆕 Bug B 方案 G：标记"是否补齐过 data-order"。save/restore 后
                    // tool-content 的键序列可能与上次相同（__lastOrder diff 误判"顺序未变"
                    // → 跳过 sort），但块物理顺序已被 restore/迁移打乱（data-order 与
                    // 物理顺序不一致）→ 折叠框内"思考在前、工具在后"。凡补齐过
                    // data-order（说明经历 markdown 重渲染 + save/restore），必须强制 sort。
                    var _assignedDataOrder = false;
                    for (var _oa = 0; _oa < _allKids.length; _oa++) {{
                        var _oaKid = _allKids[_oa];
                        if (_oaKid.getAttribute('data-order') !== null) continue;
                        var _oaBk = _oaKid.getAttribute('data-block-key');
                        var _oaTid = _oaKid.getAttribute('data-tool-call-id');
                        var _oaPos = (_oaBk && posMap['bk:' + _oaBk] !== undefined)
                            ? posMap['bk:' + _oaBk]
                            : (_oaTid && posMap['tcid:' + _oaTid] !== undefined)
                                ? posMap['tcid:' + _oaTid]
                                : null;
                        if (_oaPos === null) continue;
                        var _oaBefore = 0;
                        for (var _sf2 = 0; _sf2 < _streamFloors.length; _sf2++) {{
                            if (_streamFloors[_sf2] <= _oaPos) _oaBefore++;
                        }}
                        _oaKid.setAttribute('data-order', String(_oaPos + _oaBefore));
                        _assignedDataOrder = true;
                    }}
                    // ── [PERF v2] 顺序哈希 diff：键序列未变时跳过 sort + appendChild ──
                    // 流式期间大部分 updateContent 走"键序列未变"快路径，避免 sort 抖动
                    var _curKeys = [];
                    var _curKids = toolContent.children;
                    for (var _ci = 0; _ci < _curKids.length; _ci++) {{
                        var _ck = _curKids[_ci];
                        var _ckbk = _ck.getAttribute('data-block-key');
                        var _cktid = _ck.getAttribute('data-tool-call-id');
                        if (_ckbk) {{
                            _curKeys.push('bk:' + _ckbk);
                        }} else if (_cktid) {{
                            _curKeys.push('tcid:' + _cktid);
                        }} else {{
                            _curKeys.push('idx:' + _ci);
                        }}
                    }}
                    var _lastOrder = toolContent.__lastOrder;
                    // 🆕 Bug B 方案 G：补齐过 data-order（markdown 重渲染 + save/restore 路径）
                    // → 键序列 diff 不可靠（键相同但物理顺序已被 restore 打乱），强制 sort。
                    var _orderChanged = _assignedDataOrder || !_lastOrder || _lastOrder.length !== _curKeys.length;
                    if (!_orderChanged) {{
                        for (var _di = 0; _di < _curKeys.length; _di++) {{
                            if (_curKeys[_di] !== _lastOrder[_di]) {{
                                _orderChanged = true;
                                break;
                            }}
                        }}
                    }}
                    // 🐛 修复（完成框沉底）：restore 恒 appendChild 沉底 + append_tool_result
                    // 原地 replaceChild 转换（继承 data-order、物理位置不动）后，完成块
                    // data-order 正确但物理滞留底部；S1（正文先于工具结束）终渲染后无新
                    // markdown 块 → 键集合恒同且无块缺 data-order → 上述 diff 全绿跳过
                    // sort → 沉底固化。按物理顺序检查 data-order 单调性：跳过运行中块
                    // （1e9 沉底语义，data-order 是调用时刻旧快照）与无 data-order 块
                    // （think-streaming 待补齐），发现倒序即强制 sort（getPos 为权威
                    // 排序，物理已正确时 sort 结果不变，零视觉影响）。
                    if (!_orderChanged) {{
                        var _prevOd = -Infinity;
                        for (var _mi = 0; _mi < _curKids.length; _mi++) {{
                            var _mk = _curKids[_mi];
                            if (_mk.classList && _mk.classList.contains('tool-streaming-block')) continue;
                            var _mod = _mk.getAttribute('data-order');
                            if (_mod === null) continue;
                            var _mv = parseFloat(_mod);
                            if (isNaN(_mv)) continue;
                            if (_mv < _prevOd) {{ _orderChanged = true; break; }}
                            _prevOd = _mv;
                        }}
                    }}
                    if (_orderChanged) {{
                        // 顺序变了：用 sort 一次性重排（避免多次 appendChild 抖动）
                        var _sortedChildren = Array.prototype.slice.call(toolContent.children).sort(function(a, b) {{
                            function getPos(el) {{
                                // 🆕 F1：运行中工具块（tool-streaming-block）强制沉底——
                                // 不被调用时刻快照 data-order 排到思考块上方（dock 语义：
                                // 最新活动最下）。be57674d 方案 D 引入 data-order 排序后，
                                // 运行中块 data-order 是工具调用时刻锚点前 think/tool 计数
                                // （固定快照），后续思考块补出更大 data-order → sort 把运行中
                                // 块排到思考块上方。此处对运行中工具块直接返回 1e9（沉底），
                                // 与 be57674d~1 回归前行为一致（无 data-order → 1e9 恒沉底）。
                                // ⚠️ 必须用 class 判定而非 data-streaming 属性——think-streaming
                                // （思考流式块）同样带 data-streaming="true"，但应保持在上方。
                                if (el.classList && el.classList.contains('tool-streaming-block')) {{
                                    return 1e9;
                                }}
                                // 🆕 方案 D：data-order 优先——JS 注入的工具块
                                // （save-restore 恢复块 / append_tool_result
                                // 完成块）不在 #content-placeholder 中，posMap 查不到，
                                // 无 data-order 会返回 1e9 恒沉底 → 折叠框内
                                // "所有思考在前、所有工具在后"（Bug B 第三条路径）。
                                // data-order 与 posMap 同尺度（锚点前 think/tool 块
                                // 计数 + 同锚点序号细分），可直接混合比较排序。
                                var od = el.getAttribute('data-order');
                                if (od !== null) {{
                                    return parseFloat(od);
                                }}
                                var bk = el.getAttribute('data-block-key');
                                var tid = el.getAttribute('data-tool-call-id');
                                if (bk && posMap['bk:' + bk] !== undefined) return posMap['bk:' + bk];
                                if (tid && posMap['tcid:' + tid] !== undefined) return posMap['tcid:' + tid];
                                if (el._posIdx !== undefined) return el._posIdx;
                                return 1e9;
                            }}
                            return getPos(a) - getPos(b);
                        }});
                        for (var _ri = 0; _ri < _sortedChildren.length; _ri++) {{
                            toolContent.appendChild(_sortedChildren[_ri]);
                        }}
                        toolContent.__lastOrder = _curKeys;
                    }}
                    // [#12 R1] display 切换空窗保护：none→'' 恢复时写回快照，
                    // ''→none 前更新快照（同 reorganizeContent 空分支语义）。
                    var _r1WasHidden = toolSection.style.display === 'none';
                    if (!_r1WasHidden) toolSection._r1SavedTop = toolContent.scrollTop;
                    toolSection.style.display = toolContent.children.length > 0 ? '' : 'none';
                    if (_r1WasHidden && toolSection.style.display === '') _progScroll(toolContent, toolSection._r1SavedTop || 0);
                    if (moved || toolContent.children.length > 0) _updateToolSectionHeader();
                    // 坞态（流式中）：新条目进入后自动滚底
                    if (window._streamingActive && window._toolCompactMode) _scrollToolContentToBottom();
                    // 预览文字打字机：搬移进工具区的思考/工具预览行逐字显现
                    if (typeof window._ptPlay === 'function') window._ptPlay();
                }}
                // 工具与思考区头部折叠/展开：用 transitionend 精确监听动画结束，
                // 替代不可靠的 setTimeout(220) —— 动画时长若被 CSS 改动会失准
                function _toggleToolSection(sep, evt) {{
                    var toolSection = document.getElementById('tool-section');
                    if (!sep || !toolSection) return;
                    if (evt) {{ evt.stopPropagation(); evt.preventDefault(); }}
                    var collapsed = toolSection.getAttribute('data-collapsed') === 'true';
                    // 过渡期间抑制中间态高度报告，结束后终值单报（替代旧 transitionend+setTimeout 双报告）
                    _beginToolSectionTransition();
                    toolSection.setAttribute('data-collapsed', collapsed ? 'false' : 'true');
                    sep.setAttribute('aria-expanded', collapsed ? 'true' : 'false');
                    try {{ sessionStorage.setItem('_toolSectionCollapsed', collapsed ? '0' : '1'); }} catch(_err) {{}}
                }}
                // SVG 图形节点也要能挂 .context-tag：<g class="context-tag"
                // data-type="ask" data-content="..."> 与 <span> 走同一条点击链。
                // Element.closest 在 SVG 子树上并非处处可靠（Chromium 83 的
                // SVGElement 原型链），故先试 closest，失败或未命中再退化成
                // 手动向上遍历 + class 属性比对。
                function _closestTag(el, cls) {{
                    if (el && typeof el.closest === 'function') {{
                        try {{
                            var hit = el.closest('.' + cls);
                            if (hit) return hit;
                        }} catch (e) {{}}
                    }}
                    var node = el, guard = 0;
                    while (node && guard++ < 64) {{
                        if (node.nodeType === 1) {{
                            var c = node.getAttribute ? node.getAttribute('class') : null;
                            if (c) {{
                                var parts = String(c).split(' ');
                                for (var i = 0; i < parts.length; i++) {{
                                    if (parts[i] === cls) return node;
                                }}
                            }}
                        }}
                        node = node.parentNode;
                    }}
                    return null;
                }}
                document.addEventListener('click', e => {{
                    const sep = e.target.closest('#tool-separator');
                    if (sep) {{
                        _toggleToolSection(sep, e);
                        return;
                    }}
                    const btn = e.target.closest('button[data-action]');
                    if (btn) {{
                        const act = btn.getAttribute('data-action');
                        const b64 = btn.getAttribute('data-copy');
                        const lang = btn.getAttribute('data-lang') || '';
                        if (act === 'copy') try {{ navigator.clipboard.writeText(atob(b64)); }} catch(e) {{}}
                        console.log('pywebview_action:' + act + ':' + b64 + ':' + lang);
                        return;
                    }}
                    const summary = e.target.closest('.cm-collapsible__summary');
                    if (summary) {{
                        const block = summary.closest('.cm-collapsible');
                        if (block) {{
                            // 动画开始前暂停高度报告
                            startCollapsibleAnimation();
                            animateCollapsible(block, block.dataset.expanded !== 'true');
                        }}
                        return;
                    }}
                    const tag = _closestTag(e.target, 'context-tag');
                    if (tag) {{
                        var tagType = tag.getAttribute('data-type') || tag.getAttribute('data-action') || '';
                        var sessionId = tag.getAttribute('data-session-id') || '';
                        var tagContent = sessionId || tag.getAttribute('data-content') || tag.getAttribute('data-title') || '';
                        e.stopPropagation();
                        e.preventDefault();
                        console.log('pywebview_action:context|||' + tagContent + '|||' + tagType);
                        return;
                    }}
                    // 图片点击 → 内置预览（可滚轮缩放）
                    // 原为 'pywebview_action:open_url:' 直接交给系统默认程序打开：
                    // 跳出应用、体验割裂；且 data:/qrc: 的 src 交给 openUrl 后
                    // 实际无响应。改为内置预览，无法预览时由宿主回退 openUrl。
                    const img = e.target.closest('#content-placeholder img');
                    if (img) {{
                        e.stopPropagation();
                        e.preventDefault();
                        console.log('pywebview_action:preview_image:' + img.src);
                        return;
                    }}
                    const link = e.target.closest('a');
                    if (link) {{
                        // file:// 链接：交给 QWebEnginePage.acceptNavigationRequest 处理（系统打开）
                        if (link.href && link.href.startsWith('file://')) {{
                            return;  // 不拦截，触发默认 navigation
                        }}
                        console.log('pywebview_action:link_found:' + link.href);
                    }}
                    if (link && link.href && !link.href.startsWith('file://')) {{
                        e.preventDefault();
                        console.log('pywebview_action:open_url:' + link.href);
                    }}
                }});
                document.addEventListener('DOMContentLoaded', () => {{
                    console.log('pywebview_ready');
                    // 历史会话折叠状态由 _on_js_ready 中的 Python 侧设置
                    // （避免 DOMContentLoaded 时 window._isHistoryCard 尚未就绪的时序问题）。
                    // 此处仅恢复流式会话的 sessionStorage 折叠偏好。
                    try {{
                        var _stored = sessionStorage.getItem('_toolSectionCollapsed');
                        if (_stored === '1') {{
                            var _ts = document.getElementById('tool-section');
                            var _sep = document.getElementById('tool-separator');
                            if (_ts) _ts.setAttribute('data-collapsed', 'true');
                            if (_sep) _sep.setAttribute('aria-expanded', 'false');
                        }}
                    }} catch(_e) {{}}
                    // 无障碍：键盘 Enter / Space 触发折叠切换（WCAG button 模式）
                    var _sepEl = document.getElementById('tool-separator');
                    if (_sepEl) {{
                        _sepEl.addEventListener('keydown', function(kEvt) {{
                            if (kEvt.key === 'Enter' || kEvt.key === ' ') {{
                                _toggleToolSection(_sepEl, kEvt);
                            }}
                        }});
                    }}
                    reportHeight();
                    // 使用防抖的 ResizeObserver，避免频繁触发高度更新
                    let resizeTimeout = null;
                    new ResizeObserver(() => {{
                        // 动画期间跳过高度报告
                        if (_collapsibleHeightReporting) return;
                        if (resizeTimeout) clearTimeout(resizeTimeout);
                        resizeTimeout = setTimeout(() => requestAnimationFrame(reportHeight), 50);
                    }}).observe(document.body);

                    // 简洁模式：工具区不再设动态 max-height，
                    // 内容完全展开，由父级卡片统一处理滚动。
                }});
                window.addEventListener('load', () => {{
                    reportHeight();
                }});
                window.addEventListener('webglcontextlost', (e) => {{
                    e.preventDefault();
                    console.log('pywebview_action:context_lost');
                }}, false);
                window.addEventListener('webglcontextrestored', () => {{
                    console.log('pywebview_ready');
                    reportHeight();
                }}, false);
                window.pywebview = {{ reportHeight: reportHeight }};

                // ===== 图表工具栏：echarts / mermaid 放大查看 + 3x PNG 导出 =====
                // 图表主题三件套（明暗判定 / 导出底色 / 图标目录）。
                // 原先是骨架构建期常量：refresh_theme 只注入 CSS 变量、不重 setHtml，
                // 导致切主题后已存在卡片的 echarts 明暗、PNG 导出底色、工具栏图标
                // 永久停留在建卡时的值。改为挂 window 的运行时可变量，
                // refresh_theme 调 window._applyChartTheme() 同步。
                window._applyChartTheme = function (isDark) {{
                    window._CHART_IS_DARK = !!isDark;
                    window._CHART_BG = isDark ? '#1B1E24' : '#FFFFFF';
                    // 沙箱 widget 拿不到宿主 CSS 变量，主题切换后必须重推一次
                    if (typeof window._refreshWidgetVars === 'function') window._refreshWidgetVars();
                    // icon 目录与按钮底色同源（_CHART_IS_DARK），避免主题切换时
                    // prefix 缓存滞后致白底白 icon
                    window._ICON_BASE = isDark ? 'qrc:/icons' : 'qrc:/icons_light';
                }};
                window._applyChartTheme({str(not _is_light).lower()});
                function _b64EncodeUtf8(str) {{
                    return btoa(unescape(encodeURIComponent(str)));
                }}
                function _b64DecodeUtf8(b64) {{
                    return decodeURIComponent(escape(atob(b64)));
                }}
                function _emitChartPng(dataUrl) {{
                    var b64 = (dataUrl || '').split(',', 2)[1] || '';
                    if (!b64 || b64.length > 8 * 1024 * 1024) {{ console.error('[chart] png too large or empty'); return; }}
                    console.log('pywebview_action:save_chart_png:' + _b64EncodeUtf8('chart') + ':' + b64);
                }}
                function _svgIntrinsicSize(svg) {{
                    // mermaid 输出 svg 带 width="100%"：parseFloat 会得 100 导致导出窄条，必须排除百分比、viewBox 优先
                    var vb = svg.viewBox && svg.viewBox.baseVal;
                    var num = function (v) {{ var n = parseFloat(v); return (n && String(v).indexOf('%') === -1) ? n : 0; }};
                    var w = (vb && vb.width) || num(svg.getAttribute('width')) || 800;
                    var h = (vb && vb.height) || num(svg.getAttribute('height')) || 600;
                    return [w, h];
                }}
                function _exportMermaidSvgPng(svg, scale) {{
                    if (!svg) return;
                    var serialized = new XMLSerializer().serializeToString(svg);
                    var wh = _svgIntrinsicSize(svg);
                    var w = wh[0], h = wh[1];
                    var img = new Image();
                    img.onload = function () {{
                        var canvas = document.createElement('canvas');
                        canvas.width = Math.round(w * scale);
                        canvas.height = Math.round(h * scale);
                        var ctx = canvas.getContext('2d');
                        ctx.fillStyle = _CHART_BG;
                        ctx.fillRect(0, 0, canvas.width, canvas.height);
                        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                        _emitChartPng(canvas.toDataURL('image/png'));
                    }};
                    img.src = 'data:image/svg+xml;base64,' + _b64EncodeUtf8(serialized);
                }}
                window._attachChartToolbar = function (el, type) {{
                    if (!el || el._toolbarAttached) return;
                    el._toolbarAttached = true;
                    // 防御兜底：absolute 定位基准必须是容器自身（历史 CSS 曾被字面 ''' 破坏）
                    if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
                    var bar = document.createElement('div');
                    bar.className = 'chart-toolbar';
                    var btnExpand = document.createElement('button');
                    btnExpand.setAttribute('data-tooltip', '放大查看');
                    btnExpand.innerHTML = '<img src="' + _ICON_BASE + '/最大化.svg" />';
                    var btnExport = document.createElement('button');
                    btnExport.setAttribute('data-tooltip', (type === 'svg' || type === 'html') ? '保存源文件' : '导出 PNG（3x）');
                    btnExport.innerHTML = '<img src="' + _ICON_BASE + '/导入.svg" />';
                    bar.appendChild(btnExpand);
                    bar.appendChild(btnExport);
                    el.appendChild(bar);
                    btnExpand.addEventListener('click', function (ev) {{
                        ev.stopPropagation();
                        try {{
                            if (type === 'echarts' && el._chartInstance) {{
                                var opt = JSON.stringify(el._chartInstance.getOption());
                                console.log('pywebview_action:chart_expand:echarts:' + _b64EncodeUtf8(opt));
                            }} else if (type === 'mermaid') {{
                                var svg = el.querySelector('svg');
                                if (!svg) return;
                                console.log('pywebview_action:chart_expand:mermaid:' + _b64EncodeUtf8(svg.outerHTML));
                            }} else if (type === 'svg') {{
                                var node = (el.tagName === 'svg' || el.tagName === 'SVG') ? el : el.querySelector('svg');
                                if (!node) return;
                                console.log('pywebview_action:chart_expand:svg:' + _b64EncodeUtf8(node.outerHTML));
                            }} else if (type === 'html') {{
                                // html widget（```html fence 净化产物）：放大查看走
                                // chart_viewer_card 的独立渲染页。
                                // 取 innerHTML 前先摘除浮动工具栏：查看页无
                                // .chart-toolbar 尺寸约束，按钮 qrc svg 会被渲染成巨大图标
                                var _bar = el.querySelector(':scope > .chart-toolbar');
                                if (_bar) el.removeChild(_bar);
                                console.log('pywebview_action:chart_expand:html:' + _b64EncodeUtf8(el.innerHTML));
                                if (_bar) el.appendChild(_bar);
                            }}
                        }} catch (e) {{ console.error('[chart] expand failed:', e); }}
                    }});
                    btnExport.addEventListener('click', function (ev) {{
                        ev.stopPropagation();
                        try {{
                            if (type === 'echarts' && el._chartInstance) {{
                                el._chartInstance.resize();  // 防实例内部宽度过期导致导出畸形
                                _emitChartPng(el._chartInstance.getDataURL({{ type: 'png', pixelRatio: 3, backgroundColor: _CHART_BG }}));
                            }} else if (type === 'mermaid') {{
                                _exportMermaidSvgPng(el.querySelector('svg'), 3);
                            }} else if (type === 'svg') {{
                                var node = (el.tagName === 'svg' || el.tagName === 'SVG') ? el : el.querySelector('svg');
                                if (!node) return;
                                console.log('pywebview_action:save_widget_file:svg:' + _b64EncodeUtf8(node.outerHTML));
                            }} else if (type === 'html') {{
                                // 导出源文件同样摘除工具栏，还原纯净净化产物
                                var _bar = el.querySelector(':scope > .chart-toolbar');
                                if (_bar) el.removeChild(_bar);
                                console.log('pywebview_action:save_widget_file:html:' + _b64EncodeUtf8(el.innerHTML));
                                if (_bar) el.appendChild(_bar);
                            }}
                        }} catch (e) {{ console.error('[chart] export failed:', e); }}
                    }});
                }};

                // 工具差异对比请求函数
                window._requestToolDiff = function(toolCallId) {{
                    console.log('pywebview_action:tool_diff:' + toolCallId);
                }};

                // ===== SVG widget 工具栏挂载 =====
                // 渲染后扫描正文的自由 svg（排除 mermaid/echarts 内部）包 wrapper 挂工具栏。
                // 目标形态两种：① 顶层裸 svg（```svg 围栏透传）；② svg-only 容器（内联 svg
                // 经 _protect_inline_svg_blocks 包 <div> / markdown 段落包 <p>，svg 沉一层，
                // 只扫顶层会漏挂）。尺寸阈值滤掉装饰小图标（欢迎卡图标、行内 icon）。
                // svg._widgetToolbar 防重挂（innerHTML 全量重建后 DOM 全新，标记自然失效，重扫重挂）。
                window.renderWidgetToolbars = function() {{
                    var root = document.getElementById('content-placeholder');
                    if (!root) return;
                    var children = root.children;
                    for (var i = 0; i < children.length; i++) {{
                        var el = children[i];
                        var svg = null;
                        if (el.tagName === 'svg' || el.tagName === 'SVG') {{
                            svg = el;
                        }} else if (el.tagName === 'DIV' || el.tagName === 'P') {{
                            // svg-only 容器：唯一子节点是 svg 且内部无图表/代码结构
                            if (!el.querySelector('.mermaid-block, .echarts-container, .katex-block, .code-container') && el.children.length === 1) {{
                                var only = el.children[0];
                                if (only.tagName === 'svg' || only.tagName === 'SVG') svg = only;
                            }}
                        }}
                        if (!svg) {{
                            // HTML widget（```html fence 净化产物）：与自由 svg 同等待遇，
                            // 挂同一套工具栏（放大查看 + 保存源文件）。
                            // 尺寸阈值滤掉小装饰块，_widgetToolbar 防重挂。
                            if (el.classList && el.classList.contains('html-widget') && !el._widgetToolbar) {{
                                if (el.clientWidth < 200 || el.clientHeight < 100) continue;
                                el._widgetToolbar = true;
                                el.classList.add('widget-toolbar-host');
                                window._attachChartToolbar(el, 'html');
                            }}
                            continue;
                        }}
                        if (svg.closest('.mermaid-block') || svg.closest('.echarts-container')) continue;
                        if (svg._widgetToolbar) continue;
                        if (svg.clientWidth < 200 || svg.clientHeight < 100) continue;  // 装饰小图标
                        svg._widgetToolbar = true;
                        var wrap = document.createElement('div');
                        wrap.className = 'svg-widget-host widget-toolbar-host';
                        wrap.style.cssText = 'position:relative;margin:12px 0;';
                        el.parentNode.replaceChild(wrap, el);  // svg-only 容器整个替换，避免 div>wrap 双层嵌套
                        wrap.appendChild(svg);
                        window._attachChartToolbar(wrap, 'svg');
                    }}
                }};

                // ===== 插件 fence：assets 按需注入 + 权限桥 =====
                window.__fenceLoaded = {{}};
                function _scanFenceLangs() {{
                    var out = [], nodes = document.querySelectorAll('[data-fence-renderer]');
                    for (var i = 0; i < nodes.length; i++) {{
                        var l = nodes[i].getAttribute('data-fence-renderer');
                        if (l && out.indexOf(l) < 0) out.push(l);
                    }}
                    return out;
                }}
                function _ensureFenceAssets(langs, cb) {{
                    var list = [], i;
                    for (i = 0; i < langs.length; i++) {{
                        var spec = window.__fenceAssets[langs[i]];
                        if (spec && !window.__fenceLoaded[langs[i]]) list.push([langs[i], spec]);
                    }}
                    if (!list.length) {{ cb(); return; }}
                    var pending = 0, fired = false;
                    function _tick() {{
                        pending--;
                        if (pending <= 0 && !fired) {{ fired = true; cb(); }}
                    }}
                    for (i = 0; i < list.length; i++) {{
                        (function (lang, spec) {{
                            window.__fenceLoaded[lang] = true;
                            if (spec.css) {{
                                var link = document.createElement('link');
                                link.rel = 'stylesheet';
                                link.href = spec.css;
                                document.head.appendChild(link);
                            }}
                            if (spec.js) {{
                                pending++;
                                var s = document.createElement('script');
                                s.src = spec.js;
                                s.onload = _tick;
                                s.onerror = function () {{
                                    console.error('[fence] load failed: ' + spec.js);
                                    _tick();
                                }};
                                document.head.appendChild(s);
                            }}
                        }})(list[i][0], list[i][1]);
                    }}
                    if (pending === 0) cb();
                }}
                // 桥：权限声明制，未声明的方法恒为 undefined。插件 JS 只存活于本
                // 卡片的 QWebEngineView，跨卡 / 跨窗 / 进主进程均不可达。
                window.__drifoxBridge = {{}};
                window._syncFenceBridge = function () {{
                    var langs = _scanFenceLangs(), granted = {{}}, i, j;
                    for (i = 0; i < langs.length; i++) {{
                        var ps = window.__fenceBridgePerms[langs[i]] || [];
                        for (j = 0; j < ps.length; j++) granted[ps[j]] = true;
                    }}
                    var B = window.__drifoxBridge;
                    B.getTheme = granted.theme ? function () {{
                        return {{
                            isDark: !!window._CHART_IS_DARK,
                            chartBg: window._CHART_BG,
                            textColor: getComputedStyle(document.body).color
                        }};
                    }} : undefined;
                    B.sendPrompt = granted.sendPrompt ? function (text) {{
                        console.log('pywebview_action:fence_prompt:' + _b64EncodeUtf8(String(text)));
                    }} : undefined;
                    B.storage = granted.storage ? {{
                        get: function (k) {{
                            try {{
                                return JSON.parse(sessionStorage.getItem('__fence_' + k) || 'null');
                            }} catch (e) {{ return null; }}
                        }},
                        set: function (k, v) {{
                            try {{
                                sessionStorage.setItem('__fence_' + k, JSON.stringify(v));
                            }} catch (e) {{}}
                        }}
                    }} : undefined;
                }};
                function _syncFenceBridgeSafe() {{
                    try {{ window._syncFenceBridge(); }} catch (e) {{
                        console.error('[fence] bridge sync:', e);
                    }}
                }}
                window._runFenceAssets = function () {{
                    var _flangs = _scanFenceLangs();
                    // 桥必须先于 assets 装配：插件脚本末尾常有"首帧兜底"的主动初始化
                    // （一执行就跑），那时若桥还是空的，插件会把"未授权"状态写死在
                    // 节点上，之后再装配也救不回来（幂等标记已打）。
                    _syncFenceBridgeSafe();
                    _ensureFenceAssets(_flangs, function () {{
                        // 加载完成后再装配一次，覆盖注入期间新增的 fence lang
                        _syncFenceBridgeSafe();
                        // 插件初始化入口约定：插件脚本挂 window.__drifoxFenceInit[lang]，
                        // 宿主在 assets 就绪后调用。会被反复调用（每次渲染），
                        // 插件必须保证幂等 —— 通常用 data-* 标记已处理的节点。
                        try {{
                            var _inits = window.__drifoxFenceInit;
                            if (_inits) {{
                                for (var _fi = 0; _fi < _flangs.length; _fi++) {{
                                    var _fn = _inits[_flangs[_fi]];
                                    if (typeof _fn === 'function') _fn();
                                }}
                            }}
                        }} catch (e) {{ console.error('[fence] init:', e); }}
                        if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                    }});
                }};

                // ===== 内置可交互 widget 围栏（```widget）=====
                // 内容不内联进主文档，而是装进 sandbox="allow-scripts" 的 iframe
                // （不授予 allow-same-origin → 源为 opaque），内部再叠 CSP。
                // 宿主开了 file:// 互访 + 远程访问，脚本内联即任意本地文件读取 +
                // 外传；沙箱把这条链切断。宿主能力经 postMessage 白名单暴露。
                var _WIDGET_HOST_VARS = ['--bg', '--panel', '--panel-elevated', '--panel-soft', '--border', '--border-strong', '--text', '--text-secondary', '--text-muted', '--accent', '--accent-warm', '--code-bg', '--code-toolbar', '--code-border', '--success', '--danger', '--accent-text', '--accent-soft', '--accent-soft-strong', '--accent-border-weak', '--accent-glow', '--row-alt', '--row-hover', '--row-header', '--r-xs', '--r-sm', '--r-md', '--r-lg', '--r-xl', '--r-pill'];
                window.__WIDGET_GRANTS = {_widget_grants_js};
                function _widgetHostVars() {{
                    var cs = getComputedStyle(document.documentElement), out = {{}}, i;
                    for (i = 0; i < _WIDGET_HOST_VARS.length; i++) {{
                        var v = cs.getPropertyValue(_WIDGET_HOST_VARS[i]);
                        if (v) out[_WIDGET_HOST_VARS[i]] = String(v).trim();
                    }}
                    out['--font-family'] = getComputedStyle(document.body).fontFamily;
                    return out;
                }}
                function _widgetPushVars(win) {{
                    if (!win) return;
                    try {{ win.postMessage({{ __drifoxWidgetHost: 1, t: 'vars', v: _widgetHostVars() }}, '*'); }} catch (e) {{}}
                }}
                // 主题切换时重推变量：沙箱拿不到宿主 CSS 变量，只能靠这里同步
                window._refreshWidgetVars = function () {{
                    var fs = document.querySelectorAll('.drifox-widget iframe');
                    for (var i = 0; i < fs.length; i++) _widgetPushVars(fs[i].contentWindow);
                }};
                function _widgetStripShell(h) {{
                    return String(h)
                        .replace(/<!doctype[^>]*>/gi, '')
                        .replace(/<[/]?html[^>]*>/gi, '')
                        .replace(/<[/]?head[^>]*>/gi, '')
                        .replace(/<[/]?body[^>]*>/gi, '');
                }}
                function _widgetBuildDoc(srcHtml) {{
                    return '<!DOCTYPE html><html><head><meta charset="utf-8">'
                        + '<meta http-equiv="Content-Security-Policy" content=' + _WIDGET_CSP_JS + '>'
                        + '<style>' + _WIDGET_BASE_CSS_JS + '</style></head><body>'
                        + '<script>' + _WIDGET_PRELUDE_JS + '</scr' + 'ipt>'
                        + _widgetStripShell(srcHtml)
                        + '</body></html>';
                }}
                function _initOneWidget(node) {{
                    if (!node || node.getAttribute('data-widget-ready') === '1') return;
                    var b64 = node.getAttribute('data-widget-src');
                    if (!b64) return;
                    var src;
                    try {{ src = _b64DecodeUtf8(b64); }} catch (e) {{ return; }}
                    if (!src) return;
                    node.setAttribute('data-widget-ready', '1');
                    node.innerHTML = '';
                    var f = document.createElement('iframe');
                    f.setAttribute('sandbox', 'allow-scripts');
                    f.setAttribute('scrolling', 'no');
                    f.setAttribute('frameborder', '0');
                    f.style.cssText = 'width:100%;height:120px;border:0;display:block;overflow:hidden;background:transparent;';
                    f.onload = function () {{ _widgetPushVars(f.contentWindow); }};
                    node.appendChild(f);
                    try {{ f.setAttribute('srcdoc', _widgetBuildDoc(src)); }} catch (e) {{}}
                }}
                window._initWidgets = function () {{
                    var nodes = document.querySelectorAll('.drifox-widget[data-widget-src]');
                    for (var i = 0; i < nodes.length; i++) _initOneWidget(nodes[i]);
                }};
                window.addEventListener('message', function (e) {{
                    var d = e && e.data;
                    if (!d || !d.__drifoxWidget) return;
                    var msg = d.p || {{}};
                    var fs = document.querySelectorAll('.drifox-widget iframe'), i, f = null;
                    for (i = 0; i < fs.length; i++) {{
                        if (fs[i].contentWindow === e.source) {{ f = fs[i]; break; }}
                    }}
                    if (!f) return;
                    if (msg.t === 'h') {{
                        var h = parseInt(msg.v, 10) || 0;
                        if (h > 0) f.style.height = Math.max(48, Math.min(1600, h)) + 'px';
                        if (typeof _autoScrollAfterAsyncRender === 'function') _autoScrollAfterAsyncRender();
                        if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                        return;
                    }}
                    if (msg.t !== 'call') return;
                    var G = window.__WIDGET_GRANTS || {{}};
                    function _reply(v) {{
                        try {{ f.contentWindow.postMessage({{ __drifoxWidgetHost: 1, t: 'reply', id: msg.id, v: v }}, '*'); }} catch (err) {{}}
                    }}
                    if (msg.m === 'sendPrompt' && G.sendPrompt) {{
                        console.log('pywebview_action:fence_prompt:' + _b64EncodeUtf8(String(msg.a || '')));
                        _reply(true);
                        return;
                    }}
                    if (msg.m === 'getTheme' && G.theme) {{
                        _reply({{ isDark: !!window._CHART_IS_DARK, chartBg: window._CHART_BG, textColor: getComputedStyle(document.body).color, vars: _widgetHostVars() }});
                        return;
                    }}
                    if (msg.m === 'storage.get' && G.storage) {{
                        try {{ _reply(JSON.parse(sessionStorage.getItem('__fence_' + msg.a) || 'null')); }} catch (err) {{ _reply(null); }}
                        return;
                    }}
                    if (msg.m === 'storage.set' && G.storage) {{
                        try {{ sessionStorage.setItem('__fence_' + msg.a[0], JSON.stringify(msg.a[1])); }} catch (err) {{}}
                        _reply(true);
                        return;
                    }}
                    _reply(null);
                }});

                // 子智能体日志查看请求函数
                window._requestSubAgentLog = function(taskIds) {{
                    console.log('pywebview_action:subagent_log:' + taskIds);
                }};

                // ===== 用户滚动跟踪：判断用户是否主动滚动卡片内部内容 =====
                // 🐛 修复：当卡片内容超出 MAX_HEIGHT 时，body 出现内部滚动条。
                // 初始状态 scrollTop=0 导致 wasAtBottom 判断失败，auto-scroll 不触发。
                // 跟踪用户主动滚动行为，未滚动时强制 auto-scroll 到底部。
                // 初始即「跟随底部」：未滚动时自动滚底；一旦用户上滚离开，
                // 由下方 scroll 监听按位置判定改为停止跟随，滚回底部附近自动恢复。
                window._userScrolledWithin = false;
                window._suppressScrollEvent = false;
                window._prevScrollTop = 0;  // 历史基线，已不再用于判定
                document.body.addEventListener('scroll', function() {{
                    var _st = document.body.scrollTop;
                    // 即使被抑制也保持 _prevScrollTop 同步，避免后续用户滚动时
                    // _prevScrollTop 陈旧（该字段仅作历史基线，不再用于任何判定）。
                    if (window._suppressScrollEvent) {{
                        window._prevScrollTop = _st;
                        return;
                    }}
                    window._prevScrollTop = _st;
                    // 🔧 核心修复：用「位置判定」取代脆弱的 delta 阈值。
                    // 靠近底部(_scrollThreshold 内) = 跟随态(_userScrolledWithin=false)，
                    // 离开底部 = 用户主动上滚(_userScrolledWithin=true)。
                    // 程序性滚底同样落在底部 → 自动恢复跟随；用户滚轮上滚 → 立即停止
                    // 跟随；滚回底部附近 → 恢复跟随。彻底消除 delta 竞态导致的
                    // “输出跳到莫名其妙位置 / 滚轮被永久锁死”问题。
                    var _nearBottom = Math.abs(document.body.scrollHeight - _st - document.body.clientHeight) < {
            AUTO_SCROLL_THRESHOLD
        };
                    window._userScrolledWithin = !_nearBottom;
                }});
                // ======================================================

                // ===== JS驱动的蛇形思考动画（替代CSS animation）=====
                // 使用 requestAnimationFrame 持续更新 stroke-dashoffset，
                // 即使 updateContent 重建DOM，新SVG元素在下一帧立即获得正确偏移，
                // 不再因 CSS animation 重启而导致视觉跳跃。
                let _snakeStartTime = null;
                let _snakeRafId = null;
                function _animateThinkSnake() {{
                    const nodes = document.querySelectorAll('.think-snake-arc');
                    if (!nodes.length) {{
                        // 蛇形图标已从 DOM 移除（思考结束 / 内容重建）：停掉 rAF。
                        // 旧实现在此仍无条件 requestAnimationFrame，形成**永不停止**
                        // 的 60fps 空转 + 每帧全表扫描，思考结束后白烧主线程。
                        // 元素重新出现时由下方 MutationObserver 自动唤醒。
                        _snakeRafId = null;
                        _snakeStartTime = null;
                        return;
                    }}
                    if (_snakeStartTime === null) _snakeStartTime = performance.now();
                    const elapsed = performance.now() - _snakeStartTime;
                    // 周期 1.5s，完整一圈对应 stroke-dashoffset: 0→-50.265（周长 2π×8 ≈ 50.265）
                    nodes.forEach(el => {{
                        let extraDelay = 0;
                        if (el.classList.contains('think-snake-head')) extraDelay = 350;
                        else if (el.classList.contains('think-snake-body')) extraDelay = 180;
                        const phase = (elapsed + extraDelay) % 1500;
                        const offset = -(phase / 1500) * 50.265;
                        el.setAttribute('stroke-dashoffset', offset);
                    }});
                    _snakeRafId = requestAnimationFrame(_animateThinkSnake);
                }}
                function _ensureThinkSnake() {{
                    // 幂等：rAF 已在跑时立即返回（成本 = 一次空判断）
                    if (_snakeRafId === null && document.querySelector('.think-snake-arc')) {{
                        _animateThinkSnake();
                    }}
                }}
                window._ensureThinkSnake = _ensureThinkSnake;
                new MutationObserver(_ensureThinkSnake).observe(document.body, {{ childList: true, subtree: true }});
                _ensureThinkSnake();

                // ===== 工具区（#tool-content）自动滚底 =====
                // 当工具/思考区有新内容时，自动滚动到底部，让用户始终看到最新状态。
                // 用户主动上滚后不再打扰（_userScrolledUp），滚回底部附近自动恢复跟随。
                // [#12 R2] v37(45463d9b) 误删工具区滚动保护链，从 77882330 基线恢复；
                // :4142/:4400/:6376 三处残留调用由此复活。todo-content 同款监听不恢复
                // （v37 todo 已迁移内嵌，不回摆）。
                function _scrollToolContentToBottom() {{
                    var tc = document.getElementById('tool-content');
                    if (!tc) return;
                    // 用户主动向上滚动了工具区则不自动滚底
                    if (tc._userScrolledUp) return;
                    // 抑制本次程序滚底触发的 scroll 事件：异步 scroll 到达时
                    // scrollHeight 可能已增长（流式新块加入），atBottom 误判 false
                    // 会错误置位 _userScrolledUp 导致跟随中断。
                    _progScroll(tc, tc.scrollHeight);
                }}
                // 工具区滚动跟踪：用户主动向上滚动时标记，滚到底部时取消标记
                document.getElementById('tool-content')?.addEventListener('scroll', function() {{
                    var tc = this;
                    // 🐛 修复（流式滚动位置重置）：updateContent / save-restore 的 DOM
                    // 操作窗口内 scrollTop 被钳制产生的程序性 scroll 事件（异步派发
                    // 到达时 _suppressScrollEvent 已复位）不得误判为用户滚动——否则
                    // 钳制位置恰在底部附近时 _userScrolledUp 被误复位 → 跟随重新激活
                    // → 后续每次流式更新强制拉底，用户阅读位置反复丢失。与
                    // #content-placeholder 监听的 _suppressScrollEvent 抑制对称。
                    if (window._suppressScrollEvent) return;
                    // 程序性滚底（_scrollToolContentToBottom / innerHTML 重建）不视为用户行为
                    if (tc._progDepth > 0) {{ tc._progDepth--; return; }}
                    var atBottom = Math.abs(tc.scrollHeight - tc.scrollTop - tc.clientHeight) < 30;
                    tc._userScrolledUp = !atBottom;
                    if (atBottom) tc._userScrolledUp = false;
                }});
                // 🐛 修复（流式滚动位置重置）：wheel 事件同步标记上滚意图——scroll
                // 事件异步派发，与流式 JS（_scrollToolContentToBottom）存在竞争窗口：
                // 用户滚轮后 scroll 未派发，流式 JS 判 _userScrolledUp=false 抢先拉底
                // 覆盖阅读位置。对齐 #content-placeholder 的 wheel 修复模式。
                // 用户滚动意图绑定（wheel / 触摸 / 键盘），语义见 _bindUserScrollIntent
                // （定义在 card_render_core 骨架，v37 未删，仅调用侧被误删）。
                _bindUserScrollIntent(document.getElementById('tool-content'));

                {_STREAMING_DOCK_JS}
                {_TYPEWRITER_JS}
                {_PREVIEW_TYPEWRITER_JS}
                {_FLIP_JS}

                // ===== 流式工具块：移除超时自动标记 ====
                // 原 _cleanupStuckTools 会在 30 秒后标记工具为"超时未返回结果"，
                // 但工具可能仍在执行中，不应急于标记失败。硬等即可。

                // ===== 深度思考轮播提示（减少等待焦虑，类似 CodeBuddy 设计理念）=====
                // 当 .think-streaming[data-streaming="true"] 存在时，定时轮换显示
                // 说明信息，让用户在等待期间能获取有用提示，而不是只盯着转圈。
                const _thinkTips = [
                    "正在深度思考中...",
                    "分析上下文关联...",
                    "检索相关知识库...",
                    "正在综合推理...",
                    "组织回答结构...",
                    "即将输出结果...",
                    "梳理关键信息...",
                    "对比多个方案...",
                    "校验逻辑完整性...",
                    "回溯历史消息...",
                    "推理最佳路径...",
                    "整合分析结果...",
                    "审查边缘场景...",
                    "串联上下文线索...",
                    "构建最终输出...",
                    "准备呈现答案..."
                ];
                let _tipIndex = 0;
                let _tipTimer = null;

                function _startTipRotation() {{
                    _stopTipRotation();
                    // 首次启动时给文字 span 加上脉冲渐变色 class
                    const el0 = document.querySelector('.think-streaming[data-streaming="true"]');
                    if (el0) {{
                        const s0 = el0.querySelector('span > span:last-child');
                        if (s0) s0.classList.add('think-streaming-tip');
                    }}
                    _tipTimer = setInterval(() => {{
                        const el = document.querySelector('.think-streaming[data-streaming="true"]');
                        if (!el) {{ _stopTipRotation(); return; }}
                        // 🐛 修复：不能用 span:last-child — 外层 span（唯一子元素）也会命中，
                        // 导致 textContent 替换时清掉 spinner SVG。改为精确选择内层文字 span。
                        const tipSpan = el.querySelector('span > span:last-child');
                        if (tipSpan) {{
                            _tipIndex = (_tipIndex + 1) % _thinkTips.length;
                            tipSpan.textContent = _thinkTips[_tipIndex];
                        }}
                    }}, 3500);
                }}

                function _stopTipRotation() {{
                    if (_tipTimer) {{
                        clearInterval(_tipTimer);
                        _tipTimer = null;
                    }}
                }}

                // 通过 MutationObserver 监听 content-placeholder 变化，
                // 自动启停轮播（兼容 updateContent 全量重建 DOM 的场景）。
                const _tipObserver = new MutationObserver(() => {{
                    const hasStreaming = !!document.querySelector('.think-streaming[data-streaming="true"]');
                    if (hasStreaming && !_tipTimer) {{
                        _startTipRotation();
                    }} else if (!hasStreaming && _tipTimer) {{
                        _stopTipRotation();
                    }}
                }});
                const _tipTarget = document.getElementById('content-placeholder');
                if (_tipTarget) {{
                    _tipObserver.observe(_tipTarget, {{ childList: true, subtree: true }});
                }}
                // 也监听 tool-content（思考块被移动到此处）
                const _tipToolContent = document.getElementById('tool-content');
                if (_tipToolContent) {{
                    _tipObserver.observe(_tipToolContent, {{ childList: true, subtree: true }});
                }}
            </script>
        </body>
        </html>
        """
        # 存入全局骨架缓存，避免后续卡片重复构造同一 HTML 模板
        _skeleton_cache[cache_key] = html
        _skeleton_cache.move_to_end(cache_key)
        if len(_skeleton_cache) > _SKELETON_CACHE_MAX:
            _skeleton_cache.popitem(last=False)  # LRU：淘汰最久未用
        # 以项目根目录为基础 URL，使相对路径图片（如 images/xxx.png）可正确解析
        self.setHtml(html, QUrl.fromLocalFile(_PROJECT_ROOT + "/"))

    # ========== 差量渲染常量 ==========
    # 安全兜底渲染间隔（ms）：无自然边界到达时强制全量渲染
    # 🔧 300ms 基础值，实际值根据流式速度在 150-500ms 间自适应
    _SAFETY_RENDER_INTERVAL = 300
    # 自适应安全渲染间隔参数：
    # - 快速流式（chunk 间隔 < 200ms）：用 150ms，响应更及时
    # - 慢速流式（chunk 间隔 > 500ms）：用 500ms，减少冗余渲染
    # - 默认：300ms
    _ADAPTIVE_INTERVAL_FAST = 150
    _ADAPTIVE_INTERVAL_SLOW = 500
    _ADAPTIVE_THRESHOLD_FAST = 200  # ms
    _ADAPTIVE_THRESHOLD_SLOW = 500  # ms
    # [PERF] 软边界（句号结尾）的**最小渲染间隔**（ms）—— 不是固定延迟，而是
    # 「距上次渲染不足此窗口才合并，否则照旧即时渲染」。
    # 中文正文句号极密集（约每 15~40 字一个），密集流式下无脑 immediate 会让
    # 上方 150~500ms 的自适应节流形同虚设，退化为「每 chunk 一次 O(tail) 转换」
    # ——随消息长度呈 O(n²)，是「流式越到后面越卡」的主因。
    # 用最小间隔而非固定延迟，可在快速流式合并的同时保住慢速流式的即时观感。
    # [T28 L1] 40 → 100ms：40ms 窗口太窄，快速流式（chunk 间隔 ~20-50ms）下
    # 大部分软边界仍落在窗口外 → 仍逐句重渲染。100ms 覆盖典型 chunk 间隔的
    # 2-5 倍，合并生效且不拖慢慢速流式（慢速流式距上次渲染远超 100ms → 仍即时）。
    _SOFT_BOUNDARY_MERGE_MS = 100
    # 预编译代码块闭合检测
    _CLEAN_BOUNDARY_CODE_BLOCK_RE = re.compile(r"```[\s]*$")
    # 长内容**历史渲染**走线程池的字符阈值（仅用于非"流式结束"的渲染）。
    # ⚠️ 结束路径后面紧跟 _cleanup_render_cache()，它 `self._render_seq += 1`
    # 会把异步结果判为过期丢弃（详见 _perform_update 内注释）——所以结束态必须
    # 保持同步，历史加载没有这个紧随的 cleanup，异步是安全的。
    # 真机实测（2026-09-09）：历史卡 md 24k~37k 时主线程 render 占 40~120ms/张，
    # 一张张串行就是"加载大会话时一顿一顿"的来源。
    _ASYNC_HISTORY_RENDER_MIN_CHARS = 6000

    @staticmethod
    def _has_reached_clean_boundary(md_text: str) -> bool:
        """检测 markdown 文本是否在自然边界结束

        自然边界 = 段落结束 / think 块闭合 / 代码块闭合。
        在此边界做全量 HTML 渲染可得稳定结果，无需后续重算。

        Returns:
            True: 文本在自然边界结束，适合触发全量渲染
        """
        if not md_text:
            return False
        # 段落结束（双换行）：用原文本检测，因 rstrip 会移除尾部换行
        if md_text.endswith("\n\n"):
            return True
        # think 块 / 代码块闭合：用 rstrip 处理尾部空白
        stripped = md_text.rstrip()
        return stripped.endswith("</think>") or CodeWebViewer._CLEAN_BOUNDARY_CODE_BLOCK_RE.search(stripped) is not None

    @staticmethod
    def _has_reached_soft_boundary(md_text: str) -> bool:
        """检测 markdown 是否以句号类标点结尾（软边界，适合差量渲染）。

        大段中文正文常无 \n\n 空行，_has_reached_clean_boundary（硬边界）无法
        在流式期间及时触发渲染。句号结尾即视为「可增量闭合」的软边界，
        触发差量渲染（_extract_closed_segments 会按句号软边界切段），
        而不必等安全定时器兜底（300ms）——显著缩短纯文本停留时间。

        Returns:
            True: 文本以句号类标点结尾，适合触发差量渲染
        """
        if not md_text:
            return False
        stripped = md_text.rstrip()
        return bool(stripped) and stripped[-1] in _SENTENCE_END_CHARS

    def append_chunk(self, text: str):
        if not text:
            return

        self._markdown_text += text

        # [PERF] 更新流式速度跟踪
        now = time.monotonic_ns()
        if self._last_chunk_time > 0:
            elapsed_ms = (now - self._last_chunk_time) / 1_000_000
            if elapsed_ms < self._ADAPTIVE_THRESHOLD_FAST:
                self._current_adaptive_interval = self._ADAPTIVE_INTERVAL_FAST
            elif elapsed_ms > self._ADAPTIVE_THRESHOLD_SLOW:
                self._current_adaptive_interval = self._ADAPTIVE_INTERVAL_SLOW
            else:
                self._current_adaptive_interval = self._SAFETY_RENDER_INTERVAL
        self._last_chunk_time = now

        if not self._is_js_ready:
            return
        if self._streaming and len(text) > 3:
            # 差量渲染：仅在自然边界触发，否则靠增量文本 + 安全兜底
            # [PERF] 软边界（句号）不再走 immediate —— 中文句号密度极高，
            # 每命中一次就同步跑一遍 O(tail) 的 markdown 转换，随消息增长呈
            # O(n²)。改由 _schedule_render 内部的 90ms 短定时器合并（见
            # _SOFT_BOUNDARY_RENDER_DELAY_MS），硬边界仍保持 immediate。
            if self._has_reached_clean_boundary(self._markdown_text):
                self._schedule_render(immediate=True)
            else:
                self._schedule_render(immediate=False)
        else:
            self._schedule_render()

    def _append_text_incremental(self, text: str):
        """增量追加纯文本到 DOM（流式模式），让用户立即看到文字，不等全量渲染。

        在全量渲染（updateContent）到达前先推送纯文本内容，
        避免渲染延迟导致的"卡高先涨、文字后显"问题。
        """
        if not self._is_js_ready or not self.page():
            return
        # [PERF] 不可见期间跳过 DOM 注入。resize preview / 对话框穿透防护 / 切 tab
        # 隐藏期间，viewer 已被 MessageCard.hide() 或 WA_TranslucentBackground
        # 守卫关掉，但 runJavaScript 仍会执行 → Chromium 持续累积 DOM 节点 →
        # preview 退出或恢复可见时首帧 paint 阻塞整页重排，是流式 + resize 卡顿的
        # 根因之一。文本已在 _markdown_text 累积，恢复可见时由 _perform_update
        # 一次性渲染（_render_deferred 标记 + showEvent 补渲已覆盖此路径）。
        if not self.isVisible():
            return
        try:
            # 防御：过滤掉可能出现在正文 chunk 中的 <think> / </think> 标签
            # （防止增量显示标签，全量渲染会正确处理）
            text_clean = text.replace("<think>", "").replace("</think>", "")
            if not text_clean:
                return
            # 内存优化：超长 chunk 截断增量推送，避免单次 JS 调用传输过大数据
            # 全量渲染最终会提供完整格式化后的内容
            if len(text_clean) > 2000:
                text_clean = text_clean[:2000] + "\n\n..."
            js = f"""
            (function() {{
                // ── 追加逻辑注册为全局函数（只注册一次）──
                // 打字机揭示队列（window._twPush）需要按帧调用同一套"把一段文本
                // 接到正文尾部"的逻辑；注册成函数后队列与直接调用共用一份实现，
                // 避免两处逻辑漂移（段落/host 判定一旦分叉就会出现跳位）。
                if (typeof window._dfxAppendStreamText !== 'function') {{
                window._dfxAppendStreamText = function(text, skipReport) {{
                var c = document.getElementById('content-placeholder');
                if (!c || !text) return;
                // ── 尾部文本宿主定位 ──
                // rendered 尾部节点的 innerHTML 是 md.convert 产物（<p>/<ul>/<li>/<pre>…），
                // 直接把文本追加到节点本身会落在最后一个块级元素**之后**（另起一段）。
                // 下潜到最后一块级元素内，让文字接在已有文字后面**连续增长**。
                var _BLOCK_TAGS = {{P:1, UL:1, OL:1, LI:1, BLOCKQUOTE:1, PRE:1, H1:1, H2:1, H3:1, H4:1, H5:1, H6:1}};
                function _tailTextHost(node) {{
                    var host = node;
                    for (var _g = 0; _g < 6; _g++) {{
                        var lc = host.lastElementChild;
                        if (!lc) break;
                        if (lc.tagName === 'PRE') {{
                            // 代码块：文本承载在 <code> 内（行内 <code> 不下潜，
                            // 避免后续正文钻进行内代码）
                            host = (lc.lastElementChild && lc.lastElementChild.tagName === 'CODE')
                                ? lc.lastElementChild : lc;
                            break;
                        }}
                        if (!_BLOCK_TAGS[lc.tagName]) break;
                        host = lc;
                    }}
                    return host;
                }}
                // [PERF] 文本合并追加：帧级揭示（~60fps）下若每次都 appendChild 新建
                // 文本节点，一条 2000 字回复会产出 600+ 个 Text 节点 —— DOM 节点数
                // 膨胀，内存与后续 querySelectorAll / innerHTML 成本同步上升
                // （用户反馈"内存占用高了很多"的主因之一）。
                // 末尾已是文本节点时直接 appendData 合并 → 每段稳定 1 个文本节点。
                function _appendTextMerged(host, txt) {{
                    var lc = host.lastChild;
                    if (lc && lc.nodeType === 3) {{
                        lc.appendData(txt);
                    }} else {{
                        host.appendChild(document.createTextNode(txt));
                    }}
                }}
                function _newIncrementalP(txt) {{
                    var _p = document.createElement('p');
                    // [B1] 标记为增量纯文本节点：差量渲染追加格式化 HTML 时会先移除
                    _p.setAttribute('data-incremental', 'true');
                    _p.textContent = txt;
                    c.appendChild(_p);
                    return _p;
                }}
                // ── 智能段落处理 ──
                // 只有**段落分隔**（>=2 个换行）才新建 <p>；单个换行是 Markdown 软换行
                // （最终渲染为空格），必须接在当前段内 —— 否则每个 chunk 独占一行，
                // 流式期间整段正文被切成一堆碎片行。
                var lead = text.match(/^[\\r\\n]+/);
                var newlines = lead ? lead[0].replace(/\\r\\n/g, '\\n').length : 0;
                var last = c.lastElementChild;
                // 🐛 修复（流式文字跳位）：#char-count（字数统计空 DIV）拼在全量 HTML
                // 末尾，是 container 的 lastElementChild。不跳过它的话，全量渲染后
                // 的所有正文 chunk 都拿不到真实末块（稳定 <p>），落入"新建独立 <p>"
                // 兜底分支 → 源文同段文字被拆成独立行先蹦在最底部，下一轮渲染才
                // 合并回正文（用户感知"文字先换行出现在最下面，再跳回正确位置"）。
                if (last && last.id === 'char-count') last = last.previousElementSibling;
                if (newlines >= 2) {{
                    // 段落分隔：去掉前导换行，创建独立 <p>
                    var clean = text.replace(/^[\\n\\r]+/, '');
                    if (clean) {{
                        _newIncrementalP(clean);
                    }} else {{
                        // 纯分隔换行（chunk 里只有 \\n\\n）：**不建空节点** ——
                        // 空 <p> 的上下 margin 会凭空撑高一行，150~500ms 后又被
                        // tail 渲染移除，表现为"正文下方闪一段空白"。改为打挂起标记，
                        // 由下一个文字 chunk 建新段落（无空行抖动 + 段落立即正确）。
                        c.setAttribute('data-pending-break', '1');
                    }}
                }} else if (c.getAttribute('data-pending-break') === '1') {{
                    // 上一段以纯分隔换行收尾：新文字必须另起一段。
                    // 否则会短暂粘在上一段末尾，等下次 tail 渲染才分开 → 又是一次跳位。
                    c.removeAttribute('data-pending-break');
                    _newIncrementalP(text);
                }} else if (last && last.getAttribute('data-incremental') === 'true') {{
                    // 🐛 修复（流式文字碎片化）：增量节点（含 data-rendered 的尾部渲染节点）
                    // **就地追加文本节点**承接新文字，保持与已有内容连续。
                    // 旧逻辑对 data-rendered 节点新建独立 <p> —— 流式期间每个 chunk 都堆出
                    // 一个带段落间距的新行，观感就是"文字先在最后几行以片段形式冒出来，
                    // 随后 updateTailHtml 又把碎片合并回正文"（文字不断跳位重排）。
                    // appendChild(textNode) 既不覆盖已渲染的行内 HTML（textContent += 会把
                    // <strong>/<code> 抹回 markdown 源码形态），又让文字连续增长。
                    _appendTextMerged(_tailTextHost(last), text);
                }} else if (last && last.tagName === 'P') {{
                    // 🐛 修复（正文段落丢失）：最后是已格式化渲染的稳定段落（非增量节点）。
                    // 不能打 data-incremental 标记/原地追加——否则下次差量渲染
                    // updateContentAppend 移除全部 data-incremental 节点时会连带
                    // 删除该稳定段落（已渲染正文永久丢失，"内容显示不全"）。
                    // 新建增量节点承载：格式化段落必为已闭合段（\\n\\n 结尾），
                    // 后续文本属新段落，独立 <p> 结构正确。
                    _newIncrementalP(text);
                }} else {{
                    // 思考块 / 工具块 / 空容器等：新段落承载
                    _newIncrementalP(text);
                }}
                // 🐛 修复：同步 auto-scroll（无 setTimeout 渲染间隙），
                // 避免浏览器在异步间隙中 paint 出滚动位置不一致的画面。
                // 附加修复：auto-scroll 成功后复位 _userScrolledWithin，
                // 防止用户一次滚轮操作后永久丧失粘性滚底能力。
                // 用 scrollTop 差值识别用户滚动（替代原 200ms 时间窗，避免
                // 快速流式时时间窗永不过期导致用户滚轮被永久忽略）。
                window._suppressScrollEvent = true;
                if (!window._userScrolledWithin) {{
                    _autoScrollStreamingBody();
                }} else {{
                    var wasAtBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < {AUTO_SCROLL_THRESHOLD};
                    if (wasAtBottom) {{
                        _autoScrollStreamingBody();
                        window._userScrolledWithin = false;
                    }}
                }}
                // 同步 _prevScrollTop，使 delta 检测有正确的基线
                window._prevScrollTop = document.body.scrollTop;
                window._autoScrollTime = performance.now();
                window._suppressScrollEvent = false;
                // skipReport：打字机揭示按帧调用，由队列侧节流（见 _twStep）
                if (!skipReport) reportHeightDebounced();
                }};  // ── _dfxAppendStreamText 定义结束 ──
                }}
                // ── 交给打字机揭示队列（帧级揭示）──
                // 队列不可用时（旧骨架 / 降级）退化为立即追加，行为与改造前一致。
                var text = {json.dumps(text_clean).decode("utf-8")};
                if (typeof window._twPush === 'function') {{
                    window._twPush(text);
                }} else {{
                    window._dfxAppendStreamText(text);
                }}
            }})();
            """
            self.page().runJavaScript(js)
        except RuntimeError:
            pass

    def _render_markdown_to_html(self, raw_md: str) -> str:
        """渲染 markdown 到 HTML。

        reasoning 现在作为 <think> 标签嵌入在 raw_md 中（由 content_to_markdown 生成），
        与文本、工具结果按实际顺序交错排列，不再需要单独的 _reasoning_blocks 逻辑。
        """
        # 刷新字体（响应系统字体设置变化）
        self._refresh_viewer_font_css()
        # 根据主题切换代码高亮风格（通用代码块 + 行内 diff）
        try:
            from app.utils.theme_manager import theme_manager
            from app.widgets.render_helpers import set_diff_highlight_style

            _style = "friendly" if theme_manager.is_light_theme() else "dracula"
            set_pygments_style(_style)
            set_diff_highlight_style(_style)
            # 同步缓存图标前缀，避免每次渲染都重新检测主题
            _update_icon_prefix()
            global _CODE_FONT_SIZE
            _CODE_FONT_SIZE = scale_font_size(13)
        except Exception:
            pass

        if not self._streaming:
            # 非流式模式：直接渲染，所有 <think> 都是已完成的
            html_content = _render_markdown_to_html_cached(
                raw_md,
                compact=self._tool_compact_mode,
            )
            # 将图片相对路径转为绝对 file:/// 路径
            html_content = _resolve_image_src(html_content)
            return html_content

        # 流式模式：仅在最后一个块是 reasoning 且思考尚未被工具调用标记为完成时，去掉其闭合标签
        # 判断标准：markdown 以 </think> 结尾（说明最后一个块恰好是 reasoning）
        streaming_md = raw_md.rstrip()
        if self._streaming and streaming_md.endswith("</think>") and not self._thinking_finalized:
            # 末尾正好是 reasoning 块的闭合标签，去掉它表示该块尚未完成
            streaming_md = streaming_md[: -len("</think>")].rstrip()

        safe_md = _sanitize_incomplete_markdown(streaming_md)
        safe_md = _extract_formulas(safe_md)  # KaTeX 公式提取
        safe_md = _unwrap_code_blocks_with_context_links(safe_md)
        safe_md = _inject_context_links(safe_md)
        # fence 内容保护：代码块内协议标签不被 inject 抽出渲染成假卡片
        # （与 _render_markdown_to_html_worker 流式分支对齐；缺此保护时模型在
        # 代码示例中写的 <tool>/<think> 协议标签会在流式期间被抽成假卡片，
        # 流式结束非流式渲染有保护又变回代码块，形态跳变）
        _fences, safe_md = _extract_fenced_code(safe_md)
        processed_md = _inject_think_cards(safe_md, self._streaming is False, compact=self._tool_compact_mode)
        processed_md = _inject_tool_blocks(processed_md, self._streaming is False, compact=self._tool_compact_mode)
        processed_md = _inject_hook_blocks(processed_md, self._streaming is False)
        processed_md = _inject_tag_cards(processed_md, self._streaming is False, compact=self._tool_compact_mode)
        processed_md = _restore_fenced_code(processed_md, _fences)

        # [PERF] 实例级哈希缓存：processed_md 未变时直接返回缓存的 HTML，
        # 跳过 md.convert() + _wrap_code_blocks（最昂贵的步骤）。
        # 命中场景：resize 触发重复渲染、thinking_finalized 状态切换但内容未变、
        # finish_streaming 后的 immediate render 与随后 render 定时器重叠
        processed_hash = hash(processed_md)
        if self._processed_md_hash == processed_hash and self._cached_streaming_html is not None:
            return self._cached_streaming_html

        try:
            md = get_markdown_instance()
            md.reset()
            html_content = md.convert(processed_md)
            html_content = _wrap_code_blocks_with_copy_button_web(html_content)

            # 将图片相对路径转为绝对 file:/// 路径
            html_content = _resolve_image_src(html_content)

            # 流式模式：追加字数统计显示
            if self._streaming:
                html_content = html_content + _CHAR_COUNT_HTML

            # 缓存渲染结果（只存一份，内存开销小）
            self._processed_md_hash = processed_hash
            self._cached_streaming_html = html_content
            self._cached_raw_md_hash = hash(str(self._markdown_text))
            return html_content
        except Exception:
            return f"<pre>{escape(raw_md)}</pre>"

    def _schedule_render(self, immediate: bool = False):
        if not self._is_js_ready:
            # 🛡️ F2：JS 未就绪时的渲染请求标记 deferred（不直接丢弃），
            # _on_js_ready 时统一补渲。否则 viewer 创建后未显示 + JS 未加载
            # 完成 + 期间渲染请求（隐藏 tab 积压）→ 请求被清但永不补渲，
            # 工具区/消息区永久空白且无自愈路径。
            self._render_deferred = True
            return
        # [V1] 可见性门控：隐藏 tab 不启动渲染定时器、不立即渲染，
        # 仅标记 deferred，恢复可见时（showEvent）按需补渲。
        # 流式数据由 worker 驱动写入 _markdown_text，门控只跳过 UI 渲染帧，不丢数据。
        if not self.isVisible():
            self._render_deferred = True
            return
        if immediate:
            if self._render_timer.isActive():
                self._render_timer.stop()
            self._perform_update()
            return

        # ── 差量渲染策略 ──
        # 增量纯文本已由 _append_text_incremental 即时显示到 DOM，
        # 全量 HTML 渲染仅在以下时机触发，避免 O(n) 逐帧重排：
        # 1. 自然边界触发（由 append_chunk 检测到并传 immediate=True）
        # 2. 安全兜底：2s 内无边界到达，强制渲染确保格式最终正确
        if self._streaming:
            # 硬边界（段落结束 / think 闭合 / 代码块闭合）：立即渲染。
            # 硬边界密度远低于软边界，且段落结束必须及时重排，保持同步。
            if self._has_reached_clean_boundary(self._markdown_text):
                self._perform_update()
                return
            # 软边界（句号结尾）：**仅在密集流式时合并**，否则仍即时渲染。
            #
            # 背景：中文句号极密集，无脑 immediate 会让整个自适应节流失效并退化
            # 为 O(n²)；但一律延迟又会拖慢打字机观感（句子结束后格式迟迟不变）。
            # 折中：只有「距上次渲染不足一个合并窗口（_SOFT_BOUNDARY_MERGE_MS，
            # 现为 100ms）」时才推迟到窗口末尾——
            #   慢速流式（人能逐句阅读，句间隔 >100ms）→ 仍 immediate，观感不变；
            #   快速流式（连续句号刷屏，句间隔 <100ms）→ 合并为窗口内一次，
            #   砍掉重复转换（实测该场景占流式时长的大头）。
            if self._has_reached_soft_boundary(self._markdown_text):
                since_last_ms = (time.monotonic() - getattr(self, "_last_render_ts", 0.0)) * 1000
                if since_last_ms < self._SOFT_BOUNDARY_MERGE_MS:
                    # ⚠️ 定时器已激活时必须比较间隔再决定是否重启：
                    # _render_timer 是 singleShot，若已被 150~500ms 的兜底定时器
                    # 占用而不重启，句子结束也要干等到兜底间隔才渲染（观感明显变慢）。
                    if (not self._render_timer.isActive()) or (
                        self._render_timer.interval() > self._SOFT_BOUNDARY_MERGE_MS
                    ):
                        self._render_timer.start(self._SOFT_BOUNDARY_MERGE_MS)
                else:
                    self._perform_update()
                return
            # 无边界：启安全定时器（仅当未激活时）
            # [PERF] 使用自适应间隔：快速流式用 150ms，慢速用 500ms，默认 300ms
            if not self._render_timer.isActive():
                self._render_timer.start(self._current_adaptive_interval)
        else:
            # 非流式模式（历史加载）：40ms 防抖后渲染
            if not self._render_timer.isActive():
                self._render_timer.start(40)

    def _refresh_viewer_font(self):
        """刷新 viewer 字体样式，响应系统字体设置变化"""
        if not hasattr(self, "_viewer_font_family"):
            return
        # [B1] 字体变化：差量 HTML 缓存失效，强制全量重渲染
        # 🆕 差量收尾：满足条件时保留差量基线（_stable_md_len），走「稳定区不动
        # + 仅收尾」路径，避免整页 innerHTML 替换造成的重排闪动与坞态归位跳动。
        # 不满足则原样回退全量终渲染（行为完全不变）。
        self._incremental_finalize = self._should_incremental_finalize()
        if self._incremental_finalize:
            self._needs_full_render = False
        else:
            self._needs_full_render = True
            self._stable_html = ""
            self._stable_md_len = 0
        self._refresh_viewer_font_css()
        self._schedule_render(immediate=True)

    def _refresh_viewer_font_css(self):
        """刷新字体 CSS 变量，供 render 使用"""
        if not hasattr(self, "_viewer_font_family"):
            return
        font_family = self._viewer_font_family
        font_css = get_font_family_css()
        body_font_size = scale_font_size(14)
        self._viewer_font_css = f"{font_css} font-family: {font_family}, sans-serif; font-size: {body_font_size}px;"

    def refresh_theme(self):
        """刷新主题颜色，响应全局主题切换

        优化：使用 ThemeRefreshCoordinator 全局缓存 JS 字符串。
        同一主题版本内所有 MessageCard 共享同一份 JS 代码，
        避免逐卡重复构建字符串。
        """
        from app.utils.theme_refresh import ThemeRefreshCoordinator

        try:
            from app.utils.theme_manager import theme_manager

            _is_light = theme_manager.is_light_theme()
        except Exception:
            _is_light = False

        # 版本号检查：同一主题版本内跳过 JS 注入
        v = ThemeRefreshCoordinator.get_version()
        if getattr(self, "_last_theme_version", -1) == v:
            return
        self._last_theme_version = v

        # 🐛 主题确实变化：失效实例级 markdown HTML 缓存。
        # 否则 _perform_update 非流式分支的 _cached_streaming_html 复用逻辑
        # 会返回旧主题渲染的 HTML（旧 pygments 代码高亮 + 旧思考图标路径），
        # 导致主题切换后代码块颜色/思考图标不更新。
        self._cached_streaming_html = None
        self._processed_md_hash = 0
        self._cached_raw_md_hash = 0
        # [B3] 主题变化：递增渲染序号使在途线程池任务过期（旧主题 HTML 丢弃），
        # 强制后续 _schedule_render 以新主题重新提交渲染。
        self._render_seq += 1
        # [B1] 主题变化：差量 HTML 缓存失效（旧主题高亮/图标颜色），强制全量重渲染
        self._needs_full_render = True
        self._stable_html = ""
        self._stable_md_len = 0

        theme = current_theme()
        js_code = ThemeRefreshCoordinator.get_or_build_js(theme, _is_light)
        # body_font_size 供图表主题 JS 使用（与 _load_skeleton 构建期同语义：
        # scale_font_size(14)）。此前引用的是 _refresh_viewer_font_css 的局部
        # 变量，作用域外抛 NameError，致批处理主题刷新在该卡中断，后续输入框/
        # 设置弹窗卡片刷新全部跳过（2026-09-06 日志实证）。
        body_font_size = scale_font_size(14)

        # [PERF] 仅对可见 viewer 注入 CSS 变量：隐藏卡（不可见 tab / 未渲染）
        # 跳过 runJavaScript（WebEngine IPC 开销大，200 卡 ≈ 100ms）。
        # 跳过时置 _theme_css_pending 标记，恢复可见（showEvent）补注入，
        # 避免 updateContent 复用旧骨架 CSS 变量导致主题色残留。
        try:
            if self.page():
                # [T28] 常量更新与重活解耦：三件套（_applyChartTheme / _MMD_THEME_VARS /
                # _mmdApplyTheme）是 echarts init 主题的唯一运行时更新通道，属便宜
                # 常量写入，对所有 page 存活的卡无条件发送（无图卡流式新增图表也要
                # 用新主题常量 init，否则明暗错配——T27 审查 B1）。
                _chart_theme_const_js = (
                    f"window._applyChartTheme({str(not _is_light).lower()});"
                    f"window._MMD_THEME_VARS = {_mmd_theme_vars_js(body_font_size)};"
                    "window._mmdApplyTheme();"
                )
                # [vault] 主题切换：清空图表暂存区 + dispose 旧主题 echarts 实例
                # （重活，严格按象限门控）。echarts 主题在 init 时确定、实例不可变色，
                # 复用旧实例会残留旧配色；清空 vault + 置 _echartInited=false 后，
                # 下次全量渲染按新主题重 init。有图卡隐藏时仍必须执行——否则恢复
                # 可见后 vault 回插的仍是旧主题实例。
                # [PERF] 主题切换会丢弃全部已渲染图表，必须**逐个 dispose** 而非
                # 只 clear() Map：vault 里每个节点都持有 echarts 实例 + ResizeObserver
                # （RO 对 target 是强引用，不 disconnect 则整棵子树常驻）。原实现
                # clear() 丢弃引用但不释放资源，每次主题切换泄漏一批，与流式期间
                # 的孤儿实例叠加 → 多图卡片 renderer 进程 OOM 白屏。
                _chart_reset_js = (
                    "if (window.__chartVault && window.__chartVault.size) {"
                    "  window.__chartVault.forEach(function (el) { window._disposeChartNode(el); });"
                    "  window.__chartVault.clear();"
                    "}"
                    "if (window.echarts) {"
                    "  document.querySelectorAll('.echarts-container').forEach(function (el) {"
                    "    window._disposeChartNode(el);"
                    "    el._chartStashed = false;"
                    "  });"
                    "}"
                )

                def _wrap_try(js: str, tag: str) -> str:
                    return (
                        "try{" + js + f"}}catch(err){{if(window.console)console.warn('[theme] {tag} failed',err);}}"
                    )

                # [T28] 四象限分发（互斥）。常量串对所有象限必发（象限 4 由 T24 的
                # 零 IPC 变为仅发轻量常量串——语义变化，见上）；vault 释放重活仍
                # 严格按有图门控。可见象限合并为一次 IPC。
                # _has_charts 用 getattr 防御：__new__ 绕过 __init__ 的测试桩
                # 无此属性（项目惯例，如 test_message_card_refresh_theme）。
                if self.isVisible():
                    if getattr(self, "_has_charts", False):
                        # 有图可见：常量 + 重置 + CSS 三段合并一次 IPC（各自 try/catch
                        # 异常域隔离，一段抛错不拖垮另一段，T8 风险 2）
                        _combined_js = (
                            _wrap_try(_chart_theme_const_js, "chart const")
                            + _wrap_try(_chart_reset_js, "chart reset")
                            + _wrap_try(js_code, "css vars")
                        )
                        self.page().runJavaScript(_combined_js)
                    else:
                        # 无图可见：常量并入 CSS 段，仍一次 IPC
                        self.page().runJavaScript(_wrap_try(_chart_theme_const_js, "chart const") + _wrap_try(js_code, "css vars"))
                    self._theme_css_pending = False
                elif getattr(self, "_has_charts", False):
                    # 有图隐藏：常量 + 重置合并；CSS 变量走 _theme_css_pending 补注入
                    self.page().runJavaScript(
                        _wrap_try(_chart_theme_const_js, "chart const") + _wrap_try(_chart_reset_js, "chart reset")
                    )
                    self._theme_css_pending = True
                else:
                    # 无图隐藏：仅发轻量常量串（T28 语义变化），CSS 走补注入
                    self.page().runJavaScript(_wrap_try(_chart_theme_const_js, "chart const"))
                    self._theme_css_pending = True
        except RuntimeError:
            pass

    def _mark_has_charts(self, html: str) -> None:
        """[T24] 全量渲染产物含图表容器任一标记 → 置位 _has_charts（只置不清）"""
        if (
            "echarts-container" in html
            or "mermaid-block" in html
            or "chart-streaming" in html
        ):
            self._has_charts = True

    def _perform_update(self):
        # ⚠️ 必须无条件取时间：此前写成 `perf_counter() if FINISH_TIMING_ENABLED else 0.0`，
        # 未开打点时 _t_enter=0，render 会被算成 perf_counter()*1000（千万毫秒级假数据）。
        _t_enter = time.perf_counter()
        # [PERF] 记录本次渲染时刻，供 _schedule_render 的软边界合并窗口判断
        self._last_render_ts = time.monotonic()
        try:
            if not self.page():
                return

            # [V1] 可见性门控（双保险）：直接调用路径（如工具结果到达时
            # MessageCard 直接调 viewer._perform_update）绕过 _schedule_render，
            # 隐藏 tab 时不执行 setHtml/runJavaScript，标记 deferred 待恢复补渲。
            if not self.isVisible():
                self._render_deferred = True
                return

            # 预览文字打字机开关：只在本轮流式（含结束后的终渲染）播放。
            # 历史会话加载时 _streaming / _streaming_finished 均为 False —— 一次
            # 加载几十张卡片，若都逐字播放会同时起几十个 rAF 抢帧，且"打字机"
            # 对已存在的历史内容没有意义，故关闭（文字按渲染结果直接全显）。
            _pt_enabled = "true" if (self._streaming or getattr(self, "_streaming_finished", False)) else "false"
            try:
                self.page().runJavaScript(f"if (window._pt) window._pt.enabled = {_pt_enabled};")
            except RuntimeError:
                pass

            # 已完成（结果已到达）的工具 id 集合，供下方 restore 逻辑判断运行框是否可复活
            _finished_ids = list(getattr(self, "_restore_finished_ids", set()) or set())
            _safe_finished = json.dumps(_finished_ids).decode("utf-8")

            # ── 非流式模式（历史加载 / 流式结束）：直接渲染，跳过所有增量比较逻辑 ──
            if not self._streaming:
                # 🆕 差量收尾：稳定区 DOM 不动，只补渲染剩余段 + 流式块就地定稿。
                # 成功即返回，完全不走下面的整页 innerHTML 替换。
                if getattr(self, "_incremental_finalize", False) and self._try_incremental_finalize():
                    self._incremental_finalize = False
                    self._final_render_pending = False
                    self._last_rendered_markdown = self._markdown_text
                    return
                # 收尾失败 / 不适用 → 清标记 + 清稳定区基线，回退原全量路径。
                # _try 失败时 _stable_md_len/_stable_html 可能已被部分推进
                # （先 extract 推进、后 rest 检查未闭合），残值是脏偏移；本轮
                # 全量重建 DOM 不读它们，但必须清掉防下一轮差量续写错位。
                self._incremental_finalize = False
                self._stable_html = ""
                self._stable_md_len = 0
                self._refresh_viewer_font_css()
                # 如果有懒回调，执行一次获取最终 markdown
                if self._lazy_markdown_cb:
                    self._markdown_text = self._lazy_markdown_cb()
                    self._lazy_markdown_cb = None
                # 🚀 [PERF] 流式结束优化：复用 _cached_streaming_html 跳过重渲染
                # finish_streaming() 触发此非流式分支时，_cached_streaming_html
                # 已有完整的渲染结果（由流式模式的最后一次 _render_markdown_to_html
                # 缓存）。直接复用可避免重复的 markdown→HTML 转换（sanitize +
                # inject_think + inject_tool + md.convert），节省 20-80ms 主线程阻塞。
                # ⚡ 哈希验证：确认 _markdown_text 自缓存以来未改变
                # （防止 _lazy_markdown_cb 在缓存后更新了 _markdown_text）。
                # 移除流式模式追加的字符统计 <div>，它只在流式期间有用。
                if (
                    self._cached_streaming_html is not None
                    and hash(str(self._markdown_text)) == self._cached_raw_md_hash
                ):
                    if _CHAR_COUNT_HTML in self._cached_streaming_html:
                        html_content = self._cached_streaming_html[
                            : self._cached_streaming_html.rfind(_CHAR_COUNT_HTML)
                        ]
                    else:
                        html_content = self._cached_streaming_html
                elif len(self._markdown_text) > self._ASYNC_HISTORY_RENDER_MIN_CHARS and not getattr(
                    self, "_final_render_pending", False
                ):
                    # [PERF] 长内容的**历史/非结束态**渲染走线程池：md.convert +
                    # Pygments 在长消息上是 40~120ms 的主线程阻塞（真机实测），
                    # 加载大会话时每张卡都堵一拍。_apply_render_result 已覆盖
                    # save/restore、auto-scroll 与 seq 过期丢弃，语义等价。
                    self._last_rendered_markdown = self._markdown_text
                    self._height_report_pending = True
                    self._sequence_render(self._markdown_text, self._tool_compact_mode)
                    return
                else:
                    # ⚠️ 结束态（_final_render_pending）必须保持同步：
                    # MessageCard.finish_streaming 在 viewer.finish_streaming() 之后
                    # 立即调用 _cleanup_render_cache()，而它会 `self._render_seq += 1`
                    # （本意是让在途流式渲染过期）。异步提交的结果回来时 seq 已变，
                    # 被 _apply_render_result 判定过期直接丢弃 → 最终渲染永远不落地
                    # （卡片停在流式形态、高度不收敛）。要保持结束态异步就必须把
                    # cleanup 推迟到渲染落地之后，属另一处改造。
                    html_content = self._render_markdown_to_html(self._markdown_text)
                self._final_render_pending = False
                self._last_rendered_markdown = self._markdown_text
                self._height_report_pending = True
                # [T24] 非流式全量产物入图检测（只置不清）
                self._mark_has_charts(html_content)
                # 🐛 修复：非流式路径也会在"流式结束但工具仍在并行执行"时触发
                # （finish_streaming 将 _streaming 置 False 后走此分支）。
                # 此时 DOM 中存在 JS 增量注入的"工具运行折叠框"（data-tool-call-id），
                # 它不在 _content_data 中、也不会被 markdown 重新生成。
                # 若直接 updateContent 会整体替换 content-placeholder 的 innerHTML，
                # 把所有运行框连同已完成的工具结果块一并抹掉，导致
                # "一堆运行框出现后又立马消失，只剩个别框" 的闪灭现象。
                # 因此与流式分支保持一致：先 save 活跃+已完成运行块，updateContent 后用 restore 还原。
                # ♻️ 修复：保存所有 [data-tool-call-id] 块，不仅 data-streaming="true"。
                # 因为 finish_tool_streaming 注入的已完成块 (data-streaming="false")
                # 不在 _content_data 中，不会被 markdown 重新生成，若不保存也会被抹掉。
                # 非流式分支：使用共享的 _build_save_and_restore_js 模板
                # 🚀 [PERF] 使用异步 runJavaScript（带 callback）避免主线程阻塞
                # 等待 WebEngine 处理 DOM。同步版本会卡 30-120ms。
                # 异步后主线程立即释放，WebEngine 在后台解析 HTML 和替换 DOM。
                # [B2] IPC 瘦身：仅当工具 DOM 被 JS 增量注入（_tool_dom_dirty）或存在
                # 已完成工具块待 restore（_restore_finished_ids）时才需要 save/restore 保护；
                # 否则裸 updateContent（省整页 JS 包装，MB 级 IPC 载荷下降）。
                _needs_save_restore = self._tool_dom_dirty or bool(getattr(self, "_restore_finished_ids", set()))
                _kind = "finish" if getattr(self, "_finish_t0", 0.0) > 0.0 else "history"
                _t_ser = time.perf_counter()
                if _needs_save_restore:
                    _gen = self._tool_dom_dirty_gen
                    _js_code = self._build_save_and_restore_js(
                        html_content, getattr(self, "_restore_finished_ids", set())
                    )
                    _ser_ms = (time.perf_counter() - _t_ser) * 1000
                    _render_ms = (_t_ser - _t_enter) * 1000
                    # 🐛 打点默认也打（只在"慢"时打，常规一两行/条消息，噪声可忽略）：
                    # 结束态卡顿必须靠数据定位，不能靠猜。DRIFOX_FINISH_TIMING=1 强制全量。
                    if FINISH_TIMING_ENABLED or _render_ms >= 20 or _ser_ms >= 8:
                        logger.info(
                            f"[finish-render] kind={_kind} path=save_restore "
                            f"md={len(self._markdown_text)} "
                            f"html={len(html_content)} render={_render_ms:.1f}ms dumps={_ser_ms:.1f}ms"
                        )
                    self.page().runJavaScript(_js_code, lambda _r, _g=_gen: self._clear_tool_dom_dirty_guarded(_g))
                else:
                    _payload = json.dumps(html_content).decode("utf-8")
                    _ser_ms = (time.perf_counter() - _t_ser) * 1000
                    _render_ms = (_t_ser - _t_enter) * 1000
                    if FINISH_TIMING_ENABLED or _render_ms >= 20 or _ser_ms >= 8:
                        logger.info(
                            f"[finish-render] kind={_kind} path=bare "
                            f"md={len(self._markdown_text)} "
                            f"html={len(html_content)} render={_render_ms:.1f}ms dumps={_ser_ms:.1f}ms"
                        )
                    self.page().runJavaScript(
                        f"updateContent({_payload});",
                        lambda _result: None,
                    )
                # 🐛 修复（编辑工具框运行中消失）：不再同步清除 _tool_dom_dirty——
                # runJavaScript 异步，JS 未执行完时 DOM 中运行框仍在；若立即清 dirty，
                # 紧随其后的渲染（正文流式/兜底/finish_streaming）判定 _needs_save_restore=False
                # → 裸 updateContent 重建 content-placeholder → 抹掉 JS 注入的运行框。
                # 清除交由 JS 回调 _clear_tool_dom_dirty_guarded（pending + 代际守卫）。
                self._last_rendered_html = None
                return

            # ── 以下为流式模式（增量渲染） ──
            # 懒加载：通过回调获取最新 markdown（避免每次 reasoning chunk 都调用 content_to_markdown）
            if self._lazy_markdown_cb:
                fresh_md = self._lazy_markdown_cb()
                self._lazy_markdown_cb = None  # 清除回调，避免后续 set_content 重复转换
                self._markdown_text = fresh_md
            elif self._markdown_text:
                # 🐛 修复：_markdown_text 已通过 set_content（来自 ensure_rendered）
                # 预填充了内容，但 _lazy_markdown_cb 从未被 append_text 设置过。
                # 不应跳过渲染，否则内容永远不显示。
                pass
            else:
                # [PERF-opt] 无新内容：流式模式下跳过全量渲染
                # 工具块/思考块的状态切换已通过增量 JS（_inject_tool_streaming_html /
                # _maybe_finish_thinking_for_tool）处理完毕，无需全量 updateContent
                # 覆盖 DOM，避免"闪灭→再现"闪烁和重复工作。
                return

            # [PERF-opt] 内容变化检测：markdown 未变化时跳过全量渲染
            # 避免定时器空转、回调无变化等场景下的冗余 innerHTML 替换
            if self._markdown_text == self._last_rendered_markdown:
                return

            # [B1] 差量渲染快路径：流式且非全量模式时，仅增量渲染已闭合的完整段。
            # 条件：流式 + 未强制全量 + 无活跃工具 DOM（工具块走 save/restore 全量保护）
            if self._streaming and not self._needs_full_render and not self._has_active_tool_dom():
                stable_len, segs = _extract_closed_segments(self._markdown_text[self._stable_md_len :])
                if segs:
                    # 增量渲染闭合段：sanitize→inject→md.convert（主线程小段快速路径）
                    # 差量段很小（单个段落/代码块），同步渲染耗时 <1ms，无需线程池
                    # 🐛 修复：传 _tool_compact_mode，与全量渲染 _render_markdown_to_html
                    # 的 compact 对齐——否则差量段硬编码 compact=False 会把思考块渲染成
                    # think-block 折叠框（简洁模式下应为 think-compact），形态分裂
                    # （9c76d04f 只给 _render_stable_segment 加了参数，调用点漏改）。
                    new_html = "".join(_render_stable_segment(seg, compact=self._tool_compact_mode) for seg in segs)
                    self._stable_md_len += stable_len
                    self._stable_html += new_html
                    # ⚠️ updateContentAppend 是"追加"语义：只推送本次新增段，
                    # 不能推送累积值（否则旧段重复渲染）。
                    # 🐛 修复（正文尾部丢失）：_extract_closed_segments 只产出
                    # 已闭合段；未闭合尾部（stable 之后的剩余 md）若不移交给 JS，
                    # updateContentAppend 移除 data-incremental 节点时会连带删除
                    # 该尾部 → 正文尾部永久丢失（用户可见"显示不全"）。
                    # 将未闭合尾部**行内渲染后的 HTML**作为第二参数传入，JS 端重建
                    # 增量节点保尾（innerHTML 注入：已闭合的行内语法即时格式化，
                    # 不再字面显示 markdown 源码）。
                    # ⚠️ 未闭合 think/tool：tail 含未闭合块时不渲染（静默累积，
                    # 等闭合后由差量段/全量渲染处理，避免思考内容泄漏到正文）。
                    _tail = self._markdown_text[self._stable_md_len :]
                    _tail_html = ""
                    if _tail:
                        # 🐛 修复（流式吞内容）：tail 含未闭合 think/tool 时只截到未闭合
                        # 块起点——未闭合块内容照旧静默累积（等闭合后由差量/全量渲染
                        # 落地），但它**之前**的正文必须重建：updateContentAppend 会
                        # 无条件 remove 全部 [data-incremental] 节点，整段 tail 不重建
                        # 就等于把这部分已显示的文字抹掉且不恢复。
                        _safe_tail = _tail_before_unclosed_block(_tail)
                        if _safe_tail and not _has_unclosed_think_or_tool(_safe_tail):
                            _tail_html = _render_inline_tail(_safe_tail, compact=self._tool_compact_mode)
                    js = (
                        "updateContentAppend("
                        f"{json.dumps(new_html).decode('utf-8')},"
                        f"{json.dumps(_tail_html).decode('utf-8')});"
                    )
                    # [T28] 差量产物入图检测（只置不清）：流式新增图表块不再拉长
                    # 无图卡窗口期（与全量置位点同款）
                    self._mark_has_charts(new_html + _tail_html)
                    self.page().runJavaScript(js)
                    # 已差量消费的 markdown 视为"已渲染"（避免重复全量）
                    self._last_rendered_markdown = self._markdown_text[: self._stable_md_len]
                    return
                # 无新闭合段：增量纯文本已在 DOM（_append_text_incremental）。
                # 🐛 修复（思考块滞留/泄漏）：think 配对守卫 break（未闭合 think 段）
                # 使差量一个段都产不出时，若 md 已达自然边界（think 闭合 `</think>` /
                # 段落 `\n\n` 结尾），必须走全量渲染消费——否则安全定时器触发的
                # _perform_update 也会被此分支拦截，思考块/闭合段永远滞留
                # （或仅靠流式结束才一次性显示）。
                if self._has_reached_clean_boundary(self._markdown_text):
                    self._refresh_viewer_font_css()
                    self._sequence_render(self._markdown_text, self._tool_compact_mode)
                else:
                    # 🐛 修复（流式显示与最终不符）：无空行分隔的长段落没有闭合段
                    # 可差量渲染，尾部在流式期间以纯文本显示 markdown 源码
                    # （**加粗**、`code`、[链接](url)），直到流式结束全量渲染才
                    # 格式化。将尾部整体行内渲染（单个 convert 保持段落/列表/代码块
                    # 结构正确），替换 DOM 增量节点；未闭合 think/tool 跳过防泄漏。
                    self._render_tail_inline()
                return

            # 🐛 修复（大段正文流式期间纯文本滞留）：差量快路径被 _needs_full_render
            # （初始 True，首次全量渲染应用成功才置 False）或 _has_active_tool_dom()
            # 让位时，流式正文只能依赖全量线程池渲染。大段正文渲染耗时长，期间新
            # chunk 持续提交新 seq → 在途结果被 _apply_render_result 的 seq 校验
            # 丢弃 → _needs_full_render 保持 True → 差量路径永远进不去 → 纯文本
            # 滞留到流式结束才一次性刷新成 HTML。
            # 尾部行内渲染不依赖全量渲染（自带哈希缓存、只操作 data-incremental
            # 节点，对工具 DOM 安全），在差量不可走的流式路径也先执行，保证流式
            # 期间 markdown 语法（**加粗**、`code`、[链接]）即时格式化。
            if self._streaming:
                self._render_tail_inline()

            # 刷新字体 CSS var
            self._refresh_viewer_font_css()

            # [B3] 渲染移出主线程：提交线程池渲染，完成回调在主线程应用 DOM。
            # 主线程不再同步执行 md.convert（20-80ms 阻塞消除），
            # 渲染参数以快照形式传引用（md 不复制）。
            self._last_rendered_markdown = self._markdown_text
            self._sequence_render(self._markdown_text, self._tool_compact_mode)

        except RuntimeError:
            pass

    def _render_tail_inline(self):
        """把流式未闭合尾部整体行内渲染为 HTML，替换 DOM 增量纯文本节点。

        解决：无空行分隔的长段落（大模型常见输出，尤其中文）在流式期间没有
        `\\n\\n` 闭合段可差量渲染，尾部长时间以纯文本显示 markdown 源码
        （**加粗**、`code`、[链接](url)），只有流式结束全量渲染才格式化——
        用户感知"流式显示内容与最终不符"。

        尾部整体一次 convert（_render_inline_tail）：段落/列表/引用/代码块
        结构在单一 markdown 上下文中保持正确；未闭合行内语法由 markdown 库
        字面保留、闭合后由下一次渲染补全。产物为带 data-incremental +
        data-rendered 标记的节点，后续差量段（updateContentAppend）与全量
        （updateContent）会整体移除替换，无重复。

        带哈希缓存：尾部文本未变化（安全定时器重复触发）时跳过重复渲染。
        """
        _tail = self._markdown_text[self._stable_md_len :]
        if not _tail or not _tail.strip():
            return
        # 🐛 未闭合 think/tool：静默累积，不在此渲染（过滤标签会把思考内容
        # 当正文泄漏显示），等闭合后由差量段/全量渲染处理。
        if _has_unclosed_think_or_tool(_tail):
            return
        # 渲染型 fence 未闭合：静默累积（半截图表代码不能行内渲染成普通代码块，
        # 闭合后由全量渲染分发 chart-streaming 骨架/真图）
        if _has_unclosed_chart_fence(_tail):
            return
        _h = hash(_tail)
        if _h == self._tail_html_hash:
            return
        html = _render_inline_tail(_tail, compact=self._tool_compact_mode)
        # 无论结果是否为空都记录哈希（think/tool 尾部返回空串时避免重复计算）
        self._tail_html_hash = _h
        if not html:
            return
        try:
            js = f"updateTailHtml({json.dumps(html).decode('utf-8')});"
            # [T28] 差量尾部入图检测（chart-streaming 骨架在闭合后才落地，此处是
            # 流式期图表置位的主动径，只置不清）
            self._mark_has_charts(html)
            self.page().runJavaScript(js)
        except RuntimeError:
            pass

    def _clear_tool_dom_dirty_guarded(self, gen: int):
        """JS 渲染回调：带守卫地清除 _tool_dom_dirty。

        🐛 修复（编辑工具框运行中消失）的双重守卫：
        - pending 守卫：_injected_pending_tools 非空（仍有 JS 注入未完成的工具块在
          DOM，如运行框/完成态预览框）→ 不清除。这些块不在 markdown 中，若清 dirty，
          下一次全量渲染会裸 updateContent 抹掉它们（直到 append_tool_result 才重现）。
        - 代际守卫：_tool_dom_dirty_gen 与捕获值一致才清除。若期间有新注入
          （append_tool_result / update_tool_streaming 递增了代际），本回调放弃清除，
          避免"旧渲染回调误清新 dirty"导致运行框失去保护。

        仅在"渲染 JS 真正执行完成"后由 runJavaScript 回调调用（同步清除的旧逻辑
        在 JS 异步未执行时就把 dirty 清掉，是"运行中→完成中间消失"的根因）。
        """
        try:
            if getattr(self, "_injected_pending_tools", None):
                return
            if getattr(self, "_tool_dom_dirty_gen", 0) == gen:
                self._tool_dom_dirty = False
        except Exception:
            pass

    def _has_active_tool_dom(self) -> bool:
        """B1: 是否有活跃工具 DOM（JS 注入的工具块 / 待 restore 的完成块）。
        返回 True 时差量渲染必须让位全量渲染（工具块涉及 save/restore 保护，
        且 _tool_md_cache 影响 _inject_tool_blocks 输出——差量段渲染不带该缓存，
        会导致工具块 HTML 与全量不一致）。
        """
        if self._tool_dom_dirty:
            return True
        try:
            if getattr(self, "_restore_finished_ids", None):
                return True
            # pending 集合非空 = 仍有 JS 注入未完成的工具块在 DOM（运行框/预览框），
            # 差量渲染同样必须让位全量渲染（save/restore 保护）。防御：dirty 清除
            # 回调理论上已受 pending 守卫，此处再兜底一次防其他路径直接改 dirty。
            if getattr(self, "_injected_pending_tools", None):
                return True
        except Exception:
            pass
        return False

    # ========== B3: 异步渲染（线程池 + 序号校验 + 防抖） ==========

    def _collect_render_snapshot(self, md: str, compact: bool) -> dict:
        """主线程：采集渲染快照（只读全局参数，md 引用传递不复制）"""
        try:
            from app.utils.theme_manager import theme_manager

            _style = "friendly" if theme_manager.is_light_theme() else "dracula"
        except Exception:
            _style = "dracula"
        return {
            "md": md,
            "streaming": self._streaming,
            "thinking_finalized": getattr(self, "_thinking_finalized", False),
            "compact": compact,
            "pygments_style": _style,
            "icon_prefix": _ICON_PREFIX_CACHE,
            "heavy_caps": _HISTORY_TOOL_CAPS if getattr(self, "_is_history", False) else None,
            "code_font_size": _CODE_FONT_SIZE,
        }

    def invalidate_inflight_render(self) -> None:
        """B3 兜底：作废在途异步渲染（其结果快照已被新内容超越）。

        使用场景：编辑类工具完成只做 JS 增量注入、不触发渲染（防闪烁设计）。
        若此时恰有在途异步渲染（长内容的非流式渲染走线程池），其 HTML 快照不含
        该工具完成块；结果落地时 save/restore 会把 DOM 中的完成框 `el.remove()`，
        而 restore 判定该 id 已 finished → 不恢复 → 完成框被吞（永久消失）。
        递增 seq 让在途结果过期丢弃，pending 快照一并清空；DOM 由增量注入的块
        与后续任意一次渲染（含 finish_streaming 终渲染）兜底。
        """
        if self._render_inflight:
            self._render_seq += 1
            self._render_pending = None

    def _sequence_render(self, md: str, compact: bool):
        """B3: 序列化异步渲染——提交线程池，在途时只记 pending（防抖积压最新快照）

        - 序号校验：每次提交 seq+=1，回调时 seq != self._render_seq 视为过期丢弃
        - 防抖：在途任务未完成时，新请求只覆盖 _render_pending；完成后续派最新
        """
        self._render_seq += 1
        seq = self._render_seq
        if self._render_inflight:
            # 在途：只记录最新 pending，完成回调后统一续派
            self._render_pending = (seq, md, compact)
            return
        self._render_inflight = True
        snapshot = self._collect_render_snapshot(md, compact)
        try:
            fut = _RENDER_POOL.submit(_render_markdown_to_html_worker, snapshot)
        except RuntimeError:
            # 线程池已关闭（进程退出）：降级为同步渲染
            self._render_inflight = False
            self._apply_render_result(seq, self._render_markdown_to_html(md))
            return
        wself = weakref.ref(self)
        fut.add_done_callback(lambda f, s=seq, w=wself: _dispatch_render_done(s, f, w))

    def _on_render_done_signal(self, seq: int, html):
        """主线程槽：接收 worker 线程池渲染完成信号（renderDone.emit 跨线程投递）"""
        try:
            self._apply_render_result(seq, html)
        except RuntimeError:
            pass

    def _apply_render_result(self, seq: int, html):
        """B3: 主线程应用渲染结果（线程池回调经 QTimer.singleShot 转发至此）

        - seq 守卫：过期结果（新渲染已提交）直接丢弃
        - 成功后检查 pending 续派（在途期间积压的最新快照）
        """
        try:
            if seq != self._render_seq:
                # 过期结果：丢弃（新渲染已提交或已失效）
                return
            if html is None:
                return
            if sip.isdeleted(self) or not self.page():
                return
            self._last_rendered_html = html
            self._height_report_pending = True
            # [T24] 线程池全量产物入图检测（只置不清）
            self._mark_has_charts(html)
            # [B1] 全量渲染成功应用后：重置差量基线——差量稳定区与全量内容对齐，
            # 后续流式新段从当前 markdown 末尾继续差量追加（不再重复渲染已全量覆盖的内容）。
            # ⚠️ 必须用 _last_rendered_markdown（线程池提交时的渲染对象），而非
            # _markdown_text（回调到达时可能已被新 chunk 追加，造成差量跳过未渲染内容）。
            self._stable_html = html
            # 🐛 修复（思考泄漏）：md 含未闭合  thinking/<tool> 块时**不**推进差量基线。
            # 首次流式迭代的 append_reasoning 首 chunk 会触发全量渲染（显示
            # "深度思考中" spinner），此时 md 是部分的思考内容（未闭合 think）。
            # 若照常推进基线，后续差量扫描起点会落在 think 块内部，切片以
            # `内容 response` 开头（无 ` thinking` 配对）→ 配对守卫不触发 →
            # 残段被当普通正文渲染 → 思考内容泄漏到正文（后续全量渲染才消失）。
            # 保持旧基线 → 下一次差量从 think 开头扫描，配对守卫正确 break，
            # 等思考完整闭合后整体差量/全量渲染（无重复：基线未推进期间不产出段）。
            if self._streaming and not _has_unclosed_think_or_tool(self._last_rendered_markdown):
                # 🐛 修复（流式文字跳位）：stable 推进到最后一个 \n\n 之后，而非 md 末尾。
                # 全量渲染的 DOM 末尾 <p> 往往是未闭合段的中间形态（md 尾部无空行），
                # 若 stable 推到 md 末尾，这段半截文字会被划入"稳定区"——但下方打标 JS
                # 已把它标为增量节点，updateTailHtml/updateContentAppend 移除 [inc]
                # 时会删掉它，而 tail（从 stable 起）又不含它 → 内容丢失。
                # 推进到最后段落边界后，末段整体划入 tail 区：打标删除与 tail 重建
                # 语义闭环（删掉的正是 tail 会重建的），不丢不重。
                _md_r = self._last_rendered_markdown
                # fence 感知：推进点不落在未闭合 fence 内部（否则后续差量切片
                # 起点在 fence 内，内部代码行被当普通段产出 → 图表源码生肉流入）
                _last_break = _last_para_break_outside_fence(_md_r)
                self._stable_md_len = _last_break + 2 if _last_break != -1 else 0
            # 🐛 修复（思考框/工具框重复）：md 含未闭合块时基线**不推进**（防残段
            # 泄漏到正文），但 DOM 里已渲染出该块（think-streaming / 工具框）。
            # 若让后续走差量追加，会把已渲染的段再渲染一遍 → 同一段重复出现。
            # 故基线未推进时强制下一次走全量渲染（updateContent 整体替换，不重复）。
            self._needs_full_render = self._streaming and _has_unclosed_think_or_tool(self._last_rendered_markdown)
            # 🐛 修复（流式文字跳位）：全量渲染的 DOM 末尾 <p> 可以是**未闭合段的
            # 中间形态**（首渲染 / 工具后重渲等，md 尾部无 \n\n）。此时末尾 <p>
            # 承载的是未写完的段落，后续同段 chunk 应继续接在它后面。若不补打
            # data-incremental 标记，_append_text_incremental 拿不到增量节点，
            # 新文字落入"新建独立 <p>"分支 → 源文同段被拆成独立行先蹦在最底部，
            # 等下一轮渲染才合并回正文（视觉跳位）。
            # 仅对 <p> 打标：代码块/列表/引用等复杂尾部结构不打（新内容应另起段，
            # 维持原兜底行为，避免文字钻进 <pre>/<li>）。updateContentAppend /
            # updateTailHtml 移除 [inc] 节点时会连带删除它，但闭合段 HTML / tail
            # HTML 包含全文（stable 推进到全量末尾），内容不丢。
            _mark_unclosed_para_js = ""
            if self._streaming and not _has_unclosed_think_or_tool(self._last_rendered_markdown):
                _tail_after_break = (
                    self._last_rendered_markdown.rsplit("\n\n", 1)[-1] if self._last_rendered_markdown else ""
                )
                if _tail_after_break.strip():
                    _mark_unclosed_para_js = (
                        "(function(){"
                        "var _c=document.getElementById('content-placeholder');"
                        "if(!_c)return;"
                        "var _l=_c.lastElementChild;"
                        "if(_l&&_l.id==='char-count')_l=_l.previousElementSibling;"
                        "if(!_l||_l.hasAttribute('data-incremental'))return;"
                        # 🐛 修复（尾部重复）：原判据只认 <p>，末尾是代码块/列表/引用时
                        # 打不上标记 → updateTailHtml / updateContentAppend 删不掉它，
                        # tail 又被重建一遍 → 同一段内容重复出现。放宽到可安全重建的
                        # 块级元素（含代码包装 DIV 里的 <pre>）。
                        # 图表/公式容器不打标：remove() 会销毁已渲染 canvas/SVG，
                        # 而 chart vault 只覆盖 updateContent 路径，不覆盖 append。
                        "if(_l.querySelector&&_l.querySelector('.echarts-container,.mermaid-block,.katex-container'))return;"
                        "var _tn=_l.tagName;"
                        "var _isPre=(_tn==='PRE')||(_tn==='DIV'&&!!_l.querySelector('pre'));"
                        "if(!(_tn==='P'||_tn==='PRE'||_tn==='UL'||_tn==='OL'||_tn==='BLOCKQUOTE'||/^H[1-6]$/.test(_tn)||_isPre))return;"
                        "_l.setAttribute('data-incremental','true');"
                        "})();"
                    )
            # 🐛 修复：全量渲染后卡住不滚底。流式增量文本（_append_text_incremental）
            # 先触发 reportHeight 消费了 _content_just_loaded 标记，导致 50ms 后
            # updateContent 的 height report 到达时 _content_just_loaded 已为 False，
            # _on_message_card_height_changed 跳过外部滚底。
            # 这里在推 JS 前还原标记，确保全量渲染后的 height report 能触发外部滚底。
            _card = self.parent()
            if _card is not None and _card.__class__.__name__ == "MessageCard":
                _card._content_just_loaded = True
            # 流式分支：复用共享的 save+restore 模板，末尾追加 auto-scroll 逻辑
            # （工具块 restore 后 scrollHeight 可能增加，需要重新判断滚到底）
            auto_scroll_js = (
                "window._suppressScrollEvent=true;"
                "if(!window._userScrolledWithin){"
                "document.body.scrollTop=document.body.scrollHeight;"
                "}else{"
                f"var _prd=Math.abs(document.body.scrollHeight-document.body.scrollTop-document.body.clientHeight);"
                f"if(_prd<{AUTO_SCROLL_THRESHOLD}){{"
                "document.body.scrollTop=document.body.scrollHeight;"
                "window._userScrolledWithin=false;"
                "}}"
                "window._prevScrollTop=document.body.scrollTop;"
                "window._autoScrollTime=performance.now();"
                "window._suppressScrollEvent=false;"
            )
            # [B2] IPC 瘦身：仅当工具 DOM 被 JS 增量注入（_tool_dom_dirty）或存在
            # 已完成工具块待 restore（_restore_finished_ids）时才走 save/restore 包装
            _needs_save_restore = self._tool_dom_dirty or bool(getattr(self, "_restore_finished_ids", set()))
            if _needs_save_restore:
                js_code = self._build_save_and_restore_js(html, getattr(self, "_restore_finished_ids", set())).replace(
                    "})();", auto_scroll_js + "})();"
                )
            else:
                js_code = f"updateContent({json.dumps(html).decode('utf-8')});" + auto_scroll_js
            if _mark_unclosed_para_js:
                js_code += _mark_unclosed_para_js
            # 🐛 修复（编辑工具框运行中消失）：dirty 清除延后到 JS 回调（pending + 代际守卫），
            # 原理同 _perform_update 非流式分支——避免异步 JS 未执行期间被下一次渲染
            # 误判"无工具 DOM"而裸 updateContent 抹掉 JS 注入的运行框。
            _gen = self._tool_dom_dirty_gen
            self.page().runJavaScript(js_code, lambda _r, _g=_gen: self._clear_tool_dom_dirty_guarded(_g))
            # 🐛 修复（刚打出的字被抹掉）：updateContent 用的是「渲染快照」的 HTML，
            # 在途期间到达的 chunk 只存在于 DOM 增量节点，整体替换会连它们一起删掉。
            # 落地后把快照之后的增量补回（排在 updateContent 之后执行）。
            self._push_unrendered_tail_text()
            # 释放缓存：HTML 已推送到 WebEngine，Python 端不再保留减少内存占用
            self._last_rendered_html = None
        except RuntimeError:
            pass
        finally:
            # 无论成功/过期，都要释放 in-flight 并续派 pending（若有）
            self._render_inflight = False
            if self._render_pending:
                pseq, pmd, pcompact = self._render_pending
                self._render_pending = None
                self._sequence_render(pmd, pcompact)

    def _push_unrendered_tail_text(self):
        """把「渲染快照之后新增」的文本重新推回 DOM（防全量渲染落地时抹掉）。

        [B3] 全量渲染提交的是 md 快照（`_last_rendered_markdown`）；线程池在途
        期间到达的 chunk 只存在于 DOM 的增量纯文本节点（`_append_text_incremental`）。
        渲染落地时 `updateContent` 整体替换 innerHTML 会连它们一起删掉 —— 视觉上
        是"刚打出来的字消失一块"。落地后把快照之后的增量补回，内容不再回退。
        """
        try:
            if not self._streaming or not self._is_js_ready:
                return
            snapshot = self._last_rendered_markdown or ""
            latest = self._markdown_text or ""
            if not snapshot or len(latest) <= len(snapshot):
                return
            if not latest.startswith(snapshot):
                # 内容被整体替换/重排（非纯追加）：语义未知，交给下一次全量渲染
                return
            extra = latest[len(snapshot) :]
            if not extra.strip():
                return
            # 🐛 未闭合 tag/think/渲染型 fence 的半截内容不能以纯文本回补 DOM
            # （快照可能切在块中间，extra 检不出 open 标签 → 检测用全量 latest）：
            # 回补后会在下一次全量渲染被卡片/图表替换 → "文字闪现后消失"
            if (
                _has_unclosed_registered_tag(latest)
                or _has_unclosed_think(latest)
                or _has_unclosed_chart_fence(latest)
            ):
                return
            self._append_text_incremental(extra)
        except RuntimeError:
            pass

    def _build_save_and_restore_js(self, html_content: str, finished_ids: set = None) -> str:
        """生成"保存工具块 → 重写内容 → 还原工具块"的 JS 模板（流式/非流式共享）

        为什么需要这个三步流程？
        - 流式期间 `_inject_tool_streaming_html` 会把运行中的工具块直接 append 到
          #tool-content（带 data-tool-call-id 标记），不在 _content_data 中。
        - updateContent() 重写 #content-placeholder 的 innerHTML **不会**影响 #tool-content
          里的块，但**已完成且由 JS 注入**的工具结果块（data-tool-call-id）也不会被 markdown
          重新生成（它们是 JS 端瞬时数据）。若不保存就 updateContent，这些块会被 JS 视为
          "应被保留"，从而产生一闪而没或重复出现的"闪灭"现象。
        - 解决：保存 #tool-content 内所有 data-tool-call-id 块 → updateContent → 按原 idx
          位置还原。已完成块会被"复活"为静态折叠框（移除 data-streaming、标记 data-expanded=false）。

        Args:
            html_content: 全量渲染的 HTML
            finished_ids: 已完成（结果已 append_tool_result）的工具 id 集合。
                restore 时这些 id 的块**不恢复**（markdown 已含其结果，updateContent
                会重新生成）；未完成的块（运行中 / finish_tool_streaming 完成态预览）
                必须恢复——它们不在 markdown 中，save 后若不恢复会被抹掉。

        调用方：流式分支需要在末尾额外追加 auto-scroll 逻辑；非流式分支直接 runJavaScript。

        🐛 修复"残留思考框累积"：
        旧实现同时保存 think-block（用 data-block-key），但 reorganizeContent 在
        updateContent 中已正确处理 think-block 从 markdown 的迁移和清理。save/restore
        把旧 think-block 加回来后，reorganizeContent 的清理被完全撤销，导致多轮思考后
        旧思考框持续堆积在 #tool-content 底部。

        【新策略——谁的孩子谁抱走】
        - think-block / think-streaming：完全交给 reorganizeContent 处理（来自 markdown），
          不参与 save/restore。
        - tool-block with data-streaming="true"（流式进行中）：必须 save/restore，
          因为它们由 JS 注入，不在 markdown 中。
        - tool-block with data-streaming="false"（已完成）：来自 markdown，
          不再 save/restore，由 reorganizeContent 从 #content-placeholder 迁移。
        - 恢复时只做"追加回去"，不再做 streaming→completed 转换（因为已完成块已由
          markdown 渲染 + reorganizeContent 处理）。
        """
        _target_id = self._tool_target_id
        _finished_js = json.dumps(list(finished_ids or set())).decode("utf-8")
        return (
            "(function(){"
            # 🆕 Bug B 方案 E：save 阶段把流式工具块的 data-order 暂存到 window，
            # 供 reorganizeContent 补 data-order 时合并（_streamFloors）。根因：save 会
            # 把所有 data-tool-call-id 块（含仍在流式、尚未进入 _content_data 的工具块）
            # 从 #tool-content 移除，导致 reorganizeContent 执行时 toolContent.children
            # 里已无流式块 → _streamFloors 恒为空 → 思考/完成工具块补的 data-order 缺少
            # "排在其前的流式工具数"修正 → restore 按保存的 data-order 插回时与思考块
            # 尺度不一致 → 找不到比它大的节点 → appendChild 沉底 → 折叠框内
            # "所有思考在前、所有工具在后"（坞态归位瞬间错乱）。
            # 🐛 锚点事务：save 阶段会 el.remove() 掉工具块 → 容器 scrollHeight 收缩。
            # 必须在**任何** DOM 操作之前捕获锚点，否则 updateContent 内部捕获到的
            # 已是钳制后的位置 —— 这正是阅读位置漂到「新内容底部」的根因。
            "if(typeof _beginDomUpdate==='function')_beginDomUpdate();"
            "window.__pendingStreamFloors=[];"
            f"var _tc=document.getElementById('{_target_id}');"
            # 🐛 修复（流式滚动位置重置）：save 会清空 #tool-content（el.remove()）
            # → scrollHeight 骤减 → scrollTop 被浏览器钳制（常归 0），restore 后
            # 无恢复逻辑 → 阅读态位置丢失（停在顶部，表现为滚动位置被重置）。
            # save 前记录钳制前位置，restore 后恢复（见尾部 _tcPrevTop 块）。
            "var _tcPrevTop=(_tc&&_tc.scrollHeight>_tc.clientHeight)?_tc.scrollTop:0;"
            # 🆕 修复（简洁模式编辑工具框消失）：save 阶段必须同时覆盖正文容器
            # #content-placeholder——编辑类工具（write/edit/multi_edit 等 _edit_tools() 派生）
            # 的流式/完成块由 JS 注入到正文（L9328 _stream_target），简洁模式下
            # _tool_target_id="tool-content"，旧 save 只遍历 _tc → 编辑工具运行框
            # 不在保存范围 → 全量渲染 updateContent 重建正文时被抹除，直到
            # append_tool_result 才重新出现（"运行中→完成"中间消失一阵子）。
            "var _tcBody=document.getElementById('content-placeholder');"
            "var _saved=[];"
            # 🐛 修复：保存所有 data-tool-call-id 块（含已完成态），并从 DOM 移除。
            # 【根因】原实现只读取 outerHTML 不移除旧块，导致 reorganizeContent
            # 在 updateContent 内部迁移 markdown 新块时，发现 #tool-content 已有
            # 同 data-tool-call-id 的旧块，误判为重复并移除新块。最终 #tool-content
            # 保留旧块（流式态 tool-streaming-block），append_tool_result 的增量
            # 更新找不到 .cm-collapsible__summary / __body，原地转换失败，
            # 运行框卡在"运行中"。
            # 【修复】保存后立即 el.remove()，让 reorganizeContent 干净迁移新块。
            # restore 时只恢复 data-streaming="true" 的流式块（不在 markdown 中），
            # 已完成块由 markdown 重新生成 + reorganizeContent 迁移。
            # [PERF] 快速路径：_tc 无子元素时跳过 save 循环，减少 JS 执行开销
            "var _saveRoots=[_tc];"
            "if(_tcBody&&_tcBody!==_tc)_saveRoots.push(_tcBody);"
            "for(var _sr=0;_sr<_saveRoots.length;_sr++){var _root=_saveRoots[_sr];"
            "if(_root&&_root.children.length){"
            "Array.prototype.forEach.call(Array.prototype.slice.call(_root.children),function(el,i){"
            "if(el.hasAttribute&&el.hasAttribute('data-tool-call-id')){"
            # 🆕 方案 E：暂存流式块（data-streaming="true"）的 data-order，供
            # reorganizeContent 补 data-order 时修正"排在其前的流式工具数"。
            # 这些块即将被 remove()，不在 markdown 中、不会被重新渲染，
            # 只有 save/restore 保留；若不暂存其 data-order，reorganizeContent
            # 的 _streamFloors 收集不到它们 → 思考块补的 data-order 缺修正 → restore
            # 插入循环（只比较带 data-order 的节点）找不到目标 → appendChild 沉底。
            "if(el.getAttribute('data-streaming')==='true'){"
            "var _pfo=parseFloat(el.getAttribute('data-order'));"
            "if(!isNaN(_pfo)){window.__pendingStreamFloors.push(Math.floor(_pfo));}"
            "}"
            "_saved.push({id:el.getAttribute('data-tool-call-id'),"
            "html:el.outerHTML,kind:'tool',"
            "streaming:el.getAttribute('data-streaming')||'',"
            "src:_root.id||''});"
            "el.remove();}"
            "});}"
            "}"
            "document.querySelectorAll('[data-tool-injected]').forEach(function(el){el.remove()});"
            f"updateContent({json.dumps(html_content).decode('utf-8')});"
            # 🐛 修复：只恢复流式进行中的块（data-streaming="true"）。
            # 已完成块已由 markdown 重新生成 + reorganizeContent 迁移到 #tool-content。
            # 恢复流式块时检查同 ID 是否已存在（避免与 reorganizeContent 迁移的块重复）。
            # [PERF] _saved 为空时跳过 restore，这是最常见场景（无活跃工具块）
            f"var _finishedSet={_finished_js};"
            f"if(_saved.length){{_tc=document.getElementById('{_target_id}');if(_tc){{"
            "_saved.forEach(function(b){"
            # 🐛 修复（工具块被吞·restore 判据根治）：restore 条件由「未完成
            # （!isFinished）且 DOM 无同 id 块」改为「**DOM 无同 id 块**」。
            # 旧判据默认「已完成块的 markdown 一定会重建」，把「是否恢复」与「HTML
            # 是否真的含该块」解耦——任何一次全量渲染的 HTML 缺块（在途旧快照落地、
            # _lazy_markdown_cb 未刷新、注入失败、md 生成失败…）都会让该块被 save
            # 移除后无人恢复，永久消失（“编辑工具完成框被吞”的根因族）。
            # 新判据只看 DOM：markdown 已重建同 id 块 → 跳过（防重复，与旧行为一致）；
            # 没重建 → 把保存的块原样放回（无论是否 finished），块不再丢。
            # 历史沿革：`b.streaming==='true'`（只恢复流式块）→ `streaming 或未完成`
            # （补 finish_tool_streaming 的完成态预览块）→ 本次的 DOM 存在性判据。
            "var _isFinished=(_finishedSet.indexOf(b.id)!==-1);"
            "if(!document.querySelector('[data-tool-call-id=\"'+b.id+'\"]')){"
            "var _t=document.createElement('div');_t.innerHTML=b.html;"
            "var _bk=_t.firstElementChild;if(_bk){"
            "_bk.removeAttribute('data-tool-injected');"
            "_bk.setAttribute('data-restored','true');"
            # _isFinished 不再参与恢复判定，仅作排查标记：恢复的块若属于「已完成」
            # （本该由 markdown 重建却没重建），打 data-restored-finished 供定位来源。
            "if(_isFinished)_bk.setAttribute('data-restored-finished','true');"
            # 🆕 F1：restore 恢复的运行中块（data-streaming="true"）直接 appendChild 沉底——
            # 不再按 data-order 插位。be57674d 方案 D 的按 data-order 插位逻辑本意是
            # 让"流式块恢复后保持交错顺序"，但运行中块 data-order 是调用时刻快照，
            # 与后续思考块补出的 data-order 尺度不一致 → 恢复插回时被排到思考块上方。
            # 运行中块语义为"当前最新活动"，dock 语义下应恒在最下面；data-order 属性
            # 仍保留（供 append_tool_result 完成态继承归位，不破坏"完成块归位"语义）。
            'var _odMatch=b.html.match(/data-order="([^"]*)"/);'
            "var _odVal=_odMatch?_odMatch[1]:null;"
            "if(_odVal){"
            "_bk.setAttribute('data-order',_odVal);"
            "}"
            "var _home=(b.src&&document.getElementById(b.src))||_tc;"
            "_home.appendChild(_bk);"
            "}}})"
            "}}"
            # 🐛 修复（流式滚动位置重置）：恢复钳制前的工具区滚动位置（打
            # _progScroll 吞掉恢复赋值触发的 scroll 事件，防止误判用户滚动），
            # 随后 _scrollToolContentToBottom 仍按跟随态决定是否拉底——阅读态
            # 保持原位，跟随态照常置底，两者不再互相覆盖。
            "if(_tc&&_tcPrevTop>0){"
            "var _tcMax=Math.max(0,_tc.scrollHeight-_tc.clientHeight);"
            "var _tcTarget=Math.min(_tcPrevTop,_tcMax);"
            "if(_tc.scrollTop!==_tcTarget){_progScroll(_tc,_tcTarget);}"
            "}"
            # 🐛 锚点复位：放在工具区自动滚底**之前**，跟随态仍由后者置底覆盖
            "if(typeof _endDomUpdate==='function')_endDomUpdate();"
            # 🐛 修复：save-restore 恢复块后工具区自动滚底
            "if(typeof _scrollToolContentToBottom==='function')_scrollToolContentToBottom();"
            "if(window._toolCompactMode){"
            "var _ts2=document.getElementById('tool-section');"
            "if(_ts2){"
            # [R1] display 切换空窗保护：none→'' 恢复时写回快照（此前钳制恢复的
            # scrollTop 否则停在 0），''→none 前更新快照供下次恢复。
            "var _r1Hidden=(_ts2.style.display==='none');"
            "if(!_r1Hidden&&_tc)_ts2._r1SavedTop=_tc.scrollTop;"
            "_ts2.style.display=(_tc&&_tc.children.length>0)?'':'none';"
            "if(_r1Hidden&&_ts2.style.display===''&&_tc){_progScroll(_tc,_ts2._r1SavedTop||0);}"
            "_updateToolSectionHeader();"
            "}"
            "}"
            "})();"
        )

    def _should_incremental_finalize(self) -> bool:
        """结束这一拍能否走差量收尾（稳定区 DOM 不动）。

        差量收尾要求「流式期间确实走过差量路径」（_stable_md_len > 0），且没有
        必须整页重排的 DOM 状态（活跃工具运行框 / 待注入工具）。任一不满足都
        回退全量，保证与旧行为一致。
        """
        if not INCREMENTAL_FINALIZE_ENABLED:
            return False
        if self._stable_md_len <= 0:
            # 流式期间一次差量都没走过（长段落无闭合段 / 一直在全量）→ 没有
            # 可保留的稳定区，收尾等价于整页重渲，直接走原路径。
            return False
        if getattr(self, "_injected_pending_tools", None):
            return False
        if self._has_active_tool_dom():
            return False
        return True

    def _try_incremental_finalize(self) -> bool:
        """差量收尾：只把「未差量消费的剩余 markdown」渲染上去，稳定区 DOM 不动。

        Returns:
            True = 已收尾（调用方直接返回，不再走全量）；False = 收尾失败，调用方
            回退全量终渲染，保证行为与旧路径一致。
        """
        try:
            md = self._markdown_text or ""
            new_html = ""
            tail_html = ""
            if md[self._stable_md_len :].strip():
                stable_len, segs = _extract_closed_segments(md[self._stable_md_len :])
                if segs:
                    new_html = "".join(_render_stable_segment(s, compact=self._tool_compact_mode) for s in segs)
                    self._stable_md_len += stable_len
                    self._stable_html += new_html
                rest = md[self._stable_md_len :]
                # 结束这一拍 md 已是终态：剩余部分整体行内渲染。若仍残留未闭合的
                # think/tool，说明内容不完整，放弃差量交给全量（宁可闪，不能缺内容）。
                if rest.strip():
                    if _has_unclosed_think_or_tool(rest):
                        return False
                    tail_html = _render_inline_tail(rest, compact=self._tool_compact_mode)
            # 流式思考块就地定稿：按 data-flip-key 的 ordinal 配对生成完成态 HTML
            replacements: dict[str, str] = {}
            for ordinal, content, closed in _iter_think_segments(md):
                if not closed:
                    continue
                replacements[str(ordinal)] = _render_think_block(
                    content, completed=True, compact=self._tool_compact_mode, flip_idx=ordinal
                )
            if not new_html and not tail_html and not replacements:
                return True  # 无待渲染内容：直接认定收尾完成，避免空转一次全量
            if new_html or tail_html:
                self.page().runJavaScript(
                    "updateContentAppend("
                    f"{json.dumps(new_html).decode('utf-8')},"
                    f"{json.dumps(tail_html).decode('utf-8')});"
                )
                # [T28] 差量收尾产物入图检测（与全量置位点同款，只置不清）
                self._mark_has_charts(new_html + tail_html)
            if replacements:
                self.page().runJavaScript(f"finalizeStreamingBlocks({json.dumps(replacements).decode('utf-8')});")
            self._height_report_pending = True
            return True
        except Exception as e:  # 差量收尾绝不能把消息卡在流式形态
            logger.debug(f"[incremental-finalize] 回退全量: {e}")
            return False

    def finish_streaming(self, keep_dock: bool = False, immediate: bool = True):
        """流式结束收尾。

        Args:
            keep_dock: True 时保留坞态（简洁模式下工具区仍沉底）——流式文本可能
                先于工具结果结束（S1：dock 归位早于工具完成），此时不应立即归位，
                等最后一个工具完成时再由 append_tool_result 兜底归位。
            immediate: False 时最终全量渲染不立即派发（仅启内部定时器），
                供批量加载路径错峰用（T11）。状态清理/JS 调用不受影响，
                仍全部同步执行。
        """
        self._streaming = False
        # 🆕 差量收尾判定必须在 [B1] 清空**前**取样：_should_incremental_finalize
        # 以 _stable_md_len > 0 为第二守卫，B1 先清零会让判定恒 False（env=1 下
        # 差量收尾静默失效）。走差量时 [B1] 不清 _stable_md_len/_stable_html，
        # 保留给 _perform_update 内 _try_incremental_finalize 作续写偏移；
        # 不走差量（判定 False）时照旧清空，全量路径基线干净。
        self._incremental_finalize = self._should_incremental_finalize()
        # [B1] 流式结束：差量缓存失效（尾部未闭合内容需全量渲染收尾），
        # 清空稳定区避免差量/全量混合导致重复段落。走差量时保留（见上）。
        self._needs_full_render = True
        if not self._incremental_finalize:
            self._stable_html = ""
            self._stable_md_len = 0
        # [B3] 流式结束：递增渲染序号使在途线程池任务过期（避免旧流式 HTML
        # 晚到覆盖最终非流式渲染结果）；pending 积压清空。
        self._render_seq += 1
        self._render_pending = None
        # [B2] 流式结束：重置工具 DOM 脏标记。随后 _schedule_render 走非流式分支，
        # 该分支依据 _tool_dom_dirty/_restore_finished_ids 决定 save/restore 或裸更新；
        # 显式清零保证完成渲染后不再残留"脏"状态（防误走整页 save/restore 包装）。
        # 🐛 修复（编辑工具框消失）：keep_dock=True 时仍有活跃工具（S1：文本先于
        # 工具结果流式结束），此时**不能**清理脏标记——否则 _schedule_render 走
        # 非流式裸更新重建 #content-placeholder，把 JS 注入的编辑工具运行框抹掉，
        # 直到 append_tool_result 才重现（"运行中→完成"中间消失一阵子）。保留
        # dirty 使最终渲染走 save/restore 保护（_saved 为空时零开销）。
        if not keep_dock and not getattr(self, "_injected_pending_tools", None):
            self._tool_dom_dirty = False
        # 流式结束：坞态归位（简洁模式下工具区从底部回到顶部）
        # 🆕 F2（S1）：keep_dock=True 时保留坞态——流式文本先于工具结果结束是
        # 常见时序（工具执行耗时 > 文本流式），此时立即归位会让用户看到
        # "工具还在运行但工具区已回顶部"的跳动。归位推迟到最后一个工具完成时。
        # [T30] 归位即折叠（collapse_after=True）：归位展开 → 折叠收起的两段式
        # 往返峰是结束态剧烈抖动主因，终态本就是折叠，同帧合并成一条曲线。
        # 非简洁模式由 JS 侧 _toolCompactMode 守卫自动 no-op。
        if not keep_dock:
            self._sync_streaming_dock(False, collapse_after=True)
        # 🐛 FIX: 流式结束时清除 tool_md_cache，防止缓存过期导致
        # 后续非流式渲染拿到缺内容的旧 <tool> markdown，造成 tool-block
        # 在 reorganizeContent 中因不匹配而被清除或生成重复。
        if hasattr(self, "_tool_md_cache"):
            self._tool_md_cache.clear()
        # 🆕 Bug B 方案 D+：流式结束必须清除"流式语义缓存"的 HTML。
        # _cached_streaming_html 是流式渲染产物：thinking 被渲染成 .think-streaming
        # （无 data-block-key，reorganizeContent 查不到 posMap → getPos=1e9 沉底）。
        # 若 finish 的非流式分支直接复用它，就会在"坞态归位/折叠框从底部移到上部"的
        # 最终渲染中，把思考块与 save/restore 插入的工具块错位（"所有思考在前、
        # 所有工具在后"）。清除后强制以完成态重新渲染（think-compact/think-block
        # 带稳定 data-block-key），与加载历史会话的排序尺度一致。
        self._cached_streaming_html = None
        self._processed_md_hash = 0
        self._cached_raw_md_hash = 0
        # 重置思考文本流式标志，防止下一轮对话误判
        self._think_text_streaming_started = False
        self._reasoning_streaming_started = False
        # 🆕 FLIP：最终全量重排会把工具/思考块从"流式态沉底"换成"完成态归位"，
        # arm 一个短窗口，让紧随其后的 updateContent 采集旧位置并补间成位移动画。
        # （未 arm 时 _flipCapture 直接返回 null，流式期间零额外布局开销。）
        try:
            if self._is_js_ready and self.page():
                self.page().runJavaScript("if(typeof window._flipArm==='function')window._flipArm(2000);")
        except RuntimeError:
            pass
        # ── 打点：记录"结束这一拍"的起点，首次高度上报时结算 JS 落地+布局耗时 ──
        # （render/dumps 只覆盖主线程，真正的 innerHTML 解析与重排在 WebEngine 侧，
        #  这一段只能靠"渲染派发 → 首个 reportHeight"的时间差来度量）
        if FINISH_TIMING_ENABLED:
            logger.info(
                f"[finish-render] begin md={len(self._markdown_text or '')} "
                f"finished_tools={len(getattr(self, '_restore_finished_ids', set()) or set())}"
            )
        self._finish_t0 = time.perf_counter()
        # 标记"接下来这次非流式渲染是流式结束的终渲染"：它必须同步完成
        # （紧随其后的 _cleanup_render_cache 会让异步结果过期），见 _perform_update。
        # （差量收尾判定已在函数开头 [B1] 前取样，见 _incremental_finalize。）
        self._final_render_pending = True
        # 流式结束：触发一次最终全量渲染，完成所有未完成的内容
        # 注意：不强制清除 _last_rendered_markdown —— 流式对话期间
        # think-streaming（展开）应保持，只有历史会话加载走非流式分支
        # 才会渲染为 think-block（折叠）。强制重渲染会把流式期间的
        # 展开态误转为折叠态，违背"流式展开 / 历史折叠"的产品预期。
        # immediate=False（T11 批量加载错峰）：交内部短定时器合并派发，
        # 避免 N 卡同帧全量重渲；状态清理与 JS 调用已在上方同步执行。
        if immediate:
            self._schedule_render(immediate=True)
        else:
            self._schedule_render(immediate=False)
        # 简洁模式：流式结束后自动折叠工具与思考区（收起为"工具与思考 · N 项"
        # 标题栏）。坞态归位 + 折叠由 MessageCard.finish_streaming 统一触发
        # （需 Python 端 _streaming/_has_active_tools 判据，viewer 侧无此状态，
        # 故不在此处调用）；非简洁模式保持流式结束后的展开态不变。

    def _auto_collapse_tool_section(self):
        """流式结束时自动折叠工具与思考区（仅简洁模式）

        在 dock 归位 + stop_streaming_anim 标完流式块后调用，收起为标题栏。
        调用方（MessageCard.finish_streaming / append_tool_result 兜底归位）
        已保证无活跃工具、非流式，故不做 DOM 流式块查询守卫——0ms 时序下
        最终渲染尚未落 DOM，陈旧的 data-streaming="true" 会误致跳过。
        非简洁模式保持展开态（与旧产品决策一致），直接 no-op。
        getattr 默认 True：stub viewer（测试桩）无该 property，视为简洁模式。
        """
        if not getattr(self, "_tool_compact_mode", True):
            return
        try:
            if self._is_js_ready and self.page():
                self.page().runJavaScript(
                    "(function(){"
                    "var _run=function(){"
                    "var _ts=document.getElementById('tool-section');"
                    "var _sep=document.getElementById('tool-separator');"
                    "if(_ts){"
                    "  if(typeof _beginToolSectionTransition==='function')_beginToolSectionTransition();"
                    "  _ts.setAttribute('data-collapsed','true');"
                    "  if(_sep)_sep.setAttribute('aria-expanded','false');"
                    "}};"
                    # 动画串行：折叠排在归位/重排的 FLIP 之后再跑，
                    # 避免「归位→重排→折叠」三段 200ms 过渡同时开跑造成掉帧与抖动。
                    "if(typeof window._animEnqueue==='function'){window._animEnqueue(_run,220);}else{_run();}"
                    "})();"
                )
        except RuntimeError:
            pass

    def _sync_streaming_dock(self, active: bool, collapse_after: bool = False):
        """同步流式活动坞状态到 JS 端。

        Args:
            active: True 进坞 / False 归位。
            collapse_after: [T30] 归位时同帧折叠工具区（仅简洁模式生效）。坞态
                归位本会把 #tool-content 从 220px 限高展开到自然高度，随后折叠
                又收回去 —— 两段式是结束态剧烈抖动的主因。True 时 JS 在归位
                事务内连置折叠属性，高度只走一条 220px→0 的曲线。
                非归位方向（active=True）忽略。

        仅简洁模式下 JS 侧 _setStreamingDock 会真正切换 body.streaming-dock，
        非简洁模式注入为空操作。JS 未就绪时跳过——_on_js_ready 会按当前
        _streaming / _is_history 状态兜底同步。
        """
        # 欢迎卡片（light 骨架）不进入坞态：坞态 CSS 会限死正文高度，
        # 欢迎卡片的长内容（会话列表/项目列表）会被截断在 330px。
        if self._light_skeleton:
            return
        try:
            if self._is_js_ready and self.page():
                flag = "true" if active else "false"
                collapse = "true" if (collapse_after and not active) else "false"
                self.page().runJavaScript(
                    f"if(typeof _setStreamingDock==='function')_setStreamingDock({flag},{collapse});"
                )
        except RuntimeError:
            pass

    def _cleanup_render_cache(self):
        """清理渲染缓存，降低内存占用（流式完成后调用）

        流式结束后清空 Python 端缓存字段，但保留 _lazy_markdown_cb 回调，
        以便主题切换或卡片复用时能从 MessageCard._content_data 按需重新生成
        _markdown_text，避免常驻两份等价的文本数据。

        🐛 修复：JS 未就绪时不清除 _lazy_markdown_cb，防止流式完成早于
        _on_js_ready 时丢失内容引用，导致卡片永久空白。
        """
        # [B3] 清空渲染缓存：递增序号使在途线程池任务过期（避免旧内容被应用）
        self._render_seq += 1
        self._render_pending = None
        # [B1] 清空差量缓存：强制下次全量渲染（流式结束后的最终态）
        self._needs_full_render = True
        self._stable_html = ""
        self._stable_md_len = 0
        self._last_rendered_html = None
        self._last_rendered_markdown = ""
        self._markdown_text = ""
        # 不再清除 _lazy_markdown_cb——主题切换 / 卡片复用需要按需从
        # MessageCard._content_data 重新生成 markdown，避免 2 份等价文本常驻。
        # 真正释放卡片时才由 cleanup() 统一置 None。

    @staticmethod
    def clear_global_cache():
        """类方法：清理模块级 LRU 渲染缓存"""
        clear_global_render_cache()

    def get_plain_text(self) -> str:
        """获取消息纯文本内容

        优先返回缓存的 _markdown_text（性能最优），
        若已被 _cleanup_render_cache 清空，则尝试从 _lazy_markdown_cb 重新生成，
        最后兜底从父级 MessageCard 获取 content_to_text 纯文本。
        """
        if self._markdown_text:
            return self._markdown_text
        # _markdown_text 被 _cleanup_render_cache 清空后的兜底
        if self._lazy_markdown_cb:
            try:
                fresh = self._lazy_markdown_cb()
                if fresh:
                    self._markdown_text = fresh
                    return fresh
            except Exception:
                pass
        # 从父 MessageCard 兜底
        p = self.parent()
        while p:
            if hasattr(p, "get_plain_text") and not isinstance(p, CodeWebViewer):
                try:
                    return p.get_plain_text()
                except Exception:
                    pass
                break
            p = p.parent()
        return ""

    def get_html(self) -> str:
        """获取消息的完整 HTML 页面（非流式/导出用）

        优先返回已缓存的 _last_rendered_html（含工具块等全量 DOM 等效 HTML），
        否则从 _markdown_text 或 _lazy_markdown_cb 重新生成。

        注意：_last_rendered_html 在流式渲染注入 JS 后会被清空以节省内存，
        因此导出时多数走 markdown→HTML 路径。
        """
        # 优先：已缓存的完整 HTML 直接返回（含工具展开块等，最完整）
        if self._last_rendered_html:
            return self._last_rendered_html
        # 次优：从 _markdown_text 转换
        md = self._markdown_text
        if not md and self._lazy_markdown_cb:
            try:
                md = self._lazy_markdown_cb()
                if md:
                    self._markdown_text = md
            except Exception:
                pass
        if md:
            return self._convert_md_to_html(md)
        return ""

    def _show_context_menu(self, pos):
        """显示大模型卡片右键菜单：查看差异、复制"""
        from app.utils.design_tokens import Colors

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {Colors.CARD_BG_SOLID};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 8px 32px 8px 12px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {scale_font_size(13)}px;
                {get_font_family_css()}
            }}
            QMenu::item:selected {{
                background-color: {Colors.HOVER_BG};
                border-radius: 4px;
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {Colors.BORDER};
                margin: 4px 8px;
            }}
        """)

        # 查看差异
        diff_action = menu.addAction(get_icon("差异对比"), "查看差异")
        diff_action.triggered.connect(self._request_view_diff)

        menu.addSeparator()

        # 复制
        # ⚠️ 必须 lambda 转接：QAction.triggered 带一个 bool(checked) 实参，
        # 直连 self._copy_to_clipboard 会把该 bool 灌进首个形参 copy_selection，
        # 默认值 True 被静默覆盖为 False → 右键永远复制全文（选区形同虚设）。
        copy_action = menu.addAction(get_icon("复制"), "复制")
        copy_action.triggered.connect(lambda checked=False: self._copy_to_clipboard(copy_selection=True))

        # 导出
        export_action = menu.addAction(get_icon("导入"), "导出")
        export_action.triggered.connect(self._export_message)

        # Phase D：插件右键菜单项（target="message_card"）
        context = {
            "round_index": getattr(self, "_round_index", None),
            "message_index": getattr(self, "_message_index", None),
            "window_id": self._resolve_window_id(),
        }
        self._current_context_menu = menu  # 供 action_func 返回 False 时关闭菜单
        self._inject_plugin_context_actions(menu, context)

        try:
            menu.exec_(self.mapToGlobal(pos))
        finally:
            self._current_context_menu = None

    def _resolve_window_id(self):
        """沿父链查找窗口 window_id（注入插件菜单 context 用）"""
        parent = self.parent()
        while parent is not None:
            wid = getattr(parent, "_window_id", None)
            if wid:
                return wid
            parent = parent.parent()
        return None

    def _inject_plugin_context_actions(self, menu: QMenu, context: dict):
        """注入消息卡片右键菜单插件项（Phase D，target="message_card"）

        action_func 返回 False 表示"处理完成关菜单"——与现有菜单项行为对齐：
        action_func 由插件实现，返回 False 时此处自动关闭菜单（menu.close()）。
        """
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            actions = UIPluginRegistry.get_instance().get_context_actions("message_card")
        except Exception:
            return
        for info in actions:
            try:
                if info.separator_before:
                    menu.addSeparator()
                action = menu.addAction(info.label)
                enabled = True
                if info.enabled_func is not None:
                    try:
                        enabled = bool(info.enabled_func(context))
                    except Exception:
                        enabled = True
                action.setEnabled(enabled)
                action.triggered.connect(lambda checked=False, i=info: self._run_plugin_context_action(i, context))
            except Exception as e:
                logger.warning(f"[MessageCard] 插件菜单项 {info.action_id} 注入失败：{e}")

    def _run_plugin_context_action(self, info, context: dict):
        """执行插件菜单项：action_func(context)；返回 False → 关闭菜单（保持现有语义）"""
        try:
            close_menu = info.action_func(context) is False
        except Exception as e:
            logger.error(f"[MessageCard] 插件菜单项 {info.action_id} 执行失败：{e}")
            close_menu = True
        if close_menu:
            try:
                menu = self._current_context_menu
                if menu is not None:
                    menu.close()
            except Exception:
                pass

    def _request_view_diff(self):
        """请求查看差异 - 向上查找 MessageCard 并发出 cardDiffRequested 信号"""
        parent = self.parent()
        while parent:
            if hasattr(parent, "cardDiffRequested"):
                # 通知父组件显示卡片差异
                if parent._round_index is not None and parent._message_index is not None:
                    parent.cardDiffRequested.emit(parent._round_index, parent._message_index)
                break
            parent = parent.parent()

    def _copy_to_clipboard(self, copy_selection: bool = True):
        """复制内容到剪贴板（使用系统原生 API）

        优先复制页面选中文本（右键菜单标准行为），无选中时降级复制全文。

        Args:
            copy_selection: True 时优先页面选中文本（右键菜单），False 跳过
                选区直接复制全文（工具栏复制按钮路径）。与 PlainTextViewer
                同名方法签名对齐（T41：MessageCard._copy_user_message 两态
                传参，签名不一致会在 CodeWebViewer 上 TypeError）。

        🐛 修复：使用 get_plain_text() 替代直接读 _markdown_text，
        因为 _cleanup_render_cache 会将 _markdown_text 清空。
        get_plain_text() 会通过 _lazy_markdown_cb 或父 MessageCard 自动兜底。
        """
        # 优先复制选中文本：QWebEnginePage.selectedText() 返回 DOM 选区，
        # 无选中时返回空字符串；\u2029 为 WebEngine 块级换行分隔符，规范化为 \n。
        try:
            selected = self.page().selectedText() if copy_selection else ""
            if selected:
                text = selected.replace("\u2029", "\n")
            else:
                text = self.get_plain_text()
        except Exception:
            text = self.get_plain_text()
        if not text:
            return
        # 收口走 Qt 剪贴板：旧实现的原生剪贴板 Open/Empty/Set/Close 序列无
        # try/finally 保护，异常时剪贴板句柄悬挂会触发 COM failfast
        # （2026-09-16 WER 口径 CoreMessaging 族诱因之一）。Qt 侧内部自带
        # 重试与句柄管理，行为对外等价。
        try:
            from PyQt5.QtWidgets import QApplication

            _clipboard = QApplication.clipboard()
            if _clipboard is None:
                raise RuntimeError("系统剪贴板不可用")
            _clipboard.setText(text)
        except Exception as e:
            logger.warning(f"复制到剪贴板失败: {e}")

    def _get_default_filename(self) -> str:
        """生成默认导出文件名：会话名_时间戳"""
        from datetime import datetime

        session_name = "消息"
        try:
            # 沿父链向上查找主窗口（self.window() 返回 ToolPopupDialog，没有 session_manager）
            parent_widget = self.parent()
            while parent_widget is not None:
                if hasattr(parent_widget, "session_manager"):
                    session = parent_widget.session_manager.get_current_session()
                    if session:
                        name = (session.topic_summary or session.name or "").strip()
                        if name:
                            session_name = name
                    break
                parent_widget = parent_widget.parent()
        except Exception:
            pass
        # 移除文件名非法字符
        invalid_chars = r'<>:"/\|?*'
        for c in invalid_chars:
            session_name = session_name.replace(c, "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{session_name}_{ts}"

    def _export_message(self):
        """导出消息为 Markdown、HTML 或 PNG 图片文件

        🐛 修复：使用 get_plain_text()/get_html() 替代直接读 _markdown_text，
        因为 _cleanup_render_cache 会将 _markdown_text 清空。
        get_plain_text() 会通过 _lazy_markdown_cb 或父 MessageCard 自动兜底。
        """
        from PyQt5.QtWidgets import QFileDialog

        default_name = self._get_default_filename()
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self, "导出消息", default_name, "PNG 图片 (*.png);;Markdown (*.md);;HTML (*.html)"
        )

        if not file_path:
            return

        try:
            is_png = "PNG" in selected_filter or file_path.lower().endswith(".png")
            is_html = "HTML" in selected_filter or file_path.lower().endswith(".html")
            if is_png:
                if not file_path.lower().endswith(".png"):
                    file_path += ".png"
                self._export_as_image(file_path)
            elif is_html:
                if not file_path.lower().endswith(".html"):
                    file_path += ".html"
                html_content = self.get_html()
                if not html_content:
                    logger.warning("导出 HTML 失败：无法获取消息内容")
                    self._show_save_error("无法获取消息内容")
                    return
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(html_content)
                logger.info(f"消息已导出到: {file_path}")
                self._show_save_success(file_path)
            else:
                if not file_path.lower().endswith(".md"):
                    file_path += ".md"
                md_content = self.get_plain_text()
                if not md_content:
                    logger.warning("导出 Markdown 失败：无法获取消息内容")
                    self._show_save_error("无法获取消息内容")
                    return
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(md_content)
                logger.info(f"消息已导出到: {file_path}")
                self._show_save_success(file_path)
        except Exception as e:
            logger.error(f"导出失败: {e}")
            self._show_save_error(str(e))

    def _run_js_sync(self, js_code: str, timeout_ms: int = 2000) -> str:
        """同步执行 JavaScript 并返回结果"""
        from PyQt5.QtCore import QEventLoop, QTimer

        page = self.page()
        if not page:
            return ""

        result = [None]
        loop = QEventLoop()

        def callback(val):
            result[0] = val
            if loop.isRunning():
                loop.quit()

        page.runJavaScript(js_code, callback)
        QTimer.singleShot(timeout_ms, lambda: loop.quit() if loop.isRunning() else None)
        loop.exec_()

        return result[0] or ""

    def _get_card_bg_color(self) -> "QColor":
        """沿父链查找 MessageCard，获取卡片背景色（强制实心化）

        PyQt5 的 QColor() 字符串构造不支持 "rgba(r, g, b, a)" 格式
        (isValid()=False)，需要手动解析提取 r/g/b 后用 QColor(r, g, b) 构造。
        """
        import re

        from PyQt5.QtGui import QColor

        parent = self.parent()
        while parent:
            if hasattr(parent, "_theme") and isinstance(parent._theme, dict) and "bg" in parent._theme:
                bg = parent._theme["bg"]
                # 1. 先试标准颜色字符串（#hex、named color 等）
                color = QColor(bg)
                if color.isValid():
                    color.setAlpha(255)
                    return color
                # 2. 兜底：手动解析 rgba(r, g, b[, a]) / rgb(r, g, b) 字符串
                m = re.match(
                    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d.]+\s*)?\)",
                    bg,
                )
                if m:
                    return QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                # 3. 主题色字符串无效且无法解析，跳出用兜底
                break
            parent = parent.parent()
        # 兜底：暗色主题背景
        return QColor("#2B2B2B")

    def _compose_with_solid_bg(self, source: "QPixmap", width: int, height: int, dpr: float = 1.0) -> "QPixmap":
        """在 QPixmap 上填充实心卡片背景，再合成 source（dpr>1 时输出高清物理像素）

        Args:
            source:  从 widget.grab() 拿到的 pixmap（可能含透明区）
            width:   目标逻辑宽度
            height:  目标逻辑高度
            dpr:     输出 devicePixelRatio（物理像素 = 逻辑 × dpr；<1 钳制为 1）

        Returns:
            填充实心卡片背景 + 绘制 source 的合成 pixmap
        """
        from PyQt5.QtGui import QPainter, QPixmap

        if width <= 0 or height <= 0:
            return source
        dpr = max(1.0, float(dpr))
        result = QPixmap(round(width * dpr), round(height * dpr))
        result.setDevicePixelRatio(dpr)
        result.fill(self._get_card_bg_color())
        if not source.isNull():
            painter = QPainter(result)
            painter.drawPixmap(0, 0, source)  # source 与 result 同 DPR 时按物理像素 1:1 绘制
            painter.end()
        return result

    def _grab_render_widget(self) -> "QPixmap":
        """抓取 WebEngine 渲染内容（GPU 合成环境下 QWebEngineView.grab() 拿不到内容）

        QWebEngineView 的内容由 Chromium 进程远程合成，QWidget::grab 抓自身
        往往得到空白/纯背景（Qt 已知限制）；实际渲染发生在内部 RenderWidget
        （focusProxy）上，必须对它 grab。拿不到 focusProxy 或结果为空时回退
        QWidget 原生 grab（软件渲染环境该路径可用）。
        """
        target = self.focusProxy()
        if target is not None:
            try:
                pix = target.grab()
                if pix is not None and not pix.isNull() and pix.width() > 0 and pix.height() > 0:
                    return pix
            except Exception:
                logger.warning("[export] RenderWidget grab 为空，回退 QWidget grab")
        return super().grab()

    def _capture_looks_healthy(self, pix: "QPixmap") -> bool:
        """粗采样检查抓取结果：整体内容占比过低或出现大面积连续空白块视为合成未完成

        zoom 3x 撑大控件后 WebEngine 走异步合成，弱 GPU/大纹理时定时等待可能
        不够——合成器未画完的 tile 抓出来是纯背景色。粗采样 ≤64×64 点毫秒级。

        两级判据（部分渲染的"窄条内容"能骗过整体占比，拦不住连续空块）：
        1. 整体非背景采样点占比 ≥ 1%
        2. 8×8 分块中"整块皆背景"的空块占比 < 50%（正常内容散布多数块，
           部分渲染会出现大段连续空白）
        """

        img = pix.toImage()
        if img.isNull():
            return False
        bg = self._get_card_bg_color()
        w, h = img.width(), img.height()
        step = max(4, min(w, h) // 64)
        grid_n = 64  # 采样网格 64×64 点
        cols = max(1, (w + step - 1) // step)
        if cols > grid_n:
            cols = grid_n
        rows = max(1, (h + step - 1) // step)
        if rows > grid_n:
            rows = grid_n
        total = 0
        diff = 0
        block_size = 8  # 每 8×8 采样点为一块
        block_empty = [0] * ((grid_n // block_size + 1) * (grid_n // block_size + 1))
        yi = 0
        y = 0
        while y < h:
            x = 0
            xi = 0
            while x < w:
                c = img.pixelColor(x, y)
                total += 1
                is_bg = abs(c.red() - bg.red()) + abs(c.green() - bg.green()) + abs(c.blue() - bg.blue()) <= 24
                if not is_bg:
                    diff += 1
                else:
                    bi = (yi // block_size) * (grid_n // block_size + 1) + (xi // block_size)
                    block_empty[bi] += 1
                x += step
                xi += 1
            y += step
            yi += 1
        if total == 0:
            return False
        if (diff / total) < 0.01:
            return False
        # 分块：块内采样点全部为背景 → 空块
        bw = grid_n // block_size + 1
        empty_blocks = 0
        total_blocks = 0
        for r in range(min(rows, grid_n) // block_size + (1 if min(rows, grid_n) % block_size else 0)):
            for c in range(min(cols, grid_n) // block_size + (1 if min(cols, grid_n) % block_size else 0)):
                total_blocks += 1
                if block_empty[r * bw + c] == block_size * block_size:
                    empty_blocks += 1
        if total_blocks == 0:
            return False
        return (empty_blocks / total_blocks) < 0.5

    def _wait_render_stable(self, deadline_ms: int = 1200) -> None:
        """轮询等待 WebEngine 布局/合成稳定：body.scrollHeight 连续两次读数一致即放行

        替代固定 250ms 定时——大尺寸重排（zoom 3x 撑到 4K+）时固定等待可能不足，
        或小页面白白等满。最长 deadline_ms 兜底，卡死不可能（_run_js_sync 有超时）。
        """
        import json as json_mod

        from PyQt5.QtCore import QElapsedTimer, QEventLoop, QTimer

        loop = QEventLoop()
        elapsed = QElapsedTimer()
        elapsed.start()
        state = {"last": None, "stable": 0}

        def _tick():
            try:
                raw = self._run_js_sync("JSON.stringify({sh: document.body ? document.body.scrollHeight : 0})")
                cur = json_mod.loads(raw).get("sh", 0) if raw else 0
            except Exception:
                cur = 0
            if cur == state["last"]:
                state["stable"] += 1
            else:
                state["stable"] = 0
            state["last"] = cur
            # 至少 240ms（2 tick）且读数连续两次一致 → 布局稳定
            if state["stable"] >= 2 and elapsed.elapsed() >= 240:
                loop.quit()
                return
            if elapsed.elapsed() >= deadline_ms:
                loop.quit()
                return

        timer = QTimer()
        timer.setInterval(120)
        timer.timeout.connect(_tick)
        timer.start()
        try:
            loop.exec_()
        finally:
            timer.stop()

    def _capture_full_content_1x(self) -> "QPixmap":
        """1x 兜底抓取（旧逻辑完整保留）：解除 max-height 撑高后单次 grab + 实心合成"""
        import json as json_mod

        from PyQt5.QtCore import QEventLoop, QTimer
        from PyQt5.QtWidgets import QApplication

        view_w = self.width()
        cur_h = self.height()

        dims_raw = self._run_js_sync("JSON.stringify({sh: document.body.scrollHeight})")
        if not dims_raw:
            return self._compose_with_solid_bg(self._grab_render_widget(), view_w, cur_h)

        try:
            scroll_h = json_mod.loads(dims_raw).get("sh", 0)
        except Exception:
            scroll_h = 0

        if scroll_h <= cur_h or scroll_h <= 0:
            grabbed = self._grab_render_widget()
            return self._compose_with_solid_bg(
                grabbed,
                view_w,
                max(cur_h, grabbed.height() if not grabbed.isNull() else cur_h),
            )

        old_styles = self._run_js_sync("""
            var s = document.body.style;
            JSON.stringify({maxHeight: s.maxHeight, overflowY: s.overflowY})
        """)
        self._run_js_sync("""
            document.body.style.maxHeight = 'none';
            document.body.style.overflowY = 'hidden';
        """)

        orig_height = self.height()
        target_h = scroll_h + 20
        self.setFixedHeight(target_h)
        self.update()
        QApplication.processEvents()

        self._run_js_sync("window.scrollTo(0, 0);")

        stable_loop = QEventLoop()
        QTimer.singleShot(200, stable_loop.quit)
        stable_loop.exec_()

        full_pix = self._grab_render_widget()

        final_w = full_pix.width() if not full_pix.isNull() else view_w
        final_h = max(target_h, full_pix.height() if not full_pix.isNull() else 0)
        result = self._compose_with_solid_bg(full_pix, final_w, final_h)

        self.setFixedHeight(orig_height)
        if old_styles:
            try:
                prev = json_mod.loads(old_styles)
                js_restore = f"""
                    document.body.style.maxHeight = {json_mod.dumps(prev.get("maxHeight", ""))};
                    document.body.style.overflowY = {json_mod.dumps(prev.get("overflowY", "auto"))};
                    window.scrollTo(0, 0);
                """
                self._run_js_sync(js_restore)
            except Exception:
                self._run_js_sync("window.scrollTo(0, 0);")

        if result.isNull() or result.width() <= 0 or result.height() <= 0:
            return self._grab_render_widget()
        return result

    def _capture_full_content(self) -> "QPixmap":
        """截取消息的完整内容为一张高清大图（3x 物理像素 + 实心背景合成）

        策略：临时 setZoomFactor(3) 并把控件尺寸×3（布局视口 CSS 宽度 = w*3/3 = w
        不变，排版不重排，内容以 3x 物理像素渲染），grab 后按实际物理/逻辑比设置
        devicePixelRatio 还原逻辑尺寸。导出 PNG 保存物理像素，高分屏/放大查看均清晰。

        长消息：临时解除 body max-height 并撑高到完整内容高度后单次 grab。
        稳健性：渲染稳定用 scrollHeight 轮询（非固定定时）；grab 后做像素健康
        检查——3x 结果大面积空白/黑块（合成未完成或视口重排异常）时自动回退
        1x 完整路径，保证导出功能永不失败。
        """
        import json as json_mod

        from PyQt5.QtWidgets import QApplication

        _SCALE = 3.0

        view_w = self.width()
        cur_h = self.height()
        if view_w <= 0:
            return self._compose_with_solid_bg(self._grab_render_widget(), max(1, view_w), cur_h)

        # 1. 获取完整内容高度（CSS 逻辑像素，与 zoom 无关）
        dims_raw = self._run_js_sync("JSON.stringify({sh: document.body.scrollHeight})")
        scroll_h = 0
        if dims_raw:
            try:
                scroll_h = json_mod.loads(dims_raw).get("sh", 0)
            except Exception:
                scroll_h = 0
        if scroll_h <= 0:
            # 拿不到高度 → 按当前视口高度走 zoom 高清路径
            scroll_h = cur_h

        # 2. 目标逻辑高度：内容超出视图时展开全部
        is_long = scroll_h > cur_h
        target_logical_h = (scroll_h + 20) if is_long else cur_h

        orig_zoom = self.zoomFactor()
        orig_size = self.size()
        old_styles = None
        try:
            # 3. 长消息：临时解除 body max-height
            if is_long:
                old_styles = self._run_js_sync("""
                    var s = document.body.style;
                    JSON.stringify({maxHeight: s.maxHeight, overflowY: s.overflowY})
                """)
                self._run_js_sync("""
                    document.body.style.maxHeight = 'none';
                    document.body.style.overflowY = 'hidden';
                """)

            # 4. zoom 3x + 控件尺寸×3：内容物理渲染 3x，布局视口 CSS 宽度不变
            self.setZoomFactor(_SCALE)
            self.setFixedSize(round(view_w * _SCALE), round(target_logical_h * _SCALE))
            self.update()
            # ★ 强制布局：让 setFixedSize 真的撑大 widget
            QApplication.processEvents()
            self._run_js_sync("window.scrollTo(0, 0);")

            # ★ 轮询等待 zoom 重排 + 合成稳定（大纹理时固定 250ms 不够）
            self._wait_render_stable(deadline_ms=1200)

            # 5. 显式 grab 整个目标区域，按实际物理/逻辑比还原逻辑尺寸
            full_pix = self._grab_render_widget()
            if full_pix.isNull() or full_pix.width() <= 0:
                logger.warning("[export] zoom 3x 抓到空图，回退 1x")
            elif not self._capture_looks_healthy(full_pix):
                logger.warning("[export] zoom 3x 抓取疑似未完成合成（大面积空白），回退 1x")
            else:
                dpr = full_pix.width() / view_w  # 物理/逻辑（= 窗口 DPR × zoom）
                final_h = max(target_logical_h, round(full_pix.height() / dpr))
                return self._compose_with_solid_bg(full_pix, view_w, final_h, dpr=dpr)
        except Exception:
            logger.exception("[export] zoom 3x 抓取失败，回退 1x")
        finally:
            # 6. 恢复 zoom / 尺寸 / 样式
            try:
                self.setZoomFactor(orig_zoom)
                self.setFixedSize(orig_size)
            except Exception:
                pass
            if old_styles:
                try:
                    prev = json_mod.loads(old_styles)
                    js_restore = f"""
                        document.body.style.maxHeight = {json_mod.dumps(prev.get("maxHeight", ""))};
                        document.body.style.overflowY = {json_mod.dumps(prev.get("overflowY", "auto"))};
                        window.scrollTo(0, 0);
                    """
                    self._run_js_sync(js_restore)
                except Exception:
                    self._run_js_sync("window.scrollTo(0, 0);")

        # 7. 回退：1x 完整路径（健康检查通过才返回，异常仍有裸 grab 兜底）
        return self._capture_full_content_1x()

    def _split_and_stitch(self, pixmap: "QPixmap", max_cols: int = 6) -> "QPixmap":
        """将纵向长图均匀分段后水平拼接为宽高合理的矩形图

        把 pixmap 按高度均匀切成 N 段，从左到右水平拼接。
        N 的选择使最终拼接图的宽高比尽量接近 3:2。
        """
        from PyQt5.QtGui import QPainter, QPixmap

        w = pixmap.width()
        h = pixmap.height()
        if w <= 0 or h <= 0:
            return pixmap

        # 计算最佳列数：使拼接后的宽高比接近目标比例
        target_ratio = 1.5  # 3:2
        best_cols = 1
        best_diff = float("inf")

        for cols in range(2, min(max_cols + 1, (h + w - 1) // w + 1)):
            strip_h = h / cols
            ratio = (cols * w) / strip_h
            diff = abs(ratio - target_ratio)
            if diff < best_diff:
                best_diff = diff
                best_cols = cols

        if best_cols <= 1:
            return pixmap

        # 均匀切分（最后一段包含余量）
        strip_h = h // best_cols
        segments = []
        for i in range(best_cols):
            y = i * strip_h
            if i == best_cols - 1:
                seg = pixmap.copy(0, y, w, h - y)
            else:
                seg = pixmap.copy(0, y, w, strip_h)
            if not seg.isNull():
                segments.append(seg)

        if len(segments) <= 1:
            return pixmap

        # 水平拼接
        total_w = sum(s.width() for s in segments)
        max_h = max(s.height() for s in segments)
        result = QPixmap(total_w, max_h)
        painter = QPainter(result)
        x = 0
        for seg in segments:
            painter.drawPixmap(x, 0, seg)
            x += seg.width()
        painter.end()

        return result

    def _export_as_image(self, file_path: str):
        """将当前消息内容导出为 PNG 图片（全内容截取 + 智能拼接）"""

        # 1. 截取全内容大图
        full = self._capture_full_content()
        if full.isNull():
            raise RuntimeError("截图生成失败，无法获取渲染内容")

        # 2. 若内容超出视图高度，均匀分段后水平拼接为矩形图
        if full.height() > full.width() * 1.5:
            result = self._split_and_stitch(full)
        else:
            result = full

        result.save(file_path, "PNG")
        logger.info(f"消息已导出为图片: {file_path}")
        self._show_save_success(file_path)

    def _convert_md_to_html(self, markdown_text: str) -> str:
        """将 Markdown 文本转换为独立 HTML 页面"""
        from markdown import Markdown

        md = Markdown(extensions=["fenced_code", "codehilite", "tables"])
        body_html = md.convert(markdown_text)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>消息导出</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; color: #333; }}
        pre {{ background: #f5f5f5; padding: 12px; border-radius: 6px; overflow-x: auto; }}
        code {{ background: #f0f0f0; padding: 2px 4px; border-radius: 3px; font-size: 0.9em; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; }}
        img {{ max-width: 100%; }}
        blockquote {{ border-left: 4px solid #ddd; margin-left: 0; padding-left: 16px; color: #666; }}
        h1, h2, h3, h4 {{ margin-top: 24px; }}
    </style>
</head>
<body>
{body_html}
</body>
</html>"""

    def _show_save_success(self, file_path: str):
        """显示保存成功提示"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition

            main_window = self.window()
            if main_window:
                InfoBar.success(
                    "文件已导出",
                    file_path,
                    duration=3000,
                    parent=main_window,
                    position=InfoBarPosition.BOTTOM,
                )
        except Exception:
            pass

    def _show_save_error(self, error_msg: str):
        """显示保存失败提示"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition

            main_window = self.window()
            if main_window:
                InfoBar.error(
                    "导出失败",
                    error_msg,
                    duration=3000,
                    parent=main_window,
                    position=InfoBarPosition.BOTTOM,
                )
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._streaming:
            return

        # 性能优化：resize 锁（trailing debounce）——每次 resize 都续期，
        # 保证「最后一次 resize 之后 150ms」才上报高度。
        # 🐛 旧实现只在 `not _resize_locked` 时上锁（即只有第一次 resize 生效），
        # 锁的到期时刻由**第一次** resize 决定：其后发生的高度变化全被吞掉，
        # 且多张卡的解锁时刻彼此错开 → 高度分批到达 → 容器总高被逐张修改。
        self._resize_locked = True
        self._resize_unlock_timer.stop()
        self._resize_unlock_timer.start()

    def wheelEvent(self, event: QWheelEvent):
        # 内部 PlainTextViewer(QWidget) 本身不可滚动，始终转发到外部。
        # 转发外层滚动区走 qfluentwidgets SmoothScroll，与卡片间隙滚动同款平滑手感。
        # ⚠️ 宿主层级可变（曾直挂卡片，2026-09-23 起移入 assistant 气泡），
        # 不能写死 parent().parent() 层数 —— 层数变了 _parent 取不到，
        # AttributeError 被吞后滚轮静默失效。沿父链找持 _parent 的 MessageCard。
        try:
            node = self.parent()
            card = None
            while node is not None:
                if hasattr(node, "_parent"):
                    card = node
                    break
                node = node.parent()
            scroll_area = card._parent.chat_scroll_area if card is not None else None
            if scroll_area:
                vbar = scroll_area.verticalScrollBar()
                if vbar and vbar.minimum() != vbar.maximum() and event.angleDelta().y() != 0:
                    scroll_area.wheelEvent(event)
                    event.accept()
                    return
        except Exception:
            pass
        super().wheelEvent(event)

    def cleanup(self):
        """
        清理 CodeWebViewer 持有的资源，防止内存泄漏。
        应该在删除 viewer 前调用，或者在 deleteLater 中自动调用。
        """
        # 🔧 内存修复：从全局单例过滤器注销，防止注册表持有对已销毁
        # CodeWebViewer 实例的引用，导致 GC 无法回收且事件循环误调用已释放对象
        _dialog_event_filter.unregister(self)

        # [B3] 视口销毁：递增渲染序号使在途线程池任务过期（weakref 判活兜底下，
        # 序号守卫提供第二道防线，防止旧任务结果应用到已释放的 DOM）。
        self._render_seq += 1
        self._render_pending = None
        self._render_inflight = False

        # 停止所有定时器
        timers_to_stop = [
            self._render_timer,
            self._resize_unlock_timer,
        ]
        for timer in timers_to_stop:
            try:
                timer.stop()
                timer.deleteLater()
            except RuntimeError:
                pass

        # 断开所有信号连接
        try:
            if hasattr(self._page, "codeActionRequested"):
                self._page.codeActionRequested.disconnect()
            if hasattr(self._page, "contextActionRequested"):
                self._page.contextActionRequested.disconnect()
            if hasattr(self._page, "heightReported"):
                self._page.heightReported.disconnect()
            if hasattr(self._page, "contentReady"):
                self._page.contentReady.disconnect()
            if hasattr(self._page, "toolDiffRequested"):
                self._page.toolDiffRequested.disconnect()
            if hasattr(self._page, "subAgentLogRequested"):
                self._page.subAgentLogRequested.disconnect()
            if hasattr(self._page, "saveFileRequested"):
                self._page.saveFileRequested.disconnect()
        except Exception:
            pass

        # 清理流式输出和渲染缓存
        self._streaming = False
        self._markdown_text = ""
        self._last_rendered_html = ""
        self._last_rendered_markdown = ""
        self._processed_md_hash = None
        self._cached_streaming_html = None
        self._cached_raw_md_hash = 0
        self._lazy_markdown_cb = None  # 清理懒回调引用，释放 content_data
        self._is_js_ready = False

        # 清理上下文状态
        self._context_lost = False
        self._height_report_pending = False
        self._resize_locked = False

        # 清理页面：先停加载并卸载到空白页（比 setHtml("") 更轻，避免 WebEngine 异步导航竞态）
        try:
            self.stop()  # 停止页面加载
            from PyQt5.QtCore import QUrl

            self.setUrl(QUrl("about:blank"))  # 卸载，比 setHtml("") 更轻
        except RuntimeError:
            pass
        # 幂等守卫：二次 cleanup 不重复 deleteLater
        if getattr(self, "_page", None) is not None:
            self._page.deleteLater()
            self._page = None
        self.setPage(None)  # 断开 view→page，避免 view 析构再引用已删 page

        # 共享 profile 为全局单例，不可销毁；仅解除引用。
        # page 已在上方单独 deleteLater 释放渲染资源（DOM/JS heap/图层）。
        if hasattr(self, "_profile"):
            self._profile = None

        # 清理代码块缓存
        if hasattr(self, "_code_block_cache"):
            self._code_block_cache.clear()
            self._code_block_cache = None

        # 清理滚动位置
        self._last_scroll_position = 0

        # [B4-强回收] 防悬挂：清理时清零 renderer PID（进程可能已随页面销毁退出）
        self._renderer_pid = 0
        # [T6] 销毁前摘出主题补渲队列（weakref 失效亦会兜底，这里显式摘除）
        _theme_rerender_queue.discard(self)

    def deleteLater(self):
        self.cleanup()
        super().deleteLater()


class _ThemeRerenderQueue:
    """主题切换正文重渲分帧队列（T6 错峰）

    主题切换触发的 CodeWebViewer 全量 markdown 重渲（单卡 40~120ms 主线程
    阻塞，见 _perform_update 非流式分支注释）原先在 MessageCard.refresh_theme
    里逐卡 immediate 执行，N 卡同拍 = 240~700ms 主线程冻结。改为「脏标记 +
    分帧队列」：每帧最多补渲 _FRAME_BUDGET_CARDS 张，总耗时不变但不再单拍
    卡死（对齐 main_widget._UI_PLUGIN_FRAME_BUDGET_MS 分帧先例）。

    - 流式卡不入队（MessageCard.refresh_theme 判断）：refresh_theme 已置
      _needs_full_render=True，下一次流式节拍自然全量带新主题
    - 消费时 viewer 不可见：_perform_update 的 V1 门控置 _render_deferred，
      showEvent 补渲链路兜底（隐藏卡不丢主题态、不丢正文）
    - viewer 池化复用/销毁：_reset_for_reuse / cleanup 调 discard 摘队
    """

    _FRAME_BUDGET_CARDS = 2  # 每帧最多补渲张数（可见优先）
    _FRAME_INTERVAL_MS = 16  # ≈60fps 一帧

    def __init__(self):
        # id(viewer) → weakref；dict 保序，插入序 ≈ 入队序
        self._pending: "dict[int, weakref.ref]" = {}
        self._timer: Optional[QTimer] = None

    def enqueue(self, viewer) -> None:
        """登记待补渲 viewer（幂等，已入队的不重复登记）"""
        key = id(viewer)
        if key in self._pending:
            return
        self._pending[key] = weakref.ref(viewer)
        if self._timer is None:
            # 无 parent QTimer：队列清空时 deleteLater 显式释放（对齐
            # main_widget._theme_batch_timer 的生命周期管理写法）
            self._timer = QTimer()
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._drain)
        self._timer.start(self._FRAME_INTERVAL_MS)

    def discard(self, viewer) -> None:
        """viewer 被池化复用/销毁前摘队"""
        self._pending.pop(id(viewer), None)

    def _drain(self) -> None:
        """帧回调：可见优先取最多 _FRAME_BUDGET_CARDS 张执行补渲"""
        if self._timer is not None:
            self._timer.stop()
        if not self._pending:
            if self._timer is not None:
                self._timer.deleteLater()
                self._timer = None
            return

        def _visible_first(kv):
            v = kv[1]()
            return 0 if v is not None and v.isVisible() else 1

        entries = sorted(self._pending.items(), key=_visible_first)
        consumed = 0
        for key, ref in entries:
            if consumed >= self._FRAME_BUDGET_CARDS:
                break
            viewer = ref()
            self._pending.pop(key, None)
            if viewer is None:
                continue  # C++ 对象已销毁：顺手清理
            viewer._theme_render_pending = False
            consumed += 1
            try:
                # 与原 refresh_theme immediate 路径同一原语：置全量标记 +
                # _schedule_render(immediate=True)；不可见时 _perform_update
                # 内部 V1 门控转 _render_deferred，由 showEvent 补渲
                viewer._refresh_viewer_font()
            except RuntimeError:
                continue
            except Exception as e:
                logger.warning(f"[ThemeRerenderQueue] 补渲失败 {type(viewer).__name__}: {e}")
        if self._pending:
            self._timer.start(self._FRAME_INTERVAL_MS)
        else:
            self._timer.deleteLater()
            self._timer = None


# 模块级单例：消息卡主题补渲共用一条分帧队列
_theme_rerender_queue = _ThemeRerenderQueue()


class PlainTextViewer(QWidget):
    contentHeightChanged = pyqtSignal(int)

    # 用户消息卡片最大高度（px）：超过此高度启用 QTextEdit 内部滚动条
    # 约可容纳 13 行 14px 文本，平衡阅读完整性与卡片视觉占位
    #
    # ⚠️ 用户明确要求保持 300（2026-08-30）：用户气泡不应因正文变长而撑开整屏，
    # 长内容在气泡内部滚动是**预期行为**，不是缺陷。不要为了「减少滚动区域」
    # 擅自抬高它 —— 本轮滚动体验改动只针对 assistant 卡片与自动滚底守卫。
    MAX_HEIGHT = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        # 气泡宽度自适应：未换行内容理想宽度（ChatGPT 式紧凑气泡），
        # 由 MessageCard.sync_width 按容器宽度注入上限
        # PyQt5 未导出 QWIDGETSIZE_MAX，16777215 即其值（未 sync 前的不限制初始态）
        self._width_cap = 16777215
        # [PERF] “超高”单调缓存：全文档实测高度撞上 MAX_HEIGHT 上限时的最大确认宽度（0=未确认）。
        # 文档高度在某宽度撞上限后，宽度变窄只会行数更多、高度更高，故后续宽度 ≤ 该值时
        # 可直接 O(1) 判定 (cap, MAX_HEIGHT)，跳过全文档重排——消除超大用户消息的 resize 卡顿。
        # 文本替换（set_text）使缓存失效；append_chunk 只增不减，无需失效。
        self._tall_cap = 0
        self._init_ui()
        # 性能优化：添加 resize 防抖定时器
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(50)  # 50ms 防抖
        self._resize_debounce_timer.timeout.connect(self._do_resize_update)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        # 底部 2：正文与时间行间距紧凑（时间行在卡片 footer，不在 viewer 内）
        layout.setContentsMargins(8, 6, 8, 2)
        layout.setSpacing(0)

        self.text_edit = QTextEdit(self)
        self.text_edit.setReadOnly(True)
        self.text_edit.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_edit.setFrameShape(QTextEdit.NoFrame)
        self.text_edit.setContextMenuPolicy(Qt.CustomContextMenu)
        self.text_edit.customContextMenuRequested.connect(self._show_context_menu)
        # 显式声明：超出可视区域时自动显示垂直滚动条
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 气泡内禁横向滚动：超宽行强制软换行（达上限自动折行，不出横向滚动条）
        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_edit.setLineWrapMode(QTextEdit.WidgetWidth)
        self._apply_text_style()
        # 把 QTextEdit 内部滚轮劫持到 qfluentwidgets SmoothScroll 引擎——
        # 与外层 chat_scroll_area（SingleDirectionScrollArea）走同款 400ms
        # 插值、stepRatio 1.5、连滚加速。手感统一，消除"卡内跳、卡间滑"的异样。
        # CodeWebViewer 的 Chromium 内部滚动是引擎边界（事件被子进程拦截），
        # 维持原状，边界放行靠外层 MessageCard.wheelEvent 接管。
        SmoothScrollDelegate(self.text_edit)
        layout.addWidget(self.text_edit)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(40)
        # 用户消息卡片最大高度：超出后由 QTextEdit 内部滚动条处理滚动
        self.setMaximumHeight(self.MAX_HEIGHT)

    def _apply_text_style(self):
        """应用文本样式（从 Colors token 读取颜色）

        滚动条复用项目统一的 get_unified_scrollbar_style，与
        tab_panel / project_selector / settings 等列表的视觉风格保持一致。
        """
        font_css = get_font_family_css()
        text_color = Colors.USER_CARD_TEXT
        self.text_edit.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                border: none;
                {font_css}
                color: {text_color};
                font-size: {scale_font_size(14)}px;
                line-height: 1.5;
                selection-background-color: rgba(102, 198, 255, 0.28);
            }}
            {get_unified_scrollbar_style(6)}
        """)

    def refresh_theme(self):
        """主题切换后刷新文本颜色"""
        self._apply_text_style()
        # [PERF] 字体/字号可能随主题设置变化 → 行数结论失效，清除“超高”缓存待重测
        self._tall_cap = 0

    def append_chunk(self, text: str):
        self._text += text
        self.text_edit.setPlainText(self._text)
        # 设置文档宽度以确保正确计算换行
        vp_width = self.text_edit.viewport().width()
        if vp_width > 0:
            self.text_edit.document().setTextWidth(vp_width)
        self._schedule_update_height()

    def finish_streaming(self, keep_dock: bool = False, immediate: bool = True):
        """流式结束收尾。

        🆕 F4：与 CodeWebViewer.finish_streaming 保持相同签名——MessageCard.
        finish_streaming 统一以 keep_dock=self._has_active_tools() 调用两个 Viewer。
        PlainTextViewer 无 dock 概念（用户卡片无工具与思考折叠框），忽略该参数。

        [P0 修复] immediate 为接口对齐参数（兄弟团队 T11 错峰链给 CodeWebViewer
        加了该参数并在 MessageCard 无条件传参，此处漏改导致鸭子类型断裂）。
        PlainTextViewer 是同步纯 Qt 渲染，无 WebEngine JS 投递可择机，错峰无意义，
        故接收后忽略：立即 _schedule_update_height() 即等价于 immediate=True 语义。
        """
        self._schedule_update_height()

    def _schedule_update_height(self):
        """🛡️ 安全的延迟高度更新

        使用 lambda 包装 + try/except 保护，防止 PlainTextViewer 被 deleteLater()
        销毁后定时器回调仍访问已释放的 C++ 对象（text_edit）导致段错误。
        """
        QTimer.singleShot(10, lambda: self._safe_update_height())

    def _safe_update_height(self):
        """带存活性检查的 _update_height"""
        try:
            # 检查 C++ 对象是否已被销毁（text_edit 可能在懒创建前为 None）
            if self.text_edit is None:
                return
            if sip.isdeleted(self.text_edit):
                return
            self._update_height()
        except RuntimeError:
            pass

    def get_plain_text(self) -> str:
        return self._text

    def set_text(self, text: str):
        self._text = text
        # [PERF] 文本被整体替换（可能变短）→ “超高”结论不再必然成立，失效单调缓存
        self._tall_cap = 0
        self.text_edit.setPlainText(text)
        # 设置文档宽度以确保正确计算换行
        vp_width = self.text_edit.viewport().width()
        if vp_width > 0:
            self.text_edit.document().setTextWidth(vp_width)
        self._schedule_update_height()

    def set_width_cap(self, cap: int):
        """设置气泡最大宽度（由 MessageCard.sync_width 按容器宽度注入）"""
        cap = max(60, int(cap))
        if cap != self._width_cap:
            self._width_cap = cap
            self._schedule_update_height()

    def _definitely_tall_chars(self) -> int:
        """[PERF] “必然超高”字符阈值（O(1) 计算）。

        内容达到该字符数时，即使按最乐观排版估算——窗口宽至 4000px、字符窄至
        0.35×字号——渲染高度也必然撞上 MAX_HEIGHT 上限。超过阈值即可跳过全文档
        布局直接判定 (cap, MAX_HEIGHT)。误判方向只会让气泡偏高留白（内部滚动条仍可见
        全文），不会裁剪文字。
        """
        fs = self.text_edit.font().pixelSize()
        if fs <= 0:
            fs = 14
        # 填满 MAX_HEIGHT 所需行数（行高 1.5×字号）× 4000px 宽下每行最多容纳字符数
        return int((self.MAX_HEIGHT / (1.5 * fs)) * (4000.0 / (0.35 * fs)))

    def _measure_document(self) -> QTextDocument:
        """新建独立测量文档（同步布局，字体/边距与 text_edit 对齐）。

        text_edit 的共享文档会被 QTextEdit 钉在 viewport 宽，且 QTextDocument
        布局是 layoutTimer 异步的——setTextWidth(w) 后立即读 size() 拿到旧宽
        排版缓存，短消息被误判"多行"钉死 MAX_HEIGHT（气泡下方大片空白根因）。
        独立新文档无历史布局状态，size() 同步正确。调用方用完交由 GC 释放。
        """
        src = self.text_edit.document()
        doc = QTextDocument()
        doc.setDefaultFont(src.defaultFont())
        doc.setDocumentMargin(src.documentMargin())
        return doc

    def _update_height(self):
        """宽度自适应 + 高度重算：气泡按未换行理想宽度收缩，不占满整行"""
        # [PERF] 超大文本快速路径：跳过全文档布局，O(1) 判定 (cap, MAX_HEIGHT)。
        # 依据一（单调缓存）：曾实测高度撞上限的宽度 _tall_cap，更窄只会更高；
        # 依据二（字符阈值）：字符数达“必然超高”阈值，最乐观排版也撞上限。
        # 12 万字符实测：每步 2 次全文档布局 ~1.4s → O(1)，消除 resize 卡死。
        cap = self._width_cap
        if cap < 100000:  # 排除 16777215 初始未限宽态（几何不代表真实窗口）
            tall_cached = bool(self._tall_cap) and cap <= self._tall_cap
            if tall_cached or len(self._text) >= self._definitely_tall_chars():
                if cap > self._tall_cap:
                    self._tall_cap = cap
                if self.maximumWidth() != cap:
                    self.setMaximumWidth(cap)
                if self.width() != cap or self.height() != self.MAX_HEIGHT:
                    self.setFixedSize(cap, self.MAX_HEIGHT)
                    self.contentHeightChanged.emit(self.MAX_HEIGHT)
                return

        # 测量用独立同步文档：共享文档被 QTextEdit 钉在 viewport 宽且布局异步，
        # setTextWidth(w) 后立即读 size() 拿到旧宽缓存 → 短消息误判"多行"
        # 走 WIDE 分支钉死 MAX_HEIGHT（气泡下方大片空白的根因）
        doc = self._measure_document()
        doc.setPlainText(self._text)
        fm = QFontMetrics(self.text_edit.font())

        # ── 宽度自适应（ChatGPT 式）──
        # 先测内容在 cap 宽下的总高度：仍超过约 3 行 → 内容多，用满上限拉宽；
        # 短消息（≤ 2-3 行）才按最长单行收缩，避免窄气泡被迫多行换行
        doc.setTextWidth(self._width_cap)
        if doc.size().height() > 3.0 * fm.lineSpacing():
            bubble_w = self._width_cap
        else:
            # 短消息：按最长单行收缩。
            # 用 QTextLayout 实测行渲染宽度（含 fallback 字体/字距），而非 QFontMetrics：
            # 特殊字符（emoji/全角标点等）fallback 渲染实际宽度常大于 QFontMetrics
            # 测量值，旧实现 +32px 余量被 viewer 布局边距(16) + documentMargin(8)
            # 抵消后仅剩 8px，测量一旦偏小即出现文字溢出气泡右缘。
            longest = 0.0
            block = doc.begin()
            while block.isValid():
                layout = block.layout()
                if layout is not None:
                    for i in range(layout.lineCount()):
                        longest = max(longest, layout.lineAt(i).naturalTextWidth())
                block = block.next()
            # 可用文字宽 = 气泡宽 - 布局边距(8*2) - documentMargin(4*2)，
            # 故最长行 + 40（16 边距 + 8 docMargin + 16 视觉余量）
            bubble_w = max(80, min(int(math.ceil(longest)) + 40, self._width_cap))
        if self.maximumWidth() != bubble_w:
            self.setMaximumWidth(bubble_w)

        # 高度按气泡实际宽计算（viewport 在气泡收紧瞬间可能仍是旧值，不可信）。
        # 测量宽必须对齐真实渲染视口：text_edit 宽 = bubble_w - 16(布局边距 8×2)，
        # QTextDocument 渲染内容宽再减 docMargin 4×2。旧实现 setTextWidth(bubble_w)
        # 比渲染宽大 16px → "测量不溢出、渲染溢出"错位 → 滚动条出现 → 视口再窄
        # 6px → 内容重折行 → 高度变化 → 滚动条消失 → 宽度反馈环（气泡滚动条
        # 反复出现/消失抖动）。对齐无滚动条渲染宽后两态各自稳定：无溢出时测量=
        # 渲染；溢出时滚动条只会让渲染更窄更高，方向单调不回摆。
        doc.setTextWidth(bubble_w - 16)
        h = int(math.ceil(doc.size().height())) + 12  # 上下边距

        # 🛡️ 短消息收缩分支测出超高 = longest 测量伪信号（如字体 fallback 未就绪时
        # naturalTextWidth 异常偏小 → bubble_w 收到 ~80 → tiny 宽度下短文本折出
        # 十几行 → h 必然撞 MAX_HEIGHT）。回退全宽重测一次自愈，避免：
        # 1) 气泡真的收缩成 80px 孤条；2) 下方 _tall_cap 把该 cap 记为"确认超高"，
        # 之后所有 ≤cap 的宽度永久走 O(1) 快速路径 → 2 行短消息被锁死
        # (cap, MAX_HEIGHT)，气泡全宽 300 高全是空白（实测截图症状）。
        if h > self.MAX_HEIGHT and bubble_w < self._width_cap:
            bubble_w = self._width_cap
            if self.maximumWidth() != bubble_w:
                self.setMaximumWidth(bubble_w)
            doc.setTextWidth(bubble_w - 16)
            h = int(math.ceil(doc.size().height())) + 12

        # 限制最大高度：内容超出 MAX_HEIGHT 后由 QTextEdit 内部滚动条处理滚动
        h = max(40, min(h, self.MAX_HEIGHT))

        # [PERF] 更新“超高”单调缓存：撞上限 → 记录确认宽度（取 max 保留最宽确认点），
        # 后续更窄宽度走 O(1) 快速路径；未撞上限不更新（更宽时结论仍可能对更窄宽度有效）。
        # 🛡️ 仅在 bubble_w 用满上限（bubble_w >= cap，真·内容超高）时记录：
        # 短消息收缩分支的撞限是 tiny 宽度测量伪信号，一旦记录，后续宽度
        # 全部被 O(1) 快速路径锁死 (cap, MAX_HEIGHT)（见上方回退重测注释）。
        if h >= self.MAX_HEIGHT and self._width_cap < 100000 and bubble_w >= self._width_cap:
            self._tall_cap = max(self._tall_cap, self._width_cap)

        # ⚠️ 必须 setFixedSize：仅设 maximumWidth 时布局仍按 QTextEdit 的
        # 默认 sizeHint(272px) 分配宽度，气泡实际展不开（AlignRight 下尤甚）
        if self.width() != bubble_w or self.height() != h:
            self.setFixedSize(bubble_w, h)
            self.contentHeightChanged.emit(h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 性能优化：使用防抖定时器，避免每次 resize 都触发高度计算
        self._resize_debounce_timer.stop()
        self._resize_debounce_timer.start()

    def sizeHint(self):
        """返回**内容实测**尺寸。

        ⚠️ QWidget 默认 sizeHint 来自内部 QTextEdit 的默认值（272x200），与本控件
        经 ``_update_height`` 算出的真实尺寸无关。控件被 ``setFixedSize`` 钉住后，
        min/max 是对的，但 sizeHint 仍是 200 高——**父级布局按 sizeHint 分配空间**，
        于是中间容器（_user_bubble）被撑高、气泡与下方按钮栏脱节（2026-09-16
        hover/图片错位问题的根因之一）。这里改为返回当前实测量。
        """
        size = super().sizeHint()
        if self.width() > 0 and self.height() > 0:
            return QSize(self.width(), self.height())
        return QSize(size.width(), max(40, min(size.height(), self.MAX_HEIGHT)))

    def minimumSizeHint(self):
        """与 sizeHint 一致：控件尺寸由内容决定，不参与父级拉伸。"""
        return self.sizeHint()

    def _do_resize_update(self):
        """防抖后执行高度更新"""
        self._update_height()

    def update_height(self):
        """公开方法，用于外部触发高度重算（跳过防抖，直接更新）"""
        self._resize_debounce_timer.stop()  # 取消待执行的防抖
        self._update_height()

    def cleanup(self):
        """
        清理 PlainTextViewer 持有的资源，防止内存泄漏。
        """
        try:
            self._resize_debounce_timer.stop()
            self._resize_debounce_timer.deleteLater()
        except RuntimeError:
            pass

        # 清理文本缓存
        self._text = ""
        self._tall_cap = 0

        # 清理 QTextEdit（关键修复：先清空内容，再释放文档）
        if hasattr(self, "text_edit") and self.text_edit:
            try:
                self.text_edit.clear()
                # 释放文档以释放内存
                doc = self.text_edit.document()
                doc.setPlainText("")
                # 清空undo/redo历史
                doc.setUndoRedoEnabled(False)
            except RuntimeError:
                pass

        # 清理引用
        self.text_edit = None

    def _show_context_menu(self, pos):
        """显示用户卡片右键菜单：复制、撤销、删除"""
        from app.utils.design_tokens import Colors

        menu = QMenu(self.text_edit)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {Colors.CARD_BG_SOLID};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 8px 32px 8px 12px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {scale_font_size(13)}px;
                {get_font_family_css()}
            }}
            QMenu::item:selected {{
                background-color: {Colors.HOVER_BG};
                border-radius: 4px;
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {Colors.BORDER};
                margin: 4px 8px;
            }}
        """)

        # 复制
        copy_action = menu.addAction(get_icon("复制"), "复制")
        copy_action.triggered.connect(lambda: self._copy_to_clipboard())

        menu.addSeparator()
        # 撤销
        undo_action = menu.addAction(get_icon("撤销"), "撤销到这里")
        undo_action.triggered.connect(lambda: self._request_undo())

        menu.addSeparator()

        # 删除
        delete_action = menu.addAction(get_icon("删除"), "删除")
        delete_action.triggered.connect(lambda: self._request_delete())

        menu.exec_(self.text_edit.mapToGlobal(pos))

    def _copy_to_clipboard(self, copy_selection: bool = True):
        """复制内容到剪贴板

        Args:
            copy_selection: 为 True 时优先复制选中文本（上下文菜单标准行为），
                            无选中时降级复制全文。
                            为 False 时直接复制全文（工具栏按钮行为）。
        """
        from PyQt5.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if copy_selection:
            cursor = self.text_edit.textCursor()
            selected = cursor.selectedText()
            if selected:
                clipboard.setText(selected)
                return
        clipboard.setText(self._text)

    def _convert_text_to_html(self, text: str) -> str:
        """将纯文本转换为独立 HTML 页面"""
        import html as html_mod

        escaped = html_mod.escape(text)
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>消息导出</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; color: #333; }}
        pre {{ background: #f5f5f5; padding: 16px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }}
    </style>
</head>
<body>
<pre>{escaped}</pre>
</body>
</html>"""

    def _show_save_success(self, file_path: str):
        """显示保存成功提示"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition

            main_window = self.window()
            if main_window:
                InfoBar.success(
                    "文件已导出",
                    file_path,
                    duration=3000,
                    parent=main_window,
                    position=InfoBarPosition.BOTTOM,
                )
        except Exception:
            pass

    def _show_save_error(self, error_msg: str):
        """显示保存失败提示"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition

            main_window = self.window()
            if main_window:
                InfoBar.error(
                    "导出失败",
                    error_msg,
                    duration=3000,
                    parent=main_window,
                    position=InfoBarPosition.BOTTOM,
                )
        except Exception:
            pass

    def _request_undo(self):
        """请求撤销 - 通知父组件"""
        # 向上查找 MessageCard 并发出 undoRequested 信号
        parent = self.parent()
        while parent:
            if hasattr(parent, "undoRequested"):
                parent.undoRequested.emit()
                break
            parent = parent.parent()

    def _request_delete(self):
        """请求删除 - 通知父组件"""
        # 向上查找 MessageCard 并发出 deleteRequested 信号
        parent = self.parent()
        while parent:
            if hasattr(parent, "deleteRequested"):
                parent.deleteRequested.emit()
                break
            parent = parent.parent()


def extract_image_data_uris(content) -> list:
    """从 multimodal content 按序提取 image 块的 data URI（仅 data: 格式）。

    支持 chat/completions（image_url.url 为 dict/str）、Responses（input_image）、
    Anthropic（image.source.base64）三种块格式。
    """
    uris = []
    if not isinstance(content, list):
        return uris
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        url = ""
        if btype == "image_url":
            img = block.get("image_url", {}) or {}
            url = str(img.get("url", "") or "") if isinstance(img, dict) else str(img)
        elif btype == "input_image":
            url = str(block.get("image_url", "") or "")
        elif btype == "image":
            src = block.get("source", {}) or {}
            if isinstance(src, dict) and src.get("type") == "base64":
                url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
        if url.startswith("data:image"):
            uris.append(url)
    return uris


def plan_image_attachment_sources(paths, fallback_content=None) -> list:
    """规划图片附件缩略图渲染来源（纯函数，无 Qt 依赖，便于测试）。

    路径优先；路径失效（如粘贴图 temp 被系统清理）时按序取 fallback_content
    中前 N 个 image 块的 data URI 兜底——附件块在前、工具注入块在后追加，
    序号对齐可靠，注入块天然不进入预览。

    Returns:
        list[tuple[source, data_uri, path]]：source 为本地路径（data_uri 为 None）
        或兜底 data URI（source 为 None）；无法渲染的项不出现。
    """
    if not isinstance(paths, list):
        return []
    image_uris = extract_image_data_uris(fallback_content)
    plan = []
    for i, path in enumerate(paths):
        if not isinstance(path, str) or not path:
            continue
        if os.path.exists(path):
            plan.append((path, None, path))
        elif i < len(image_uris):
            plan.append((None, image_uris[i], path))
    return plan


class _ImagePreviewDialog(MaskDialogBase):
    """图片查看弹窗：Mask 遮罩风格，完整等比显示（适配屏幕可用区 60%），无滚动，点遮罩关闭。"""

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 120))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 160))

        self.widget.setObjectName("imagePreviewWidget")
        self.widget.setStyleSheet(f"""
            #imagePreviewWidget {{
                background-color: #1E1E1E;
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)
        lay = QVBoxLayout(self.widget)
        lay.setContentsMargins(6, 6, 6, 6)

        # 等比适配屏幕可用区 60%：完整显示、非原图尺寸、无滚动
        screen = QApplication.primaryScreen().availableGeometry()
        scaled = pixmap.scaled(
            int(screen.width() * 0.6),
            int(screen.height() * 0.6),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        img_label = QLabel(self.widget)
        img_label.setPixmap(scaled)
        img_label.setScaledContents(False)
        lay.addWidget(img_label)

        # 滚轮缩放：以"适配屏幕 60%"为 1.0 基准，范围 0.2x ~ 5x
        self._pixmap = pixmap
        self._base_size = (scaled.width(), scaled.height())
        self._scale = 1.0
        self._img_label = img_label

        # MaskDialogBase 的 QHBoxLayout 会把 widget 拉伸到全屏：
        # 取出后自管几何并居中（与 ConfirmDialog._fit_widget_to_content 同法）
        self.layout().removeWidget(self.widget)
        self.widget.setParent(self)
        self.widget.adjustSize()
        self._center_widget()

    def wheelEvent(self, e):
        delta = e.angleDelta().y() if hasattr(e, "angleDelta") else 0
        if not delta:
            return
        factor = 1.15 if delta > 0 else 1 / 1.15
        new_scale = max(0.2, min(5.0, self._scale * factor))
        if abs(new_scale - self._scale) < 1e-6:
            return
        self._scale = new_scale
        self._apply_scale()
        e.accept()

    def _apply_scale(self):
        bw, bh = self._base_size
        screen = QApplication.primaryScreen().availableGeometry()
        w = max(16, int(bw * self._scale))
        h = max(16, int(bh * self._scale))
        # 上限保护：不超过屏幕可用区 92%，避免缩放到超出可见范围
        max_w = int(screen.width() * 0.92)
        max_h = int(screen.height() * 0.92)
        if w > max_w or h > max_h:
            w, h = max_w, max_h
        self._img_label.setPixmap(self._pixmap.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.widget.adjustSize()
        self._center_widget()

    def _center_widget(self):
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_widget()


def _decode_image_url_to_pixmap(url_str: str):
    """file:// / 绝对本地路径 / data:image → QPixmap（同步，失败返回 None）。

    只处理能同步拿到字节的来源；http(s) 走 _download_and_preview 异步路径。
    """
    from PyQt5.QtCore import QByteArray, QUrl
    from PyQt5.QtGui import QPixmap

    try:
        if url_str.startswith("data:"):
            b64 = url_str.split(",", 1)[1] if "," in url_str else ""
            if not b64:
                return None
            pix = QPixmap()
            return pix if pix.loadFromData(QByteArray.fromBase64(b64.encode("ascii"))) else None
        if url_str.startswith("file://"):
            path = QUrl(url_str).toLocalFile()
        elif os.path.isabs(url_str):
            path = url_str
        else:
            return None
        if not path or not os.path.isfile(path):
            return None
        pix = QPixmap(path)
        return pix if not pix.isNull() else None
    except Exception:
        return None


_image_nam = None


def _get_image_nam():
    """正文图片异步下载用的共享 QNetworkAccessManager（进程级单例）。

    单例而非每卡一个：卡片数量可达数十，各自持有 NAM 会平白多出一批
    网络连接池。延迟导入避免模块加载期拉起 QtNetwork。
    """
    global _image_nam
    if _image_nam is None:
        from PyQt5.QtNetwork import QNetworkAccessManager

        _image_nam = QNetworkAccessManager()
    return _image_nam


def _download_and_preview(url_str: str, parent=None) -> None:
    """远程图片异步下载 → 预览；失败回退系统默认程序打开。"""
    from PyQt5.QtCore import QUrl
    from PyQt5.QtGui import QDesktopServices, QPixmap
    from PyQt5.QtNetwork import QNetworkRequest

    def _on_finished(reply):
        try:
            pix = QPixmap()
            if pix.loadFromData(reply.readAll()):
                _show_image_preview(pix, parent=parent)
                return
        except Exception:
            pass
        try:
            QDesktopServices.openUrl(QUrl(url_str))
        except Exception:
            pass
        finally:
            reply.deleteLater()

    try:
        reply = _get_image_nam().get(QNetworkRequest(QUrl(url_str)))
        reply.finished.connect(lambda _r=reply: _on_finished(_r))
    except Exception:
        try:
            QDesktopServices.openUrl(QUrl(url_str))
        except Exception:
            pass


def _fence_assets_for_skeleton() -> tuple:
    """收集当前已注册 fence 渲染器的 assets（file:// URL）与权限声明。

    Returns:
        (assets_json, perms_json, cache_sig)
        assets_json: {lang: {"js": file_url, "css": file_url}} 的 JSON
        perms_json:  {lang: ["theme", ...]} 的 JSON
        cache_sig:   骨架缓存 key 用的签名（路径 + mtime），vendor 热替换即失效

    插件系统未就绪或任何异常都返回空表 —— 骨架构建不能被插件拖垮。
    """

    # 内置 fence（当前只有 ```widget）没有插件替它声明权限，在此兜底合入；
    # 插件系统未就绪时也必须带上，否则 widget 桥会全空。
    def _builtin_only() -> tuple:
        return ("{}", _js_literal(_BUILTIN_FENCE_PERMS), ())

    empty = _builtin_only()
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        renderers = reg.get_all_fence_renderers()
    except Exception:
        return empty
    if not renderers:
        return empty

    assets: dict = {}
    perms: dict = {_lang: list(_ps) for _lang, _ps in _BUILTIN_FENCE_PERMS.items()}
    sig: list = []
    for lang in sorted(renderers):
        info = renderers[lang]
        try:
            resolved = reg.resolve_fence_assets(info.plugin_name, info.assets)
        except Exception:
            resolved = {}
        entry: dict = {}
        for key in sorted(resolved):
            path = resolved[key]
            try:
                url = QUrl.fromLocalFile(path).toString()
            except Exception:
                continue
            entry[key] = url
            try:
                sig.append((lang, key, path, int(os.path.getmtime(path))))
            except OSError:
                sig.append((lang, key, path, 0))
        if entry:
            assets[lang] = entry
        if info.bridge_permissions:
            perms[lang] = list(info.bridge_permissions)

    # 本模块的 json 是 orjson（`import orjson as json`）：dumps 返回 bytes，
    # 且不接受 ensure_ascii 关键字 —— 这里统一归一化成 str。
    def _dump_js(obj) -> str:
        raw = json.dumps(obj)
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        # </ 会提前闭合骨架的 <script> 块，必须转义
        return str(raw).replace("</", "<\\/")

    assets_js = _dump_js(assets)
    perms_js = _dump_js(perms)
    return (assets_js, perms_js, tuple(sig))


def _show_image_preview(pixmap, parent=None) -> None:
    """打开图片预览弹窗（_ImagePreviewDialog 同模块，无需导入）。"""
    _ImagePreviewDialog(pixmap, parent=parent).exec_()


# [L3] 单张卡片保留的「宽度 → 高度」缓存条数上限。
# 窗口拖拽时宽度连续变化，缓存过久的宽度组合价值低，8 条足够覆盖往返拖拽。
_HEIGHT_CACHE_MAX = 8
