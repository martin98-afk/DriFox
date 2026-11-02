# -*- coding: utf-8 -*-
"""工具与思考独立卡（简洁模式流式期常驻面板）

背景（2026-11-02 用户诉求）：
消息一长，卡片顶部那块「工具与思考」折叠区会被滚出视口，用户看不到正在
执行的工具与思考过程。本卡把它搬到对话区上方（TOP 卡片容器，标题栏之下），
流式期间常驻显示、可拖拽高度；流式结束清空并隐藏，内容回落到消息卡内的
折叠态（历史可回看）。所有对话复用同一张卡。

数据流（不重新渲染，只镜像补丁）：
    MessageCard._content_data（v2 块列表）
      → CodeWebViewer._perform_update_v2 生成 tool_ops
      → 本卡 viewer 执行同一份 window.patchBlocks('tool-content', ops)

两侧都从空容器开始接收同一序列，DOM 状态因此天然一致；中途显示（流式进行
到一半才打开卡）由 ``CodeWebViewer.sync_tool_mirror()`` 补一次全量。

为什么复用 CodeWebViewer 而不是另写薄 viewer：
工具区依赖骨架里的整套 JS（patchBlocks / 折叠态携带 / lazy body / 图表
dispose），自己复制一份必然与主骨架漂移。本类只把骨架「面板化」：隐藏
分隔条、解除 body 滚动、让 #tool-content 铺满视口。
"""

from __future__ import annotations

import os

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import QSizePolicy
from loguru import logger

from app.widgets.card_viewers import CodeWebViewer
from app.widgets.cards.settings.system_card_frame import SystemCardFrame

# 面板化：把完整消息骨架改造成只承载 #tool-content 的面板
# - #tool-separator 隐藏（卡片自身头部已表达「工具与思考」+ 计数）
# - body 不再滚动，滚动交给 #tool-content（卡片高度由容器控制）
# - 折叠态解除：本卡的显隐由卡片控制，不需要骨架内的折叠语义
_PANEL_CHROME_JS = r"""
(function () {
  var ts = document.getElementById('tool-section');
  if (ts) { ts.style.display = ''; ts.setAttribute('data-collapsed', 'false'); }
  var sep = document.getElementById('tool-separator');
  if (sep) { sep.style.display = 'none'; }
  var cp = document.getElementById('content-placeholder');
  if (cp) { cp.style.display = 'none'; }
  var st = document.getElementById('tool-panel-chrome');
  if (!st) {
    st = document.createElement('style');
    st.id = 'tool-panel-chrome';
    st.textContent = [
      'html, body { height: 100% !important; overflow: hidden !important; background: transparent !important; }',
      'body { padding: 0 !important; margin: 0 !important; max-height: none !important; overflow: hidden !important; }',
      '#tool-section { height: 100% !important; margin: 0 !important; padding: 0 !important; display: block !important; }',
      '#tool-content { height: 100% !important; max-height: none !important; margin: 0 !important;',
      '  padding: 2px 2px 2px 4px !important; border-left: none !important; overflow-y: auto !important; }',
      '#tool-section[data-collapsed="true"] #tool-content { max-height: none !important; opacity: 1 !important; overflow-y: auto !important; }'
    ].join('\n');
    document.head.appendChild(st);
  }
})();
"""

# 清空 + 复位镜像的 JS 侧状态（复用前调用）
_RESET_JS = r"""
(function () {
  var tc = document.getElementById('tool-content');
  if (tc) { tc.innerHTML = ''; }
  try { window.__v2Carry = null; } catch (e) {}
  if (typeof window._v2LiveDrop === 'function') { window._v2LiveDrop(); }
  if (typeof window._setStreamingDock === 'function') { window._setStreamingDock(false); }
})();
"""

class ToolThinkViewer(CodeWebViewer):
    """工具与思考面板专用 viewer：只承载 #tool-content，不做正文渲染。

    ``_streaming`` 恒 False：本 viewer 不走消息卡那套流式副作用（坞态限高、
    内容哨兵、虚高告警、终渲染调度），DOM 完全由镜像补丁驱动。
    """

    def __init__(self, parent=None):
        super().__init__(parent, light=False)
        self._streaming = False
        self._is_history = False
        # 铺满卡片；父类默认 Expanding × Fixed（消息卡按内容撑高）
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(0)
        # 工具块展开不依赖 Python 反查：本 viewer 没有 _markdown_text 书账，
        # 懒注入会让展开的块永远空白，故一律全量内联 body。
        self.setContextMenuPolicy(Qt.NoContextMenu)

    # ── 骨架语义覆盖 ──────────────────────────────────

    @property
    def _tool_compact_mode(self) -> bool:
        """恒简洁模式：工具/思考块按简洁形态渲染（折叠 body 由 lazy_body 关闭兜底）"""
        return True

    @property
    def _tool_lazy_body(self) -> bool:
        """禁用懒注入：本 viewer 无 markdown 反查源，展开时无法补 body。"""
        return False

    def _on_js_ready(self):
        super()._on_js_ready()
        self._apply_panel_chrome()
        # 骨架重建（崩溃自愈 / 主题切换重载）后镜像端 DOM 已空，
        # 通知宿主补一次全量快照（由 ToolThinkCard 接线）。
        cb = self.__dict__.get("_on_mirror_ready")
        if callable(cb):
            try:
                cb()
            except Exception:
                pass

    def _serve_tool_body(self, tool_call_id: str) -> None:
        """工具块展开请求：反查源是镜像源 viewer（本 viewer 没有 markdown 书账）

        镜像过来的块沿用消息卡的 lazy_body 形态（body 是空占位），展开时 JS
        向**本** viewer 的 console 通道请求内容。本 viewer 没有渲染源，必须
        委托消息卡 viewer 反查重渲，再回填到自己的 DOM；否则完成框点开永远是
        空的（用户可见「工具完成框展不开」）。
        """
        tid = (tool_call_id or "").strip()
        if not tid or not self._is_js_ready:
            return
        src = self.__dict__.get("_mirror_source")
        html = ""
        if src is not None:
            try:
                html = src._render_tool_body_html(tid)
            except Exception:
                html = ""
        self._fill_tool_body(tid, html)

    def _apply_panel_chrome(self) -> None:
        if not self._is_js_ready:
            return
        try:
            page = self.page()
            if page is not None:
                page.runJavaScript(_PANEL_CHROME_JS)
        except RuntimeError:
            pass

    # ── 镜像入口 ──────────────────────────────────────

    def apply_tool_js(self, js: str) -> None:
        """执行一批来自消息卡的工具区 JS（patchBlocks / live append）。

        JS 未就绪时静默丢弃：宿主会在 viewer 就绪后补一次全量快照。
        """
        if not js or not self.__dict__.get("_is_js_ready", False):
            return
        if os.environ.get("DRIFOX_TOOL_THINK_DIAG"):
            logger.info(f"[tool-think] apply len={len(js)}")
        try:
            page = self.page()
            if page is not None:
                page.runJavaScript(js)
        except RuntimeError:
            pass

    def reset_content(self) -> None:
        """清空工具区并复位镜像状态（切会话 / 新一轮流式前调用）"""
        self._v2_last = None
        self._v2_reset_live()
        self._document_height = 0
        try:
            page = self.page()
            if page is not None:
                page.runJavaScript(_RESET_JS)
        except RuntimeError:
            pass

class ToolThinkCard(SystemCardFrame):
    """工具与思考独立卡（TOP 容器，与分享卡 / 历史问题卡同槽位互斥）

    高度：阶段 1 按窗口比例取固定值（默认 30%，160px ~ 60%）；
    阶段 2 由底部拖拽条写入 ``_fixed_h`` 覆盖，并按窗口记忆。
    """

    # 卡片高度变化 → CardContainer._schedule_expand 重新对齐容器高度
    heightChanged = pyqtSignal()

    MIN_HEIGHT = 160
    DEFAULT_RATIO = 0.30
    MAX_RATIO = 0.60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.set_title_text("工具与思考")
        self.set_icon_svg("工具")
        self._fixed_h = 0
        # 拖拽/动态高度：容器高度直接 snap，不走展开动画（否则滞后一个动画时长）
        from app.widgets.cards.card_container import CardContainer

        self.setProperty(CardContainer.NO_ANIMATION_PROP, True)
        self.setMinimumHeight(self.MIN_HEIGHT)

        # 内容区：外层 ScrollArea 只作承托，滚动交给 webview 内部的 #tool-content
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("background: transparent; border: none;")

        self.viewer = ToolThinkViewer(self)
        self.content_layout.addWidget(self.viewer, 1)
        # 点击标题栏计数由宿主刷新（工具与思考 · N 项）
        self._item_count = 0

    # ── 高度 ──────────────────────────────────────────

    def _target_height(self) -> int:
        if self._fixed_h > 0:
            return self._fixed_h
        win = self.window()
        win_h = win.height() if win is not None else 0
        if win_h <= 0:
            return self.MIN_HEIGHT
        return max(self.MIN_HEIGHT, int(win_h * self.DEFAULT_RATIO))

    def sizeHint(self) -> QSize:
        # 宽度由容器布局决定（Expanding）：不能透传 QWebEngineView 的 sizeHint
        # （默认值远大于实际可用宽，会把卡片的最小宽抬到窗口显示不下）。
        return QSize(320, self._target_height())

    def set_panel_height(self, height: int) -> None:
        """外部（拖拽条 / 记忆恢复）写入固定高度"""
        win = self.window()
        win_h = win.height() if win is not None else 0
        lo = self.MIN_HEIGHT
        hi = max(lo, int(win_h * self.MAX_RATIO)) if win_h > 0 else lo
        self._fixed_h = max(lo, min(int(height), hi))
        self.updateGeometry()
        self.heightChanged.emit()

    def showEvent(self, event):  # noqa: N802 (Qt 命名)
        super().showEvent(event)
        # 首次显示 / 窗口尺寸变化后重算高度：容器展开在读 sizeHint，
        # 必须在 show 前让几何失效，否则卡片停在旧高度。
        self.updateGeometry()
        self.heightChanged.emit()
        self._refresh_viewer_theme()

    def refresh_style(self):
        """主题切换：头部/图标走父类，webview 骨架另需注入 CSS 变量"""
        super().refresh_style()
        self._refresh_viewer_theme()

    def _refresh_viewer_theme(self) -> None:
        """按当前主题刷新 webview（refresh_theme 内部按主题版本号幂等）"""
        viewer = getattr(self, "viewer", None)
        if viewer is None:
            return
        try:
            viewer.refresh_theme()
        except Exception:
            pass

    # ── 内容计数（头部展示「工具与思考 · N 项」）─────────────

    def set_item_count(self, count: int) -> None:
        self._item_count = max(0, int(count))
        self.set_count(self._item_count)

    # ── 生命周期 ──────────────────────────────────────

    def reset_panel(self) -> None:
        """清空内容（隐藏 / 切会话 / 新一轮流式前）"""
        self.set_item_count(0)
        viewer = getattr(self, "viewer", None)
        if viewer is not None:
            viewer.reset_content()
