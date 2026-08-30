# -*- coding: utf-8 -*-
"""ExpandSettingCard 动态高度接管（mixin）

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
- 把被基类 `wheelEvent` 空实现吞掉的滚轮事件 `ignore()` 掉，交还外层滚动区。

使用方只需保证：**任何改变内容的操作之后调一次 `_adjust_view_size()`**。
"""

from loguru import logger
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QScrollArea, QWidget

# 高度校正的最大帧数。新挂载的行要等布局跑完才会被 sizeHint() 计入，
# 分批构建期间每批都会改变高度，因此校正到「连续两帧高度一致」为止。
_RESYNC_ROUNDS = 6


class DynamicHeightExpandCardMixin:
    """动态内容的 ExpandSettingCard 高度接管 + 滚轮冒泡

    必须与 `ExpandSettingCard`（或其子类）一起继承，且放在前面：

        class MyCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
            ...
    """

    _adjusting = False  # resizeEvent → _adjust_view_size 的重入保护
    _height_resync_queued = False  # 下一帧高度校正是否已排队
    _resync_rounds = 0  # 剩余校正轮数（收敛式校正，见 _adjust_view_size）

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
        # 折叠时彻底隐藏内容：QLayout.sizeHint() 会忽略隐藏控件，不隐藏的话
        # 搜索过滤留下的隐藏行仍会被算进高度
        self.view.setVisible(isExpand)
        self._adjust_view_size()
        self.on_after_expand(isExpand)

    def toggleExpand(self):  # noqa: N802 - 与基类同名
        self.setExpand(not self.isExpand)

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
        try:
            if not self.isExpand:
                self.verticalScrollBar().setValue(0)
                target = self.card.height()
            else:
                self._invalidate_layouts()
                self.viewLayout.activate()
                target = self.card.height() + self.viewLayout.sizeHint().height()
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
