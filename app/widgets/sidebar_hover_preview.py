from __future__ import annotations

from PyQt5.QtCore import QEasingCurve, QPoint, QVariantAnimation, Qt, QTimer
from PyQt5.QtWidgets import QVBoxLayout, QWidget


class HoverPreviewOverlay(QWidget):
    """侧栏 hover 悬浮预览浮层：Qt.Tool 顶层 owned 窗口（路线 C）。

    为什么是独立顶层窗口（决策 D025）：路线 A 的 WA_NativeWindow 原生**子**
    HWND 常驻在 frameless 主窗口客户区内，会整体击穿 qframelesswindow 的边缘
    WM_NCHITTEST（四边 resize 全废）。路线 C 改为独立顶层 HWND：

    - 构造传 parent + Qt.Tool → owned 顶层窗口：z-order 恒在 owner 之上，
      能压住对话区 QWebEngineView（原生 HWND）不穿透；
    - 不占主窗口客户区 → 不干扰边缘命中测试，frameless resize 完好；
    - 按需 show/hide 不常驻（hover 期间才存在），进一步远离 A 路线死结；
    - Qt.Tool 不进任务栏；owner 最小化时系统自动连带隐藏本浮层；
    - WA_ShowWithoutActivating：hover 弹出不抢焦点。

    跟随策略：place() 用 mapToGlobal 换算屏幕坐标定位；主窗口 moveEvent /
    resizeEvent 时宿主调 sync_to_window() 重定位。滑入/滑出动画每帧重读
    主窗口几何，动画期间天然跟随。已知代价：预览期拖动主窗口存在一帧滞后
    （hover 预览是临时态，用户此时通常不拖窗口，可接受）。
    """

    EDGE_INSET = 6  # 贴窗口外缘内缩，避让主窗口边缘 resize 命中区

    def __init__(self, window: QWidget, side: str, titlebar_h: int):
        super().__init__(window, Qt.Tool | Qt.FramelessWindowHint)
        assert side in ("left", "right")
        self._side = side
        self._titlebar_h = titlebar_h
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)  # 浮层自身接收 HoverEnter/Leave
        self.setObjectName("hoverPreviewOverlay")
        self._slot_layout = QVBoxLayout(self)
        self._slot_layout.setContentsMargins(0, 0, 0, 0)  # 几何全部交给 place()
        self._slot_layout.setSpacing(0)
        self._content: QWidget | None = None
        self._slide: QVariantAnimation | None = None
        self._target_w = 0
        self._current_w = 0  # 当前呈现宽度（动画逐帧更新，sync_to_window 用）
        self.hide()

    # ── 定位：主窗口局部坐标 → 全局屏幕坐标 ──

    def _place_at_width(self, w: int) -> None:
        """把浮层摆到主窗口边缘：顶接标题栏、底接窗口底、外缘对齐（全局坐标）。"""
        win = self.parentWidget()
        if win is None:
            return
        ww, wh = win.width(), win.height()
        top = self._titlebar_h
        h = max(0, wh - top)
        w = max(0, min(int(w), ww - self.EDGE_INSET))
        local_x = (ww - self.EDGE_INSET - w) if self._side == "right" else self.EDGE_INSET
        g = win.mapToGlobal(QPoint(local_x, top))
        self._current_w = w
        self.setGeometry(g.x(), g.y(), w, h)

    def place(self, width: int) -> None:
        """按窗口当前尺寸与目标宽度定位浮层。"""
        self._target_w = int(width)
        self._place_at_width(width)

    def sync_to_window(self) -> None:
        """主窗口 move/resize 后重定位（保持当前宽度；动画期每帧自跟随，无需调用）。"""
        if self.isVisible():
            self._place_at_width(self._current_w)

    def set_content(self, widget: QWidget) -> None:
        """把侧栏外层 frame 挂入浮层。"""
        if self._content is widget:
            return
        self.clear_content()
        widget.setParent(self)
        self._slot_layout.addWidget(widget)
        widget.show()
        self._content = widget

    def clear_content(self) -> None:
        """从浮层摘出内容 widget（不销毁、不 setParent(None)；reparent 交调用方）。"""
        if self._content is not None:
            self._slot_layout.removeWidget(self._content)
            self._content = None

    # ── 显隐（fade 语义保留为直切，动画走 slide_in/slide_out 几何滑入滑出） ──

    def fade_in(self) -> None:
        """直接显出并提到最顶（owned 顶层窗口天然盖住主窗口与 WebEngine）。"""
        self.show()
        self.raise_()

    def fade_out(self, on_done=None) -> None:
        self.hide()
        if on_done is not None:
            on_done()

    # ── 几何滑入/滑出（逐帧全局坐标 setGeometry） ──

    def slide_in(self, target_w: int, on_done=None) -> None:
        """从贴边外缘向内滑到 target_w（180ms OutCubic）。滑入期覆盖的对话区不 resize。"""
        self._target_w = int(target_w)
        self._place_at_width(0)
        self.show()
        self.raise_()
        anim = QVariantAnimation(self)
        anim.setDuration(180)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(float(self._target_w))
        anim.valueChanged.connect(lambda v: self._place_at_width(int(v)))
        if on_done is not None:
            anim.finished.connect(on_done)
        self._slide = anim  # 持引用防 GC
        anim.start()

    def slide_out(self, on_done=None) -> None:
        """从当前宽滑回贴边外缘（150ms OutQuad），动画结束调 on_done（交宿主 reparent 回挂）。"""
        anim = QVariantAnimation(self)
        anim.setDuration(150)
        anim.setEasingCurve(QEasingCurve.OutQuad)
        anim.setStartValue(float(self._current_w))
        anim.setEndValue(0.0)
        anim.valueChanged.connect(lambda v: self._place_at_width(int(v)))
        if on_done is not None:
            anim.finished.connect(on_done)
        self._slide = anim
        anim.start()


class HoverPreviewController:
    """hover 悬浮预览状态机：按钮/浮层的进出事件 + 可取消的缓收计时。

    不持有业务数据、不读写显隐记忆；通过回调把「进入/退出预览」的具体动作
    （reparent、落位、还原 splitter）交给宿主。
    """

    def __init__(self, overlay, can_preview, on_enter, on_leave, hide_delay_ms=300):
        self._overlay = overlay
        self._can_preview = can_preview
        self._on_enter = on_enter
        self._on_leave = on_leave
        self._previewing = False
        self._hide_timer = QTimer()
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(int(hide_delay_ms))
        self._hide_timer.timeout.connect(self._do_leave)

    def is_previewing(self) -> bool:
        return self._previewing

    def on_button_hover(self, on: bool) -> None:
        if on:
            self._cancel_hide()
            if not self._previewing and self._can_preview():
                self._previewing = True
                self._on_enter()
        else:
            self._start_hide_if_previewing()

    def on_overlay_hover(self, on: bool) -> None:
        if on:
            self._cancel_hide()
        else:
            self._start_hide_if_previewing()

    def on_clicked(self) -> None:
        self._cancel_hide()
        if self._previewing:
            self._do_leave()

    def _start_hide_if_previewing(self) -> None:
        if self._previewing:
            self._hide_timer.start()

    def _cancel_hide(self) -> None:
        self._hide_timer.stop()

    def _do_leave(self) -> None:
        self._hide_timer.stop()
        if self._previewing:
            self._previewing = False
            self._on_leave()
