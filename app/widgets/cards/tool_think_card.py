# -*- coding: utf-8 -*-
"""工具与思考独立卡（简洁模式流式期常驻面板）

背景（2026-11-02 用户诉求）：
消息一长，卡片顶部那块「工具与思考」折叠区会被滚出视口，用户看不到正在
执行的工具与思考过程。本卡把它搬到对话区上方（TOP 卡片容器，标题栏之下），
流式期间常驻显示；流式结束清空并隐藏，内容回落到消息卡内的折叠态（历史可
回看）。所有对话复用同一张卡。

数据流（不重新渲染，只镜像补丁）：
    MessageCard._content_data（v2 块列表）
      → CodeWebViewer._perform_update_v2 生成 tool_ops
      → 本卡 viewer 执行同一份 window.patchBlocks('tool-content', ops)

两侧都从空容器开始接收同一序列，DOM 状态因此天然一致；中途显示（流式进行
到一半才打开卡）由 ``CodeWebViewer.sync_tool_mirror()`` 补一次全量。

高度模型（三档优先）：
    用户拖拽锁定 > 内容自然高度 > 首帧默认（窗口 20%）
统一 clamp 到 [MIN_HEIGHT, 窗口高 × MAX_RATIO]。内容高度由 viewer 侧
``#tool-content.scrollHeight`` 探测后上报（节流），卡片只做布局决策；
WebView 自身铺满卡片，内部内容靠 ``#tool-content { height: 100% }`` 撑满。
"""

from __future__ import annotations

import os

from PyQt5.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QSizePolicy, QWidget

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
  // 骨架里多处贴底/清理逻辑以 window._streamingActive 为守卫（_scrollToolContentToBottom、
  // _v2AfterPatch 的搬入节点清理）。本面板恒为「流式进行中」视图，不置位的话
  // 思考增量追加后不会贴底（用户实测「只看到第一行」）。
  window._streamingActive = true;
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

# 内容自然高度探测：工具区子节点的真实高度累加
# ⚠ 不能用 #tool-content.scrollHeight：它 height:100% 撑满卡片，scrollHeight 在
# 内容少时等于自身高度 → 卡片高度自反馈降不下来，思考结束后下方留一大片空白。
_MEASURE_JS = r"""(function(){
  var tc=document.getElementById('tool-content');
  if(!tc) return 0;
  var h=0;
  for (var i=0;i<tc.children.length;i++){
    var c=tc.children[i];
    if (!c || !c.offsetHeight) continue;
    var s=window.getComputedStyle(c);
    h += c.offsetHeight + (parseFloat(s.marginTop)||0) + (parseFloat(s.marginBottom)||0);
  }
  return Math.ceil(h) + 12;
})();"""

class ToolThinkViewer(CodeWebViewer):
    """工具与思考面板专用 viewer：只承载 #tool-content，不做正文渲染。

    ``_streaming`` 恒 False：本 viewer 不走消息卡那套流式副作用（坞态限高、
    内容哨兵、虚高告警、终渲染调度），DOM 完全由镜像补丁驱动。
    """

    # 内容高度探测节流：镜像补丁按批到达（软边界 100ms 级），探测压到 140ms
    # 合并一次，避免每条补丁都跑一次 runJavaScript 往返。
    _MEASURE_INTERVAL_MS = 140

    def __init__(self, parent=None):
        super().__init__(parent, light=False)
        self._streaming = False
        self._is_history = False
        # 铺满卡片；父类默认 Expanding × Fixed（消息卡按内容撑高）。
        # 纵向用 Ignored：ScrollArea 的 widgetResizable 会拿子控件 sizeHint
        # 定 content_widget 高度，QWebEngineView 的默认 sizeHint 很高 → 内容区
        # 被撑出视口，卡片底部的拖拽条被裁掉。
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.setMinimumHeight(0)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self._measure_timer = QTimer(self)
        self._measure_timer.setSingleShot(True)
        self._measure_timer.setInterval(self._MEASURE_INTERVAL_MS)
        self._measure_timer.timeout.connect(self._do_measure)

    # ── 骨架语义覆盖 ──────────────────────────────────

    @property
    def _tool_compact_mode(self) -> bool:
        """恒简洁模式：工具/思考块按简洁形态渲染（折叠 body 由 lazy_body 关闭兜底）"""
        return True

    @property
    def _tool_lazy_body(self) -> bool:
        """禁用懒注入：本 viewer 无 markdown 反查源，展开时无法补 body。

        注意：镜像过来的 HTML 仍沿用消息卡 viewer 的 ctx（lazy_body=True），
        展开依赖 ``_serve_tool_body`` 委托镜像源重渲，见下方覆盖。
        """
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
            return
        # DOM 变了 → 重新探测内容高度（节流合并同一批内的多次补丁）
        if not self._measure_timer.isActive():
            self._measure_timer.start()

    def reset_content(self) -> None:
        """清空工具区并复位镜像状态（切会话 / 新一轮流式前调用）"""
        self._v2_last = None
        self._v2_reset_live()
        self._document_height = 0
        try:
            self._measure_timer.stop()
        except RuntimeError:
            pass
        try:
            page = self.page()
            if page is not None:
                page.runJavaScript(_RESET_JS)
        except RuntimeError:
            pass

    # ── 内容高度探测 ──────────────────────────────────

    def _do_measure(self) -> None:
        try:
            page = self.page()
            if page is None:
                return
            page.runJavaScript(_MEASURE_JS, self._on_measured)
        except RuntimeError:
            pass

    def _on_measured(self, h) -> None:
        try:
            h = int(h or 0)
        except (TypeError, ValueError):
            return
        if h <= 0:
            return
        cb = self.__dict__.get("_on_content_height")
        if callable(cb):
            try:
                cb(h)
            except Exception:
                pass

class _ResizeHandle(QWidget):
    """卡片底部的上下拖拽条：向上拖变高、向下拖变矮。

    10px 热区（好抓）+ 居中 2px 细线（默认极淡，hover 加宽加深）。
    整条涂灰太丑，纯透明又抓不到位置——自绘一条居中的短细线是折中。
    """

    HEIGHT = 10
    _LINE_W = 36
    _LINE_W_HOVER = 60

    def __init__(self, card: "ToolThinkCard"):
        super().__init__(card)
        self._card = card
        self._drag_start_y = 0
        self._drag_start_h = 0
        self._hover = False
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip("拖动调整高度")

    def paintEvent(self, event):  # noqa: N802 (Qt 命名)
        from PyQt5.QtCore import QRectF
        from PyQt5.QtGui import QColor, QPainter

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(128, 128, 128, 90 if self._hover else 34))
        w = self._LINE_W_HOVER if self._hover else self._LINE_W
        rect = QRectF((self.width() - w) / 2.0, (self.height() - 2) / 2.0, w, 2.0)
        painter.drawRoundedRect(rect, 1.0, 1.0)

    def enterEvent(self, event):  # noqa: N802 (Qt 命名)
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 (Qt 命名)
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802 (Qt 命名)
        if event.button() == Qt.LeftButton:
            logger.info(f"[tool-think] resize press y={event.globalPos().y()}")
            self._drag_start_y = event.globalPos().y()
            self._drag_start_h = max(self._card.height(), self._card.MIN_HEIGHT)
            self._card.begin_user_resize()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 (Qt 命名)
        if event.buttons() & Qt.LeftButton:
            # 向上拖 = 变高：屏幕 y 减小 → 高度增加
            delta = event.globalPos().y() - self._drag_start_y
            self._card.apply_user_resize(self._drag_start_h - delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt 命名)
        if event.button() == Qt.LeftButton:
            logger.info(f"[tool-think] resize release h={self._card.height()}")
            self._card.end_user_resize()
            event.accept()
            return
        super().mouseReleaseEvent(event)

class ToolThinkCard(SystemCardFrame):
    """工具与思考独立卡（TOP 容器，与分享卡 / 历史问题卡同槽位互斥）

    高度三档优先：用户拖拽锁定 > 内容自然高度 > 首帧默认。
    统一 clamp 到 [MIN_HEIGHT, 窗口高 × MAX_RATIO]。
    """

    # 卡片高度变化 → CardContainer._schedule_expand 重新对齐容器高度
    heightChanged = pyqtSignal()

    MIN_HEIGHT = 96          # 一行思考 + 头部的最低可读高度
    DEFAULT_RATIO = 0.20     # 首帧默认（内容尚未探测出来时）
    MAX_RATIO = 0.40         # 上限：不超过窗口可视高度的 40%（用户要求比 65% 矮）
    # 紧凑头部：基类默认标题 12pt + 图标 20px，在大卡上偏重
    _ICON_SVG_BASE_SIZE = 15

    # 内容高度上报的合并窗口：容器展开动画约 200ms，变化过密会不断打断动画
    # 造成高度抖动。240ms 合并一次，让每段动画能跑完。
    _HEIGHT_COMMIT_MS = 240
    # 退出淡出：容器折叠是「先隐卡片再收区域」，不先淡出就会看到内容瞬间消失
    # 后剩一块空白区域慢慢收。淡出让两步连成一段。
    EXIT_FADE_MS = 130

    def __init__(self, parent=None):
        super().__init__(parent)
        self.set_title_text("工具与思考")
        self.set_icon_svg("工具")
        self._apply_compact_header()
        self._content_h = 0     # viewer 上报的内容自然高度
        self._user_h = 0        # 用户拖拽锁定的总高度（0 = 未锁定）
        self._item_count = 0
        self._resize_dragging = False

        self.setMinimumHeight(self.MIN_HEIGHT)

        # 内容区：外层 ScrollArea 只作承托，滚动交给 webview 内部的 #tool-content
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("background: transparent; border: none;")

        self.viewer = ToolThinkViewer(self)
        self.viewer._on_content_height = self._on_content_height
        self.content_layout.addWidget(self.viewer, 1)

        # 拖拽条加在**卡片主布局**（scroll_area 之后），不能放 content_layout：
        # 内容区的 content_widget 高度受 webview sizeHint 撑高，会被 ScrollArea
        # 视口裁掉 → 拖拽条根本不在可见区，用户拖不到（实测「拖拉没反应」）。
        self._handle = _ResizeHandle(self)
        self.layout().addWidget(self._handle, 0)

        self._height_commit_timer = QTimer(self)
        self._height_commit_timer.setSingleShot(True)
        self._height_commit_timer.setInterval(self._HEIGHT_COMMIT_MS)
        self._height_commit_timer.timeout.connect(self._commit_height)

    # ── 高度模型 ──────────────────────────────────────

    def _cap_height(self) -> int:
        win = self.window()
        win_h = win.height() if win is not None else 0
        if win_h <= 0:
            return self.MIN_HEIGHT
        return max(self.MIN_HEIGHT, int(win_h * self.MAX_RATIO))

    def _chrome_height(self) -> int:
        """卡片里除 viewer 之外的高度（头部 + 边距 + 拖拽条）"""
        try:
            if self.height() > 0 and self.viewer.height() > 0:
                return max(0, self.height() - self.viewer.height())
            return self._header_layout.sizeHint().height() + self._handle.HEIGHT + 14
        except (RuntimeError, AttributeError):
            return 44

    def _target_height(self) -> int:
        cap = self._cap_height()
        if self._user_h > 0:
            return max(self.MIN_HEIGHT, min(self._user_h, cap))
        if self._content_h > 0:
            return max(self.MIN_HEIGHT, min(self._content_h + self._chrome_height(), cap))
        win = self.window()
        win_h = win.height() if win is not None else 0
        floor = int(win_h * self.DEFAULT_RATIO) if win_h > 0 else self.MIN_HEIGHT
        return max(self.MIN_HEIGHT, min(floor, cap))

    def sizeHint(self) -> QSize:
        # 宽度由容器布局决定（Expanding）：不能透传 QWebEngineView 的 sizeHint
        # （默认值远大于实际可用宽，会把卡片的最小宽抬到窗口显示不下）。
        return QSize(320, self._target_height())

    def _on_content_height(self, h: int) -> None:
        """viewer 上报内容自然高度：#tool-content 的 scrollHeight

        用户拖拽锁定期间不自动改高（正在手动调，自动改会打架）。
        """
        h = max(0, int(h))
        if abs(h - self._content_h) < 6:
            return
        self._content_h = h
        if self._user_h > 0:
            return
        if not self._height_commit_timer.isActive():
            self._height_commit_timer.start()

    def _commit_height(self) -> None:
        if self.__dict__.get("_resize_dragging", False):
            return
        self.updateGeometry()
        self.heightChanged.emit()
        # 容器展开动画约 200ms：动画进行中 #tool-content 的 clientHeight 还很
        # 小，此刻滚底会被钳在中途，看起来就像「内容还没出现就已经置底了，
        # 之后新内容全在视口下方」。等高度长完再滚一次才是「内容出现后置底」。
        QTimer.singleShot(260, self._scroll_content_to_bottom)

    def _scroll_content_to_bottom(self) -> None:
        """内容与高度都落定后再贴底（尊重用户上滚阅读，不强制打断）"""
        viewer = getattr(self, "viewer", None)
        if viewer is None:
            return
        try:
            page = viewer.page()
            if page is not None:
                page.runJavaScript("if(typeof _scrollToolContentToBottom==='function')_scrollToolContentToBottom();")
        except RuntimeError:
            pass

    # ── 拖拽 ─────────────────────────────────────────

    def _container(self):
        """承载本卡的 CardContainer（TO P 槽位容器）"""
        parent = self.parent()
        if parent is not None and hasattr(parent, "_set_axis_max"):
            return parent
        return None

    def begin_user_resize(self) -> None:
        """拖拽开始：停掉容器动画 + 冻结自动高度

        不能只靠 NO_ANIMATION_PROP：_do_expand 用容器 layout.sizeHint() 算目标，
        而 QWidget 的 sizeHint 变更靠 updateGeometry 触发失效，在拖拽这种每像素
        一次的高频链路里会被布局缓存吞掉（实测拖完高度不变）。直接改容器轴向
        上限最直接。
        """
        from PyQt5.QtCore import QPropertyAnimation

        from app.widgets.cards.card_container import CardContainer

        self._height_commit_timer.stop()
        self._resize_dragging = True
        self.setProperty(CardContainer.NO_ANIMATION_PROP, True)
        container = self._container()
        anim = getattr(container, "_expand_animation", None) if container is not None else None
        if anim is not None and anim.state() == QPropertyAnimation.Running:
            try:
                anim.stop()
            except RuntimeError:
                pass

    def apply_user_resize(self, height: int) -> None:
        cap = self._cap_height()
        self._user_h = max(self.MIN_HEIGHT, min(int(height), cap))
        # 直接锁住本卡高度并抬/压容器轴向上限：不经过 sizeHint → _do_expand
        # 那条链（拖拽高频下布局缓存会吞掉 updateGeometry）。
        self.setMinimumHeight(self._user_h)
        container = self._container()
        if container is not None:
            try:
                container._set_axis_max(self._user_h)
            except (RuntimeError, AttributeError):
                pass
        self.updateGeometry()

    def end_user_resize(self) -> None:
        """拖拽结束：把高度锁交还容器（恢复 sizeHint 驱动 + 动画）"""
        from app.widgets.cards.card_container import CardContainer

        self._resize_dragging = False
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setProperty(CardContainer.NO_ANIMATION_PROP, False)
        # 松手后让容器按 sizeHint 对齐一次（拖到窗口上限被 clamp 时收正）
        self.updateGeometry()
        self.heightChanged.emit()

    # ── 生命周期 ──────────────────────────────────────

    def set_item_count(self, count: int) -> None:
        self._item_count = max(0, int(count))
        self.set_count(self._item_count)

    def showEvent(self, event):  # noqa: N802 (Qt 命名)
        super().showEvent(event)
        # 首次显示 / 窗口尺寸变化后重算高度：容器展开在读 sizeHint，
        # 必须在 show 前让几何失效，否则卡片停在旧高度。
        self.updateGeometry()
        self.heightChanged.emit()
        self._refresh_viewer_theme()
        # 上一轮可能停在淡出后的 opacity:0，显示时复位
        self._reset_enter_opacity()

    # ── 进出场动画 ──────────────────────────────────

    def play_exit(self, on_done) -> None:
        """退出：webview 内容淡出，结束后再交给 CardManager 走容器折叠动画

        CardManager.hide_card 是立即 setVisible(False)，容器随后才折叠 -> 用户
        看到的是「内容瞬失 + 空白区域收缩」。先把内容淡出，两步连起来才连贯。
        用 body opacity 而不是 QGraphicsOpacityEffect：后者对 QWebEngineView
        的原生 HWND 子窗无效。
        """
        done = False

        def _finish():
            nonlocal done
            if done:
                return
            done = True
            try:
                on_done()
            except RuntimeError:
                pass

        viewer = getattr(self, "viewer", None)
        page = viewer.page() if viewer is not None else None
        if page is None:
            _finish()
            return
        try:
            page.runJavaScript(
                "(function(){var b=document.body;if(!b)return;"
                f"b.style.transition='opacity {self.EXIT_FADE_MS}ms ease';"
                "b.style.opacity='0';})();"
            )
        except RuntimeError:
            _finish()
            return
        QTimer.singleShot(self.EXIT_FADE_MS, _finish)

    def _reset_enter_opacity(self) -> None:
        """进场：把 body opacity 复位为 1（淡出后的残留）"""
        viewer = getattr(self, "viewer", None)
        if viewer is None:
            return
        try:
            page = viewer.page()
            if page is not None:
                page.runJavaScript(
                    "(function(){var b=document.body;if(!b)return;"
                    "b.style.transition='none';b.style.opacity='1';"
                    "requestAnimationFrame(function(){b.style.transition='';});})();"
                )
        except RuntimeError:
            pass

    def refresh_style(self):
        """主题切换：头部/图标走父类，webview 骨架另需注入 CSS 变量"""
        super().refresh_style()
        self._apply_compact_header()
        self._refresh_viewer_theme()

    def _apply_compact_header(self) -> None:
        """紧凑头部：基类 refresh_style 会把标题强制改回 12pt，每次压回去

        图标尺寸走类属性 ``_ICON_SVG_BASE_SIZE``（15px），基类刷新时自动跟随。
        """
        from app.utils.utils import get_unified_font

        try:
            self.title_label.setFont(get_unified_font(10, True))
        except RuntimeError:
            pass

    def _refresh_viewer_theme(self) -> None:
        """按当前主题刷新 webview（refresh_theme 内部按主题版本号幂等）"""
        viewer = getattr(self, "viewer", None)
        if viewer is None:
            return
        try:
            viewer.refresh_theme()
        except Exception:
            pass

    def reset_panel(self) -> None:
        """清空内容（隐藏 / 切会话 / 新一轮流式前）

        刻意**不**复位高度：保留上次高度可避免下次显示时「先矮一截再长高」
        的跳动；内容真正变少时首轮测量会把高度收回。
        """
        self.set_item_count(0)
        viewer = getattr(self, "viewer", None)
        if viewer is not None:
            viewer.reset_content()
