from __future__ import annotations

from PyQt5.QtCore import QEasingCurve, QVariantAnimation, Qt, QTimer
from PyQt5.QtWidgets import QVBoxLayout, QWidget


class HoverPreviewOverlay(QWidget):
    """侧栏 hover 悬浮预览的原生浮层容器。

    WA_NativeWindow → 覆盖在对话区 QWebEngineView（原生 HWND）之上不穿透；
    WA_ShowWithoutActivating → 弹出时不抢窗口焦点。贴窗口外缘一侧内缩
    EDGE_INSET 让出 frameless 主窗口的边缘 resize 热区。
    """

    EDGE_INSET = 6  # 贴外缘内缩，避让主窗口 resize 命中区

    def __init__(self, window: QWidget, side: str, titlebar_h: int):
        super().__init__(window)
        assert side in ("left", "right")
        self._side = side
        self._titlebar_h = titlebar_h
        # 作为父窗口的 native child：靠 WA_NativeWindow 成为原生 HWND 压住
        # 对话区 WebEngine；不设 WindowFlags（child 上无效）。
        self.setAttribute(Qt.WA_NativeWindow, True)
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
        self.hide()

    def place(self, width: int) -> None:
        """按窗口当前尺寸与目标宽度定位浮层（顶接标题栏、底接窗口底）。"""
        win = self.parentWidget()
        if win is None:
            return
        ww, wh = win.width(), win.height()
        top = self._titlebar_h
        h = max(0, wh - top)
        if self._side == "right":
            x = ww - self.EDGE_INSET - max(0, min(width, ww - self.EDGE_INSET))
            self.setGeometry(x, top, ww - self.EDGE_INSET - x, h)
        else:
            x = self.EDGE_INSET  # 左缘内缩让位 resize 热区
            w = max(0, min(width, ww - self.EDGE_INSET))
            self.setGeometry(x, top, w, h)

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

    def fade_in(self) -> None:
        """直接显出并提到最顶（native child 不支持 opacity 淡入淡出，见类注释）。"""
        self.show()
        self.raise_()

    def fade_out(self, on_done=None) -> None:
        self.hide()
        if on_done is not None:
            on_done()

    # ── 几何滑入/滑出（native child 不支持 opacity 动画，用逐帧 setGeometry） ──

    def _apply_width(self, w: int) -> None:
        """设浮层宽度：贴窗口外缘一侧固定，另一侧向内伸缩，顶/底不变。"""
        win = self.parentWidget()
        if win is None:
            return
        ww, wh = win.width(), win.height()
        top = self._titlebar_h
        h = max(0, wh - top)
        w = max(0, min(int(w), ww - self.EDGE_INSET))
        if self._side == "right":
            self.setGeometry(ww - self.EDGE_INSET - w, top, w, h)
        else:
            self.setGeometry(self.EDGE_INSET, top, w, h)

    def slide_in(self, target_w: int, on_done=None) -> None:
        """从贴边外缘向内滑到 target_w（180ms OutCubic）。滑入期覆盖的对话区不 resize。"""
        self._target_w = int(target_w)
        self._apply_width(0)
        self.show()
        self.raise_()
        anim = QVariantAnimation(self)
        anim.setDuration(180)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(float(self._target_w))
        anim.valueChanged.connect(lambda v: self._apply_width(int(v)))
        if on_done is not None:
            anim.finished.connect(on_done)
        self._slide = anim  # 持引用防 GC
        anim.start()

    def slide_out(self, on_done=None) -> None:
        """从当前宽滑回贴边外缘（150ms OutQuad），动画结束调 on_done（交宿主 reparent 回挂）。"""
        anim = QVariantAnimation(self)
        anim.setDuration(150)
        anim.setEasingCurve(QEasingCurve.OutQuad)
        anim.setStartValue(float(self.width()))
        anim.setEndValue(0.0)
        anim.valueChanged.connect(lambda v: self._apply_width(int(v)))
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
