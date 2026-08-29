# -*- coding: utf-8 -*-
"""通用流式布局 FlowLayout —— 子控件按可用宽度自动换行

为什么不用 QHBoxLayout（本类存在的理由）::

    QHBoxLayout.minimumSize().width() == 所有子项最小宽度 **之和**

    它会随子项数量线性增长，并沿布局链一路向上冒泡。放在 QSplitter 的某一侧时，
    QSplitter 必须满足该窗格的 minimumWidth，于是把另一侧窗格压到它的下限。
    本项目曾因此踩坑：输入框上方的附件栏每加一个 chip，左侧边栏就被压掉一截，
    最终塌到 60px 并触发 TabPanel 的自动折叠（阈值 100px）。

    FlowLayout.minimumSize().width() == 最宽 **单个** 子项的宽度

    因此容器永远可以被压缩到「一行放一个子项」的宽度，绝不撑爆父布局。

本类由 app/widgets/cards/settings/hook_setting_card.py 的 _FlowLayout 提炼而来，
并修正了原实现的两处缺陷：

1. ``sizeHint()`` 原样返回 ``minimumSize()``，父布局永远拿不到真实的多行高度。
   现记录上一次布局宽度，返回与之对应的 ``heightForWidth``。
2. 换行判定直接用 ``item.sizeHint()``，而子项在布局尚未激活时该值为 (0, 0)，
   会导致所有子项挤进第一行。现统一走 :meth:`_item_hint` 用 ``minimumSize`` 兜底。

用法::

    layout = FlowLayout(container, spacing=6)
    layout.addWidget(chip_a)
    layout.addWidget(chip_b)
"""

from PyQt5.QtCore import QPoint, QRect, QSize, Qt
from PyQt5.QtWidgets import QLayout, QLayoutItem, QSizePolicy


class FlowLayout(QLayout):
    """流式布局：子控件按可用宽度自动换行排列，支持左/中/右对齐"""

    def __init__(self, parent=None, spacing: int = 6, alignment=Qt.AlignLeft, margins=0):
        super().__init__(parent)
        self._spacing = int(spacing)
        self._alignment = alignment
        self._items: list[QLayoutItem] = []
        # 上一次实际布局的宽度，用于 sizeHint 返回真实的多行高度
        self._last_width = 0
        if isinstance(margins, int):
            self.setContentsMargins(margins, margins, margins, margins)
        else:
            self.setContentsMargins(*margins)

    # ==================== QLayout 抽象接口 ====================

    def addItem(self, item: QLayoutItem):
        self._items.append(item)
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def expandingDirections(self) -> Qt.Orientations:
        # 只在垂直方向需要更多空间：宽度不足时靠换行而非拉伸
        return Qt.Vertical

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, max(0, width), 0), test_only=True)

    def setGeometry(self, rect: QRect):
        super().setGeometry(rect)
        self._last_width = rect.width()
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        if self._last_width > 0:
            return QSize(self.minimumWidth(), self.heightForWidth(self._last_width))
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        # 宽度取「最宽单个子项」而非求和 —— 这是不撑爆父布局的关键
        w = h = 0
        for item in self._items:
            hint = self._item_hint(item)
            w = max(w, hint.width())
            h = max(h, hint.height())
        m = self.contentsMargins()
        if not self._items:
            return QSize(m.left() + m.right(), m.top() + m.bottom())
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumWidth(self) -> int:
        return self.minimumSize().width()

    # ==================== 内部实现 ====================

    @staticmethod
    def _item_hint(item: QLayoutItem) -> QSize:
        """子项有效尺寸

        ``sizeHint()`` 在布局尚未激活时普遍返回 (0, 0)（子 widget 从未 show 过），
        直接用它做换行判定会让所有子项挤进第一行。用 ``minimumSize()`` 取较大值兜底。
        """
        if item is None or item.isEmpty():
            return QSize(0, 0)
        hint = item.sizeHint()
        minimum = item.minimumSize()
        return QSize(max(hint.width(), minimum.width()), max(hint.height(), minimum.height()))

    def _split_rows(self, available: int):
        """按可用宽度把子项切成若干行（第一遍：只分行，不定位）"""
        rows: list[list[tuple[QLayoutItem, QSize]]] = []
        cur_row: list[tuple[QLayoutItem, QSize]] = []
        cur_width = 0
        for item in self._items:
            hint = self._item_hint(item)
            if hint.width() <= 0 and hint.height() <= 0:
                continue
            projected = cur_width + hint.width() + (self._spacing if cur_row else 0)
            # 仅当当前行非空时才换行：保证单个超宽子项也能独占一行，
            # 不会出现「无限换行 / 空行」的死循环。
            if cur_row and projected > available:
                rows.append(cur_row)
                cur_row = [(item, hint)]
                cur_width = hint.width()
            else:
                cur_row.append((item, hint))
                cur_width = projected
        if cur_row:
            rows.append(cur_row)
        return rows

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        """第二遍：逐行定位。返回所需总高度（含上下 margins）"""
        m = self.contentsMargins()
        available = max(0, rect.width() - m.left() - m.right())
        top = rect.y() + m.top()
        left = rect.x() + m.left()
        y = top

        for row in self._split_rows(available):
            row_width = sum(h.width() for _, h in row) + self._spacing * max(0, len(row) - 1)
            if self._alignment == Qt.AlignRight:
                x = left + max(0, available - row_width)
            elif self._alignment in (Qt.AlignHCenter, Qt.AlignCenter):
                x = left + max(0, (available - row_width) // 2)
            else:
                x = left

            line_height = max(h.height() for _, h in row)
            for item, hint in row:
                if not test_only:
                    # 行内垂直居中：高度不一的子项不会顶部参差
                    item_h = min(hint.height(), line_height)
                    item_y = y + (line_height - item_h) // 2
                    item.setGeometry(QRect(QPoint(x, item_y), QSize(hint.width(), item_h)))
                x += hint.width() + self._spacing
            y += line_height + self._spacing

        if self._items:
            y -= self._spacing
        content_h = max(0, y - top)
        return content_h + m.top() + m.bottom()


def make_flow_container(spacing: int = 6, margins=0, alignment=Qt.AlignLeft):
    """快捷构造：返回一个已挂载 FlowLayout 的 QWidget

    用于不关心容器本身、只想要一个「会换行的行容器」的场景。

    Returns:
        tuple[QWidget, FlowLayout]
    """
    from PyQt5.QtWidgets import QWidget

    container = QWidget()
    container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    layout = FlowLayout(container, spacing=spacing, alignment=alignment, margins=margins)
    return container, layout
