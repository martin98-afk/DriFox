# -*- coding: utf-8 -*-
"""ExpandSettingCard 动态高度接管 + 高度动画（mixin）

## 背景

qfluentwidgets 的 `ExpandSettingCard` 用这套机制做展开/折叠：

1. 内容下方塞一个等高的 `spaceWidget` 占位；
2. 用动画驱动 `verticalScrollBar().value`（展开时 h→0，折叠时 0→maximum）；
3. `_onExpandValueChanged` 用 `fixedHeight = cardH + contentH - scrollValue` 反推高度。

隐含前提是**动画那 200ms 里内容高度不变**。卡片内容是动态挂载时（分批构建、
可展开的子区域、搜索过滤）这个前提就不成立——scrollBar 的 maximum 与实际内容
高度失配，表现为折叠后残留大片空白、或展开后底部被裁掉。

## 本 mixin 的做法

- 停掉基类的滚动动画，`spaceWidget` 归零；
- 折叠 = `fixedHeight` 只留 header，展开 = header + 内容实测高度；
- 展开/折叠改走高度动画（`_EXPAND_DURATION` ms）：逐帧 `setFixedHeight`，
  每帧同步滚动条（展开贴顶 / 折叠贴底），结束后再按实测内容高定格——
  观感与基类动画一致，又不依赖 spaceWidget 占位；
- 内容在动画期间继续增长（分批构建）时不打断动画，动画结束统一收敛到实测高度；
- 把被基类 `wheelEvent` 空实现吞掉的滚轮事件 `ignore()` 掉，交还外层滚动区。

使用方只需保证：**任何改变内容的操作之后调一次 `_adjust_view_size()`**。

不在本包内的设置卡（如插件自带配置卡）可直接用模块级 `animate_expand_height()`
复用同一套高度动画。
"""

from loguru import logger
from PyQt5.QtCore import QEasingCurve, QObject, QPropertyAnimation, QTimer, pyqtProperty
from PyQt5.QtWidgets import QApplication, QScrollArea, QWidget

# 高度校正的最大帧数。新挂载的行要等布局跑完才会被 sizeHint() 计入，
# 分批构建期间每批都会改变高度，因此校正到「连续两帧高度一致」为止。
_RESYNC_ROUNDS = 6

# 展开/折叠动画时长（ms）：与基类 expandAni 的 200ms 保持一致。
# ⚠️ 模块级常量而非类属性：热重载对旧实例打补丁时类属性不一定存在（见 P005）。
_EXPAND_DURATION = 200


def _invoke_card_callback(card, attr: str) -> None:
    """调用卡片上挂着的动画回调（未挂 / 控件已销毁时静默跳过）"""
    cb = getattr(card, attr, None)
    if cb is None:
        return
    try:
        cb()
    except RuntimeError as e:
        # 卡片已被销毁（设置面板关闭）时 Qt 对象失效，忽略即可
        logger.debug(f"[ExpandHeightMixin] 高度动画回调跳过（控件已销毁）: {e}")


class _CardHeightDriver(QObject):
    """高度动画驱动对象：把动画值逐帧写回卡片的 fixedHeight

    为什么不用 `QPropertyAnimation(card, b"maximumHeight")`：改上限只让父布局
    有机会重排，控件脱离布局（或尚无父级）时高度纹丝不动，动画等于没跑。
    这里用中间对象的属性驱动，逐帧 `setFixedHeight`，与 mixin 的高度定格
    方式完全一致，不依赖外部布局。
    """

    def __init__(self, card):
        super().__init__(card)
        self._card = card
        self._value = 0

    def _get_value(self) -> int:
        return self._value

    def _set_value(self, value) -> None:
        self._value = int(value)
        try:
            self._card.setFixedHeight(self._value)
        except RuntimeError:
            pass  # 卡片已销毁（设置面板关闭）

    value = pyqtProperty(int, _get_value, _set_value)


def _card_height_animation(card) -> QPropertyAnimation:
    """取（或惰性创建）卡片的高度动画对象

    动画逐帧把卡片高度写到目标值，结束后由调用方 `setFixedHeight` 定格。
    对象挂在卡片上复用，避免每次展开都新建。
    """
    ani = getattr(card, "_expand_height_ani", None)
    if ani is None:
        driver = _CardHeightDriver(card)
        ani = QPropertyAnimation(driver, b"value", card)
        ani.setDuration(_EXPAND_DURATION)
        ani.setEasingCurve(QEasingCurve.OutCubic)
        ani.finished.connect(lambda: _invoke_card_callback(card, "_expand_height_finish"))
        ani.valueChanged.connect(lambda _v: _invoke_card_callback(card, "_expand_height_tick"))
        card._expand_height_driver = driver
        card._expand_height_ani = ani
    return ani


def animate_expand_height(card, target: int, on_finished=None) -> bool:
    """把卡片高度从当前值动画到 `target`

    动画期间逐帧 `setFixedHeight`（收紧 min/max，不留回弹空间）；调用方在
    `on_finished` 里重新定格实际高度（此时内容高度可能仍在变）。

    Args:
        card: 目标卡片（ExpandSettingCard 或其子类）
        target: 目标高度（px）
        on_finished: 动画结束回调

    Returns:
        True = 已启动动画；False = 高度已等于目标（调用方直接定格即可）
    """
    target = max(0, int(target))
    start = int(card.height())
    if target == start:
        return False
    card._expand_height_finish = on_finished
    ani = _card_height_animation(card)
    ani.stop()
    ani.setStartValue(start)
    ani.setEndValue(target)
    ani.start()
    return True


class DynamicHeightExpandCardMixin:
    """动态内容的 ExpandSettingCard 高度接管 + 高度动画 + 滚轮冒泡

    必须与 `ExpandSettingCard`（或其子类）一起继承，且放在前面：

        class MyCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
            ...
    """

    _adjusting = False  # resizeEvent → _adjust_view_size 的重入保护
    _height_resync_queued = False  # 下一帧高度校正是否已排队
    _resync_rounds = 0  # 剩余校正轮数（收敛式校正，见 _adjust_view_size）
    _expanding = False  # 高度动画进行中（期间不直接改高度，避免打断动画）

    # ── 钩子（子类按需覆盖） ──

    def on_after_expand(self, is_expand: bool) -> None:
        """展开/折叠完成后的回调

        典型用途：折叠时暂停了分批构建，展开时在这里续上。
        """

    # ── 展开/折叠 ──

    def setExpand(self, isExpand: bool):  # noqa: N802 - 与基类同名
        if self.isExpand == isExpand:
            return
        self.expandAni.stop()
        self.isExpand = isExpand
        self.setProperty("isExpand", isExpand)
        self.setStyle(QApplication.style())
        self.card.expandButton.setExpand(isExpand)
        # spaceWidget 是基类滚动动画的占位件，接管后不再需要
        self.spaceWidget.setFixedHeight(0)
        if isExpand:
            # 量高度前内容必须可见（QLayout.sizeHint 忽略隐藏控件）
            self.view.setVisible(True)
            self._animate_to(self._expanded_height())
        else:
            # 折叠方向内容保持可见到高度收完，否则内容会在收缩途中凭空消失
            self._animate_to(self.card.height())
        self.on_after_expand(isExpand)

    def toggleExpand(self):  # noqa: N802 - 与基类同名
        self.setExpand(not self.isExpand)

    # ── 高度动画 ──

    def _expanded_height(self) -> int:
        """展开态应有的总高度 = header + 内容实测高度"""
        self._invalidate_layouts()
        self.viewLayout.activate()
        return self.card.height() + self.viewLayout.sizeHint().height()

    def _animate_to(self, target: int) -> None:
        """高度过渡到 target；高度已就位则直接定格"""
        self._expanding = True
        self._resync_rounds = 0  # 动画期间不做逐帧收敛校正
        # 回调懒挂：热重载后已存在的旧实例同样能接上
        self._expand_height_tick = self._on_expand_height_tick
        if not animate_expand_height(self, target, self._finish_height_anim):
            self._finish_height_anim()

    def _finish_height_anim(self) -> None:
        """动画结束：折叠收尾 → 按实测高度定格 → 补一轮收敛校正

        内容分批构建时动画期间高度还在涨，故这里再走一次收敛校正补齐。
        """
        self._expanding = False
        if not self.isExpand:
            # 折叠完成才隐藏内容：QLayout.sizeHint() 会忽略隐藏控件，不隐藏的话
            # 搜索过滤留下的隐藏行仍会被算进高度
            try:
                self.view.setVisible(False)
            except RuntimeError:
                return
        self._sync_height_now()
        self._adjust_view_size()

    def _on_expand_height_tick(self) -> None:
        """动画每帧：让内容随高度变化滚动（展开贴顶 / 折叠贴底）

        基类动画驱动的是滚动条、视觉上内容滑出视口；高度动画不滚动内容，
        故每帧手动跟随，保持同款观感。
        """
        try:
            bar = self.verticalScrollBar()
            bar.setValue(0 if self.isExpand else bar.maximum())
        except RuntimeError:
            pass

    # ── 高度同步 ──

    def _adjust_view_size(self):
        """同步高度：立刻做一次，随后几帧继续校正直到高度稳定

        新挂载的行在**下一帧**布局完成后才会被 `sizeHint()` 计入——单次校正
        不够，分批构建期间每批都会让高度变化。故这里做「收敛式校正」：
        本帧同步一次，再排至多 `_RESYNC_ROUNDS` 帧，高度不再变化就停。
        """
        self._sync_height_now()
        self._resync_rounds = _RESYNC_ROUNDS
        if not self._height_resync_queued:
            self._height_resync_queued = True
            QTimer.singleShot(0, self._sync_height_deferred)

    def _sync_height_deferred(self):
        if getattr(self, "_expanding", False):
            # 动画进行中：高度归动画管，结束后由 _finish_height_anim 统一校正
            self._height_resync_queued = False
            return
        before = self.height()
        self._sync_height_now()
        self._resync_rounds -= 1
        if self._resync_rounds > 0 and self.height() != before:
            # 高度还在变（新行刚布局完）→ 再校一帧
            QTimer.singleShot(0, self._sync_height_deferred)
        else:
            self._height_resync_queued = False

    def _invalidate_layouts(self):
        """递归失效 view 下所有布局的 sizeHint 缓存

        ⚠️ 关键：只失效 `viewLayout` 是不够的。子控件隐藏不会同步清理**中间层**
        （分组小节 / 行容器）的布局缓存，读出来的 sizeHint 仍带着已隐藏行的
        高度——表现为「收起后残留一截空白」。
        """
        self.viewLayout.invalidate()
        for child in self.view.findChildren(QWidget):
            lay = child.layout()
            if lay is not None:
                lay.invalidate()

    def _sync_height_now(self):
        if getattr(self, "_expanding", False):
            # 动画进行中：直接 setFixedHeight 会打断动画，交由结束回调校正
            return
        try:
            if not self.isExpand:
                self.verticalScrollBar().setValue(0)
                target = self.card.height()
            else:
                target = self._expanded_height()
            if target != self.height():
                self.setFixedHeight(target)
            area = self._ancestor_scroll_area(self)
            if area is not None:
                inner = area.widget()
                if inner is not None:
                    inner.updateGeometry()
        except RuntimeError as e:
            # 卡片已被销毁（设置面板关闭）时 Qt 对象失效，忽略即可
            logger.debug(f"[ExpandHeightMixin] 高度同步跳过（控件已销毁）: {e}")

    def resizeEvent(self, e):
        """宽度变化会改变文本换行 → 内容高度跟着变，需重新同步"""
        super().resizeEvent(e)
        if self.isExpand and not self._adjusting:
            self._adjusting = True
            try:
                self._adjust_view_size()
            finally:
                self._adjusting = False

    # ── 滚轮 ──

    @staticmethod
    def _ancestor_scroll_area(widget):
        """向上找最近的祖先 QScrollArea（本类自身就是，故从 parent 起找）"""
        node = widget.parentWidget()
        while node is not None:
            if isinstance(node, QScrollArea):
                return node
            node = node.parentWidget()
        return None

    def wheelEvent(self, e):
        """把滚轮事件交还给外层分页滚动区

        基类把 `wheelEvent` 空实现（它自身不开滚动条、滚轮对它无意义），但空实现
        不会 `ignore()` → 事件被吞掉，外层滚动区收不到 → 鼠标停在卡片上滚轮无反应。
        """
        e.ignore()
