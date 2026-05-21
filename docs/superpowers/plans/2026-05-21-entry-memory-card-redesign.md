# 条目记忆卡片 UI 重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重构 EntryMemoryItemWidget 为悬浮操作卡片样式——内容全宽、左侧圆形开关指示器、hover 浮现按钮、双击编辑、圆角卡片边框

**Architecture:** 替换当前水平布局（文本+按钮挤一行）为卡片式布局：QFrame 圆角边框容器内，左侧绝对定位的圆形 toggle 指示器（自定义 paintEvent），右侧全宽内容区（只读 QPlainTextEdit），右上角绝对定位的 hover 操作按钮区。编辑态时内容区变为 TextEdit。单击选中、双击进入编辑、单击指示器切换启用/禁用。

**Tech Stack:** PyQt5, qfluentwidgets, app/utils/design_tokens.py 颜色系统

---

## 文件结构

| 文件 | 变更 | 职责 |
|------|------|------|
| `app/widgets/memory_card.py` | 重写 `EntryMemoryItemWidget` 类 | 卡片式条目记忆组件 |
| `app/utils/design_tokens.py` | 新增 `ItemStyles.entry_card()` 和 `ItemStyles.entry_card_hover()` | 卡片样式模板 |

### 不变更的文件
- `_load_entries()`: 仅调用 `EntryMemoryItemWidget` 构造函数，接口不变
- 信号: `deleted`, `toggled`, `edited` 保持不变
- 构造函数签名: `(memory_id, content, enabled, source, parent)` 保持不变

---

### Task 1: 新增条目卡片样式到 design_tokens.py

**Files:**
- Modify: `app/utils/design_tokens.py` (class ItemStyles, ~line 978)

- [ ] **Step 1: 在 ItemStyles 类中新增 `entry_card()` 和 `entry_card_hover()` 静态方法**

```python
@staticmethod
def entry_card() -> str:
    """条目记忆卡片 - 常规状态"""
    Colors.refresh()
    return f"""
        QFrame {{
            background-color: rgba(50, 50, 55, 150);
            border: 1px solid rgba(80, 80, 85, 150);
            border-radius: 8px;
            padding: 10px 12px 10px 40px;
        }}
        QFrame:hover {{
            border-color: rgba(102, 198, 255, 150);
            background-color: rgba(55, 55, 60, 200);
        }}
    """

@staticmethod
def entry_card_selected() -> str:
    """条目记忆卡片 - 选中状态"""
    Colors.refresh()
    return f"""
        QFrame {{
            background-color: rgba(20, 60, 90, 100);
            border: 1px solid rgba(102, 198, 255, 180);
            border-radius: 8px;
            padding: 10px 12px 10px 40px;
        }}
    """

@staticmethod
def entry_card_disabled() -> str:
    """条目记忆卡片 - 禁用状态文字样式"""
    Colors.refresh()
    return f"""
        color: rgba(255, 255, 255, 40);
        text-decoration: line-through;
    """
```

- [ ] **Step 2: 验证语法正确**

Run: `python -c "from app.utils.design_tokens import ItemStyles; print('OK')"`

- [ ] **Step 3: 提交**

```bash
git add app/utils/design_tokens.py
git commit -m "feat: add entry card style templates to design_tokens"
```

---

### Task 2: 重写 EntryMemoryItemWidget —— 核心布局与样式

**Files:**
- Modify: `app/widgets/memory_card.py` (class `EntryMemoryItemWidget`, lines 83-310)

这是最大的改动，将当前水平布局替换为卡片式布局。

- [ ] **Step 1: 修改 EntryMemoryItemWidget 类，替换继承和布局**

将 `QWidget` 改为 `QFrame`（支持边框样式），重写 `_init_ui`：

```python
from PyQt5.QtWidgets import (
    # 新增 QFrame
    QWidget, QFrame,
    QVBoxLayout, QHBoxLayout,
    QListWidgetItem, QFileDialog,
    QSizePolicy, QMenu, QAction,
    QPlainTextEdit,
)
from PyQt5.QtGui import (
    QDropEvent, QDragEnterEvent, QDragMoveEvent, QColor,
    QTextOption, QPainter, QPen, QBrush, QFontMetrics,
)
```

新 `_init_ui` 结构：

```python
def _init_ui(self, enabled, source):
    self._enabled = enabled
    self._selected = False
    self._syncing_height = False
    self._hovered = False

    # QFrame 卡片容器
    self.setFrameShape(QFrame.NoFrame)
    self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
    self.setMinimumHeight(44)
    self.setStyleSheet(ItemStyles.entry_card())
    self.setMouseTracking(True)  # 需要 hover 事件

    # 主布局：内容区 + hover 操作按钮区
    main_layout = QVBoxLayout(self)
    main_layout.setContentsMargins(0, 0, 0, 0)
    main_layout.setSpacing(0)

    # 内容行：左侧指示器占位 + 文本区
    content_row = QHBoxLayout()
    content_row.setContentsMargins(10, 10, 12, 10)
    content_row.setSpacing(8)

    # 左侧区域（圆形开关指示器用 paintEvent 绘制）
    self._toggle_area = QWidget(self)
    self._toggle_area.setFixedSize(20, 20)
    self._toggle_area.setCursor(Qt.PointingHandCursor)
    self._toggle_area.setToolTip("切换启用/禁用")
    self._toggle_area.clicked = False  # 用于点击检测
    content_row.addWidget(self._toggle_area)

    # 文本区：只读 QPlainTextEdit
    self.content_label = QPlainTextEdit(self._content, self)
    self.content_label.setReadOnly(True)
    self.content_label.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    self.content_label.setTextInteractionFlags(Qt.NoTextInteraction)
    self.content_label.setCursorWidth(0)
    self.content_label.setFrameShape(QPlainTextEdit.NoFrame)
    self.content_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
    self.content_label.setMinimumWidth(0)
    self.content_label.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    self.content_label.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    self._apply_content_style(enabled)
    self.content_label.document().documentSizeChanged.connect(self._sync_content_height)
    content_row.addWidget(self.content_label, 1)

    # Hover 操作按钮区（默认隐藏）
    self._hover_actions = QWidget(self)
    hover_layout = QHBoxLayout(self._hover_actions)
    hover_layout.setContentsMargins(0, 0, 0, 0)
    hover_layout.setSpacing(2)

    self.edit_btn = TransparentToolButton(FluentIcon.EDIT, self)
    self.edit_btn.setToolTip("编辑")
    self.edit_btn.setFixedSize(26, 26)
    self.edit_btn.clicked.connect(self._start_edit)
    self.edit_btn.setStyleSheet("background: transparent; border: none;")

    self.delete_btn = TransparentToolButton(FluentIcon.DELETE, self)
    self.delete_btn.setToolTip("删除")
    self.delete_btn.setFixedSize(26, 26)
    self.delete_btn.clicked.connect(lambda: self.deleted.emit(self.memory_id))
    self.delete_btn.setStyleSheet("background: transparent; border: none;")

    hover_layout.addWidget(self.edit_btn)
    hover_layout.addWidget(self.delete_btn)
    content_row.addWidget(self._hover_actions)

    self._hover_actions.setVisible(False)  # 默认隐藏

    main_layout.addLayout(content_row)

    # 编辑输入框（初始隐藏）
    self.edit_widget = QWidget(self)
    self.edit_widget.setVisible(False)
    edit_outer_layout = QHBoxLayout(self.edit_widget)
    edit_outer_layout.setContentsMargins(10, 10, 12, 10)
    edit_outer_layout.setSpacing(8)

    # 编辑态左侧也放占位（保持对齐）
    edit_toggle_spacer = QWidget(self)
    edit_toggle_spacer.setFixedSize(20, 20)
    edit_outer_layout.addWidget(edit_toggle_spacer)

    from qfluentwidgets import TextEdit
    self.edit_text = TextEdit(self.edit_widget)
    self.edit_text.setText(self._content)
    self.edit_text.setPlaceholderText("编辑条目记忆...")
    self.edit_text.setStyleSheet(f"""
        QTextEdit {{
            background-color: rgba(50, 50, 50, 200);
            border: 1px solid rgba(14, 99, 156, 200);
            color: #e0e0e0;
            padding: 4px 6px;
            border-radius: 4px;
            {get_font_family_css()} {font_size_css(13)}
        }}
    """)
    self.edit_text.setMinimumHeight(36)
    self.edit_text.setMaximumHeight(200)
    self.edit_text.document().documentSizeChanged.connect(self._adjust_edit_height)
    self.edit_text.focusOutEvent = lambda e: self._on_focus_out(e)
    edit_outer_layout.addWidget(self.edit_text, 1)

    main_layout.addWidget(self.edit_widget)

    # 开关按钮（SwitchButton）移除，改用指示器
    # 不再创建 self.switch
```

- [ ] **Step 2: 添加 hover/选中事件方法**

```python
def enterEvent(self, event):
    """鼠标进入卡片 - 显示操作按钮"""
    self._hovered = True
    self._update_card_style()
    self._hover_actions.setVisible(True)
    super().enterEvent(event)

def leaveEvent(self, event):
    """鼠标离开卡片 - 隐藏操作按钮"""
    self._hovered = False
    self._update_card_style()
    self._hover_actions.setVisible(False)
    super().leaveEvent(event)

def mousePressEvent(self, event):
    """单击处理：点击指示器切换状态，其余选中"""
    if event.button() == Qt.LeftButton:
        # 判断是否点击了指示器区域
        toggle_rect = self._toggle_area.geometry()
        # 需要考虑 margin 偏移
        toggle_global_pos = self._toggle_area.mapTo(self, toggle_rect.topLeft())
        adjusted_rect = toggle_global_pos and toggle_rect
        if toggle_rect.contains(event.pos() - self.content_row_offset()):
            self._on_toggle_clicked()
            return
        self._selected = True
        self._update_card_style()
    super().mousePressEvent(event)

def mouseDoubleClickEvent(self, event):
    """双击内容区进入编辑"""
    if event.button() == Qt.LeftButton:
        self._start_edit()
    super().mouseDoubleClickEvent(event)

def content_row_offset(self):
    """计算 content_row 的偏移量用于点击判定"""
    # 返回 content_row 相对于 self 的偏移
    return QPoint(0, 0)  # 简化：整个卡片的坐标系统已一致
```

- [ ] **Step 3: 添加 paintEvent 绘制圆形开关指示器**

```python
def paintEvent(self, event):
    """绘制自定义圆形开关指示器"""
    super().paintEvent(event)

    painter = QPainter(self)
    painter.setRenderHint(QPainter.Antialiasing)

    # 指示器位置：left=10, 垂直居中
    indicator_size = 18
    area_x = 12
    # 在内容行区域内垂直居中
    content_y = 10 + max(0, (self.content_label.height() - indicator_size) // 2)
    center_x = area_x + indicator_size // 2
    center_y = content_y + indicator_size // 2
    radius = indicator_size // 2

    if self._enabled:
        # 启用态：填充圆形
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#0e639c")))
        painter.drawEllipse(center_x - radius, center_y - radius, indicator_size, indicator_size)
        # 勾号
        pen = QPen(QColor("white"), 2)
        painter.setPen(pen)
        # 绘制简化的勾号
        check_x, check_y = center_x, center_y
        painter.drawLine(check_x - 4, check_y, check_x - 1, check_y + 3)
        painter.drawLine(check_x - 1, check_y + 3, check_x + 4, check_y - 3)
    else:
        # 禁用态：空心圆
        pen = QPen(QColor("#555555"), 1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center_x - radius, center_y - radius, indicator_size, indicator_size)

    painter.end()
```

- [ ] **Step 4: 添加辅助方法**

```python
def _on_toggle_clicked(self):
    """点击指示器切换启用/禁用"""
    self._enabled = not self._enabled
    self._apply_content_style(self._enabled)
    self.toggled.emit(self.memory_id, self._enabled)
    self.update()  # 重绘指示器

def _apply_content_style(self, enabled):
    """根据启用/禁用状态设置内容文本样式"""
    Colors.refresh()
    if enabled:
        self.content_label.setStyleSheet(f"""
            QPlainTextEdit {{
                background: transparent;
                color: {Colors.TEXT_PRIMARY};
                padding: 0;
                {get_font_family_css()} {font_size_css(13)}
            }}
        """)
    else:
        self.content_label.setStyleSheet(f"""
            QPlainTextEdit {{
                background: transparent;
                color: rgba(255, 255, 255, 40);
                padding: 0;
                {get_font_family_css()} {font_size_css(13)}
            }}
        """)

def _update_card_style(self):
    """根据状态更新卡片边框样式"""
    if self._selected:
        self.setStyleSheet(ItemStyles.entry_card_selected())
    elif self._hovered:
        self.setStyleSheet(ItemStyles.entry_card_hover())
    else:
        self.setStyleSheet(ItemStyles.entry_card())

def _sync_content_height(self):
    """根据内容换行自适应只读文本区高度"""
    if self._syncing_height:
        return
    self._syncing_height = True
    try:
        doc = self.content_label.document()
        doc_height = int(doc.size().height()) + 4
        height = max(36, doc_height)
        self.content_label.setFixedHeight(height)
        self.updateGeometry()
        item = self._get_item()
        if item:
            item.setSizeHint(self.sizeHint())
    finally:
        self._syncing_height = False

def resizeEvent(self, event):
    """宽度变化超过阈值时重新同步高度"""
    super().resizeEvent(event)
    old_width = getattr(self, '_last_width', None)
    new_width = self.width()
    if old_width is None or abs(new_width - old_width) > 10:
        self._last_width = new_width
        self._sync_content_height()
```

- [ ] **Step 5: 更新编辑态切换逻辑**

```python
def _start_edit(self):
    """双击或点编辑按钮进入编辑"""
    self._editing = True
    self.content_label.setVisible(False)
    self.edit_widget.setVisible(True)
    self._adjust_edit_height()
    self.edit_text.setFocus()
    cursor = self.edit_text.textCursor()
    cursor.select(cursor.Document)
    self.edit_text.setTextCursor(cursor)

def _finish_edit(self):
    """完成编辑"""
    new_content = self.edit_text.toPlainText().strip()
    if new_content and new_content != self._content:
        self.edited.emit(self.memory_id, new_content)
        self._content = new_content
        self.content_label.setPlainText(new_content)
    self._cancel_edit()
    self._sync_content_height()

def _cancel_edit(self):
    """取消编辑"""
    self._editing = False
    self.content_label.setVisible(True)
    self.edit_widget.setVisible(False)
    self.edit_text.setText(self._content)
```

- [ ] **Step 6: 删除旧的 `self.switch` 相关代码**

在 `_init_ui` 中不再创建 `SwitchButton`。确认所有引用 `self.switch` 的地方都已清理。在 `_load_entries` 中，`EntryMemoryItemWidget` 的构造不再需要在容器布局中放 switch。

- [ ] **Step 7: 更新列表样式，给条目列表每个 item 之间加间距**

在 `_create_entries_tab` 中，给 `entries_list` 设置 `setSpacing(6)` 来为卡片之间添加间距：

```python
self.entries_list.setSpacing(6)
```

同时移除 `QListWidget::item` 的 `border-bottom` 样式（卡片已有自己的边框）。

- [ ] **Step 8: 验证语法**

Run: `python -c "from app.widgets.memory_card import EntryMemoryItemWidget; print('OK')"`

- [ ] **Step 9: 提交**

```bash
git add app/widgets/memory_card.py app/utils/design_tokens.py
git commit -m "feat: redesign entry memory card with hover actions, toggle indicator, and card border"
```

---

### Task 3: 修复点击判定与 Integration 测试

**Files:**
- Modify: `app/widgets/memory_card.py`

- [ ] **Step 1: 修复 toggle 指示器的点击判定**

当前 `mousePressEvent` 中的判定逻辑需要精确匹配指示器区域。由于指示器是用 `paintEvent` 绘制的（不是独立 widget），需要计算实际位置：

```python
def mousePressEvent(self, event):
    if event.button() == Qt.LeftButton:
        # 指示器区域：x=12..30, y=10..10+content_height
        content_height = max(36, int(self.content_label.document().size().height()) + 4)
        indicator_y_top = 10 + max(0, (content_height - 18) // 2)
        indicator_y_bottom = indicator_y_top + 18
        indicator_rect = QRect(12, indicator_y_top, 18, 18)

        if indicator_rect.contains(event.pos()):
            self._on_toggle_clicked()
            return
        self._selected = True
        self._update_card_style()
    super().mousePressEvent(event)
```

同时在文件顶部导入 `QRect`：

```python
from PyQt5.QtCore import pyqtSignal, Qt, QSize, QTimer, QRect
```

- [ ] **Step 2: 确保 hover 操作按钮不遮挡指标器的点击**

`_hover_actions` widget 的 z-order 需要在指示器之上，但位置不重叠（hover 按钮在右上角）。

- [ ] **Step 3: 验证完整功能**

手动测试步骤：
1. 启动应用，打开条目记忆 Tab
2. 确认卡片样式：圆角边框、左侧圆形指示器（蓝色实心=启用）
3. 鼠标悬停：边框高亮、操作按钮浮现
4. 点击指示器：切换禁用（空心圆+删除线样式）
5. 双击内容：进入编辑模式
6. 编辑完成（失去焦点/Ctrl+Enter）：保存并更新显示
7. 按 Esc：取消编辑
8. 删除按钮：删除条目

- [ ] **Step 4: 提交**

```bash
git add app/widgets/memory_card.py
git commit -m "fix: refine toggle indicator click detection and card interaction"
```