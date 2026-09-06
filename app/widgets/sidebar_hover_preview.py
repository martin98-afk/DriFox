from __future__ import annotations

from PyQt5.QtCore import QEasingCurve, QPoint, QVariantAnimation, Qt, QTimer
from PyQt5.QtWidgets import QWidget

from app.utils.design_tokens import Colors


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
    resizeEvent 时宿主调 sync_to_window() 重定位。已知代价：预览期拖动主窗口
    存在一帧滞后（hover 预览是临时态，用户此时通常不拖窗口，可接受）。

    展开语义是 reveal（揭示）而非 slide（滑动）：动画期内容 frame 固定在最终
    屏幕位置不动，浮层自身从窗口右缘向左扩展，像幕布从右缘向左拉开逐渐
    "揭示"内容。若让内容跟随浮层左缘一起移动/重排，观感是整块面板从屏幕
    右缘外飞进来（用户实测否决），故内容定位采用右对齐负偏移方案。
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
        # 主题联动背景（task2 遗留项落地）：独立顶层窗口不继承宿主 QSS，
        # 不设背景时画 palette 默认色（浅色），深色主题下呈白块。
        self.refresh_style()
        # 不用 layout：动画期内容 frame 由 _place_at_width 手动右对齐定位
        # （reveal 语义，见类 docstring），layout 会强制 frame 等于浮层宽导致内容跟滑
        self._content: QWidget | None = None
        self._slide: QVariantAnimation | None = None
        self._target_w = 0
        self._current_w = 0  # 当前呈现宽度（动画逐帧更新，sync_to_window 用）
        self.hide()

    def refresh_style(self) -> None:
        """主题切换 / 构造时刷新浮层背景（宿主 _on_theme_changed 调用）

        ★ 用 CONTENT_BG 实色而非 CARD_BG(alpha)：独立顶层窗口未开
        WA_TranslucentBackground，QSS rgba 会落成不透明底，半透明无意义。
        """
        self.setStyleSheet(f"#hoverPreviewOverlay {{ background: {Colors.CONTENT_BG}; }}")

    # ── 定位：主窗口局部坐标 → 全局屏幕坐标 ──

    def _place_at_width(self, w: int) -> None:
        """把浮层摆到主窗口边缘：顶接标题栏、底接窗口底、外缘对齐（全局坐标）。

        reveal 语义：内容 frame 固定在最终屏幕位置（右缘贴浮层右缘、宽度等于
        目标宽，左缘伸出浮层左侧被裁剪），浮层宽度从 0 → 目标宽只改变可见范围，
        内容在屏幕上的坐标全程静止。"""
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
        self._layout_content(h)

    def _layout_content(self, h: int) -> None:
        """reveal 定位：内容固定宽 _target_w、右缘贴浮层右缘（左缘负偏移被裁剪）。

        内容屏幕坐标 = 浮层全局x + (_current_w - _target_w) = 窗口右缘-EDGE_INSET
        -_target_w，与浮层当前宽无关 → 全程静止。稳定态 _current_w == _target_w
        时偏移归 0。左侧面板展开方向相反时改此处符号即可。"""
        c = self._content
        if c is None:
            return
        target = max(1, self._target_w)
        if self._side == "right":
            c.setGeometry(self._current_w - target, 0, target, h)
        else:
            c.setGeometry(0, 0, target, h)

    def place(self, width: int) -> None:
        """按窗口当前尺寸与目标宽度定位浮层。"""
        self._target_w = int(width)
        self._place_at_width(width)

    def sync_to_window(self) -> None:
        """主窗口 move/resize 后重定位（保持当前宽度；动画期每帧自跟随，无需调用）。"""
        if self.isVisible():
            self._place_at_width(self._current_w)

    def set_content(self, widget: QWidget) -> None:
        """把侧栏外层 frame 挂入浮层（reveal 手动定位，见 _layout_content）。"""
        if self._content is widget:
            return
        self.clear_content()
        widget.setParent(self)
        widget.show()
        self._content = widget
        self._layout_content(self.height())

    def clear_content(self) -> None:
        """从浮层摘出内容 widget。

        ★ 只清引用、不动 parent：宿主滑出回调里是「clear_content → frame.hide()
        → reparent 回 splitter」的顺序，若此处 setParent(None)，可见的 frame 会
        闪现成独立顶层窗口一帧。parent 交调用方 reparent。"""
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
