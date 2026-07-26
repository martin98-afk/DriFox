# Tab 管理器面板渐变毛玻璃风格 & 折叠功能 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** TabPanel 升级为渐变毛玻璃风格 + 标题栏折叠按钮 + 折叠态 48px 图标条。

**Architecture:** 三模块推进 — `config.py` 加配置项 → `tab_panel.py` UI 重构（品牌区/渐变/Tab项/图标条/折叠切换） → `tab_manager_window.py` 标题栏按钮 + splitter 动画。

**Tech Stack:** PyQt5, qfluentwidgets, QVariantAnimation, QSS linear-gradient

---

### Task 1: Settings 新增 `tab_sidebar_collapsed` 配置项

**Files:**
- Modify: `app/utils/config.py`

- [ ] **Step 1: 在 Settings 类中新增配置项**

找到 `tab_panel_width` 附近（搜 `tab_panel_width`），在其下方添加：

```python
# app/utils/config.py
tab_sidebar_collapsed: bool = False  # Tab 管理器侧栏是否折叠
```

- [ ] **Step 2: 验证配置可读写**

```python
# 临时验证（不提交）
from app.utils.config import Settings
s = Settings.get_instance()
s.tab_sidebar_collapsed.value = True
assert s.tab_sidebar_collapsed.value is True
s.tab_sidebar_collapsed.value = False
```

Run: `python -c "from app.utils.config import Settings; s=Settings.get_instance(); s.tab_sidebar_collapsed.value=True; print(s.tab_sidebar_collapsed.value)"`

Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add app/utils/config.py
git commit -m "feat: tab-manager - add tab_sidebar_collapsed config option"
```

---

### Task 2: TabPanel._setup_ui — 添加品牌区块，移除 plugin_title

**Files:**
- Modify: `app/widgets/tab_panel.py`

- [ ] **Step 1: 在 _setup_ui 开头插入品牌区块，移除 plugin_title**

替换 `_setup_ui` 中从 `plugin_title = CaptionLabel(...)` 到 `layout.addWidget(self._plugin_scroll)` 之前的部分。

找到这段代码（约行 385-410）：
```python
        # ── 顶部：UI 插件标题（固定在滚动区外） ──
        plugin_title = CaptionLabel("UI 插件", self)
        self._plugin_title = plugin_title
        plugin_title.setAlignment(Qt.AlignCenter)
        plugin_title.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(13)}")
        layout.addWidget(plugin_title)
```

**删除以上全部**，替换为：

```python
        # ── 品牌区块 ──
        brand_widget = QWidget(self)
        brand_layout = QHBoxLayout(brand_widget)
        brand_layout.setContentsMargins(12, 10, 12, 6)
        brand_layout.setSpacing(8)

        # 品牌渐变图标
        brand_icon = QLabel(brand_widget)
        brand_icon.setFixedSize(22, 22)
        brand_icon.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #66c6ff, stop:1 #8b5cf6); border-radius: 6px;"
        )
        brand_layout.addWidget(brand_icon)

        # 品牌名称
        brand_name = QLabel("DriFox", brand_widget)
        brand_name.setObjectName("brandName")
        brand_layout.addWidget(brand_name)

        # 会话计数徽标
        self._session_count_label = QLabel("0 会话", brand_widget)
        self._session_count_label.setObjectName("sessionCountBadge")
        brand_layout.addWidget(self._session_count_label)

        layout.addWidget(brand_widget)

        # ── 顶部渐变发光线 ──
        self._glow_line = QWidget(self)
        self._glow_line.setFixedHeight(1)
        self._glow_line.setObjectName("glowLine")
        layout.addWidget(self._glow_line)
```

- [ ] **Step 2: 更新 __init__ —— 移除 _plugin_title 属性，新增 _session_count_label 和 _glow_line**

在 `__init__` 中：
```python
# 删除这一行：
self._plugin_title: Optional[CaptionLabel] = None
# 新增（在 _gitee_account_row 行之后）：
self._session_count_label: Optional[QLabel] = None
self._glow_line: Optional[QWidget] = None
self._collapsed: bool = False
```

- [ ] **Step 3: 更新 _refresh_plugin_style —— 移除对 _plugin_title 的引用**

找到 `_refresh_plugin_style` 方法：
```python
    def _refresh_plugin_style(self):
        if self._plugin_section is None:
            return
        if self._plugin_title is not None:
            self._plugin_title.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(13)}")
        for row in self._plugin_buttons:
            row.refresh_style()
```

删除 `_plugin_title` 相关行：
```python
    def _refresh_plugin_style(self):
        if self._plugin_section is None:
            return
        for row in self._plugin_buttons:
            row.refresh_style()
```

- [ ] **Step 4: 更新 refresh_ui_plugins —— 移除对 _plugin_title 的引用**

找到 `refresh_ui_plugins` 中：
```python
        if self._plugin_layout is None or self._plugin_section is None:
            return
        while self._plugin_layout.count() > 1:  # 保留索引 0 的标题
```

改为：
```python
        if self._plugin_layout is None or self._plugin_section is None:
            return
        while self._plugin_layout.count() > 0:  # 清除所有旧项
```

- [ ] **Step 5: 验证 UI 无报错**

Run: `pytest tests/widgets/test_tab_panel.py -v`
Expected: 所有现有测试 PASS（新增的 brand_widget 不影响测试逻辑）

- [ ] **Step 6: Commit**

```bash
git add app/widgets/tab_panel.py
git commit -m "feat: tab-panel - add brand header, remove plugin_title label"
```

---

### Task 3: TabPanel 样式升级 — 渐变背景、分隔线、新建按钮

**Files:**
- Modify: `app/widgets/tab_panel.py`

- [ ] **Step 1: 添加辅助方法 — 从 Colors.INFO 提取 RGB 通道**

在 `TabPanel` 类中添加静态辅助方法：

```python
@staticmethod
def _info_rgb() -> tuple:
    """返回 Colors.INFO 的 (r, g, b) 整数值"""
    c = _QColor(Colors.INFO)
    return c.red(), c.green(), c.blue()

@staticmethod
def _card_bg_rgb() -> tuple:
    """从 Colors.CARD_BG rgab 字符串提取 (r, g, b)"""
    s = Colors.CARD_BG
    # 格式: "rgba(r, g, b, {alpha})"
    try:
        if s.startswith("rgba("):
            parts = s.strip("rgba() ").split(",")
            return int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        pass
    return 33, 33, 38  # fallback
```

- [ ] **Step 2: 添加 _apply_panel_stylesheet 方法**

在 `TabPanel` 类中添加，将所有动态样式集中管理：

```python
def _apply_panel_stylesheet(self):
    """应用/刷新面板所有动态样式 (颜色/渐变/字体随主题变化)"""
    ir, ig, ib = self._info_rgb()
    cr, cg, cb = self._card_bg_rgb()
    cr2, cg2, cb2 = max(0, cr - 5), max(0, cg - 5), max(0, cb - 5)

    # ── 面板背景渐变 ──
    self.setStyleSheet(f"""
        #tabPanel {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba({cr},{cg},{cb}, 0.97),
                stop:0.5 rgba({cr},{cg},{cb}, 0.98),
                stop:1 rgba({cr2},{cg2},{cb2}, 0.99));
        }}
    """)

    # ── 顶部渐变发光线 ──
    self._glow_line.setStyleSheet(f"""
        #glowLine {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(0,0,0,0),
                stop:0.2 rgba({ir},{ig},{ib}, 0.35),
                stop:0.5 rgba(139, 92, 246, 0.35),
                stop:0.8 rgba({ir},{ig},{ib}, 0.35),
                stop:1 rgba(0,0,0,0));
            margin: 0 12px;
        }}
    """)

    # ── 品牌文字 ──
    for child in self.findChildren(QLabel):
        if child.objectName() == "brandName":
            child.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
                f"{font_size_css(13)} font-weight: 600;"
            )
        elif child.objectName() == "sessionCountBadge":
            child.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: rgba(255,255,255,0.05); "
                f"border-radius: 10px; padding: 2px 8px; {font_size_css(11)};"
            )

    # ── 新建按钮渐变 ──
    self._new_btn.setStyleSheet(f"""
        QPushButton {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 rgba({ir},{ig},{ib}, 0.12),
                stop:1 rgba(139, 92, 246, 0.08));
            border: 1px solid rgba({ir},{ig},{ib}, 0.18);
            color: {Colors.INFO};
            border-radius: 8px;
            padding: 6px 12px;
            {font_size_css(12)}
        }}
        QPushButton:hover {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 rgba({ir},{ig},{ib}, 0.20),
                stop:1 rgba(139, 92, 246, 0.15));
            border: 1px solid rgba({ir},{ig},{ib}, 0.35);
        }}
    """)

    # ── 渐变分隔线（Tab 列表下方） ──
    for child in self.findChildren(QFrame):
        if child.objectName() == "gradientDivider":
            child.setStyleSheet(f"""
                #gradientDivider {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 rgba(0,0,0,0),
                        stop:0.5 rgba(255,255,255, 0.08),
                        stop:1 rgba(0,0,0,0));
                    margin: 0 16px;
                    border: none;
                }}
            """)

    # ── 底部按钮扁平 hover ──
    self._settings_btn.setStyleSheet(f"""
        QPushButton {{
            background: transparent;
            color: {Colors.TEXT_MUTED};
            border: none;
            border-radius: 5px;
            padding: 4px 8px;
            {font_size_css(11)}
        }}
        QPushButton:hover {{
            background: rgba(255,255,255,0.06);
            color: {Colors.TEXT_PRIMARY};
        }}
    """)
```

- [ ] **Step 3: _setup_ui 中替换分隔线和按钮**

首先在文件顶部导入中添加 `QPushButton`（如果尚未导入）。找到：
```python
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
```
添加 `QPushButton`。

在 `_setup_ui` 中，把现有的 `separator` (分隔线) QFrame 的 objectName 设为 `gradientDivider`：

```python
        # ── 分隔线 ──
        separator = QFrame(self)
        separator.setObjectName("gradientDivider")  # 新增
        separator.setFrameShape(QFrame.HLine)
        separator.setFixedHeight(1)  # 新增
        layout.addWidget(separator)
```

同样修改 `account_separator`：
```python
        account_separator = QFrame(self)
        account_separator.setObjectName("gradientDivider")  # 新增
        account_separator.setFrameShape(QFrame.HLine)
        account_separator.setFixedHeight(1)  # 新增
        layout.addWidget(account_separator)
```

把新建按钮的包装从 `TransparentPushButton` 改为普通 `QPushButton`：

```python
        # ── 新建按钮 ──
        top_bar = QWidget(self)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(6, 6, 6, 4)
        self._new_btn = QPushButton("＋ 新建标签页", top_bar)   # 改为 QPushButton
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._new_btn.clicked.connect(self.newTabRequested.emit)
        top_layout.addWidget(self._new_btn)
        layout.addWidget(top_bar)
```

顶部也需要添加 `from PyQt5.QtWidgets import QPushButton`（已有导入则跳过）。

- [ ] **Step 4: 在 _setup_ui 末尾调用 _apply_panel_stylesheet**

在 `_setup_ui` 方法的最后一行（`theme_manager.register_refresh_target(self)` 之后）添加：

```python
        self._apply_panel_stylesheet()
```

- [ ] **Step 5: 更新 refresh_style 调用 _apply_panel_stylesheet**

在 `refresh_style` 方法开头添加：

```python
    def refresh_style(self):
        from app.utils.design_tokens import Colors as _Colors
        _Colors.refresh()
        self._apply_panel_stylesheet()  # 新增
        for item in self._items:
            ...
```

- [ ] **Step 6: 更新 add_tab/remove_tab 同步会话计数**

在 `add_tab` 返回前添加：
```python
        self._update_session_count()
```

在 `remove_tab` 末尾添加：
```python
        self._update_session_count()
```

新增 `_update_session_count` 方法：
```python
    def _update_session_count(self):
        if self._session_count_label:
            n = len(self._items)
            self._session_count_label.setText(f"{n} 会话")
```

- [ ] **Step 7: Commit**

```bash
git add app/widgets/tab_panel.py
git commit -m "feat: tab-panel - gradient panel bg, glow line, new-tab button, session count"
```

---

### Task 4: TabItem paintEvent 升级 — 渐变选中态 + 光晕

**Files:**
- Modify: `app/widgets/tab_panel.py`

- [ ] **Step 1: 修改 TabItem.paintEvent 中选中态绘制**

找到 `paintEvent` 中的选中态绘制（约行 232-235）：
```python
        if self._selected:
            painter.fillRect(self.rect(), _CACHED_SELECTED_BG)
```

替换为渐变填充 + 光晕：

```python
        if self._selected:
            from PyQt5.QtGui import QLinearGradient

            w, h = self.width(), self.height()
            grad = QLinearGradient(0, 0, w, h)
            grad.setColorAt(0.0, _QColor(_CACHED_INFO.red(), _CACHED_INFO.green(), _CACHED_INFO.blue(), 46))  # ~0.18
            grad.setColorAt(1.0, _QColor(139, 92, 246, 20))  # ~0.08
            painter.fillRect(self.rect(), grad)
```

然后在选中态指示条绘制前添加光晕（仍保留现有指示条逻辑不变）：

在 `elif self._selected:` 块（约行 258）的指示条绘制中：

```python
        elif self._selected:
            # 左侧选中指示条（渐变）
            h = self.height()
            y0, y1 = 4, h - 8
            grad = QLinearGradient(0, y0, 0, y1)
            grad.setColorAt(0.0, _CACHED_INFO)
            grad.setColorAt(1.0, _QColor(139, 92, 246))
            painter.fillRect(0, y0, 2, y1, grad)
```

将原来 `painter.fillRect(0, 4, 3, self.height() - 8, _CACHED_INFO)` 这一行替换为以上。

- [ ] **Step 2: 更新 TabItem._apply_title_style — 确保 hover 态透明**

`_apply_title_style` 无需大改，TabItem 的 hover 通过 `enterEvent`/`leaveEvent` 管理，`paintEvent` 中未处理 hover —— 那就在 `enterEvent` 中添加视觉反馈。

在 `enterEvent` 中（约行 223），除了设置关闭按钮可见外，添加背景变化：

```python
    def enterEvent(self, event):
        self._close_btn.setVisible(True)
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._selected:
            self._close_btn.setVisible(False)
        self._hovered = False
        self.update()
        super().leaveEvent(event)
```

在 `paintEvent` 中，选中态之前添加 hover 处理：

```python
        if not self._selected and self._hovered:
            painter.fillRect(self.rect(), _QColor(255, 255, 255, 10))  # ~0.04
```

需要在 `__init__` 中初始化 `self._hovered = False`。

- [ ] **Step 3: 验证测试**

Run: `pytest tests/widgets/test_tab_panel.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/widgets/tab_panel.py
git commit -m "feat: tab-panel - gradient active state on TabItem with glow indicator"
```

---

### Task 5: IconStripWidget — 折叠态图标条

**Files:**
- Modify: `app/widgets/tab_panel.py`

- [ ] **Step 1: 在 tab_panel.py 中新增 IconStripWidget 类**

在文件末尾（TabPanel 类之前或之后）添加新类：

```python
class IconStripWidget(QWidget):
    """折叠态图标条 — 仅显示项目图标 + 状态徽标 + 新建按钮"""

    tabSelected = pyqtSignal(int)
    newTabRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icons: list = []  # (QPixmap, streaming, error, question)
        self._active_index = -1
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedWidth(48)
        self.setObjectName("iconStrip")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 10, 6, 6)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignTop)

        # 底部 stretch 前插入图标
        self._glow_line = QWidget(self)
        self._glow_line.setFixedHeight(1)
        self._glow_line.setObjectName("iconStripGlowLine")
        layout.addWidget(self._glow_line)

        self._icon_layout = QVBoxLayout()
        self._icon_layout.setSpacing(6)
        layout.addLayout(self._icon_layout)

        layout.addStretch()

        # 新建按钮
        self._new_btn = QPushButton("+", self)
        self._new_btn.setFixedSize(32, 32)
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.clicked.connect(self.newTabRequested.emit)
        layout.addWidget(self._new_btn, alignment=Qt.AlignCenter)

    def set_icons(self, icons: list):
        """设置图标列表: [(QPixmap, streaming, error, question), ...]"""
        self._icons = icons
        self._rebuild()

    def set_active_index(self, idx: int):
        self._active_index = idx
        self._rebuild()

    def update_icon(self, idx: int, pixmap, streaming=False, error=False, question=False):
        """更新单个图标"""
        if 0 <= idx < len(self._icons):
            self._icons[idx] = (pixmap, streaming, error, question)
            self._rebuild()

    def _rebuild(self):
        # 清除旧图标
        while self._icon_layout.count():
            item = self._icon_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for i, (pix, streaming, error, question) in enumerate(self._icons):
            btn = QPushButton(self)
            btn.setFixedSize(32, 32)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("background: transparent; border: none;")
            if not pix.isNull():
                scaled = pix.scaled(30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                btn.setIcon(QIcon(scaled))
                btn.setIconSize(QSize(30, 30))

            # 点击信号
            idx = i
            btn.clicked.connect(lambda checked, i=idx: self.tabSelected.emit(i))

            # 容器 widget 用于叠加徽标
            container = QWidget(self)
            container.setFixedSize(32, 32)
            cl = QHBoxLayout(container)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.addWidget(btn)

            # 状态徽标
            if streaming or error or question:
                badge = QLabel(container)
                badge.setFixedSize(8, 8)
                badge.setStyleSheet(
                    f"background: {'#ef4444' if error else '#f59e0b' if question else '#60D4FF'}; "
                    f"border-radius: 4px; border: 2px solid rgba(30,35,55,0.97);"
                )
                badge.move(24, 0)
                badge.raise_()

            self._icon_layout.addWidget(container, alignment=Qt.AlignCenter)
```

- [ ] **Step 2: 在 TabPanel._setup_ui 中集成 IconStripWidget**

在 `_setup_ui` 底部（theme_manager.register 之前）添加：

```python
        # ── 折叠态图标条（初始隐藏，加入主布局 stretch=0） ──
        self._icon_strip = IconStripWidget(self)
        self._icon_strip.hide()
        self._icon_strip.tabSelected.connect(self._on_icon_strip_tab_selected)
        self._icon_strip.newTabRequested.connect(self.newTabRequested.emit)
        layout.addWidget(self._icon_strip)  # 必须在 layout 中，折叠时才能 fill 空间
```

在 `__init__` 中添加：
```python
        self._icon_strip: Optional[IconStripWidget] = None
```

- [ ] **Step 3: 添加 _on_icon_strip_tab_selected 回调**

```python
    def _on_icon_strip_tab_selected(self, idx: int):
        """折叠态图标条点击 Tab"""
        self.set_active_index(idx)
        self.tabSelected.emit(idx)
```

- [ ] **Step 4: 验证导入**

确保 `from PyQt5.QtCore import QSize` 在导入列表中。

- [ ] **Step 5: Commit**

```bash
git add app/widgets/tab_panel.py
git commit -m "feat: tab-panel - IconStripWidget for collapsed state"
```

---

### Task 6: TabPanel.set_collapsed — 折叠/展开切换

**Files:**
- Modify: `app/widgets/tab_panel.py`

- [ ] **Step 1: 添加 set_collapsed 方法和 _sync_icon_strip**

```python
    def set_collapsed(self, collapsed: bool):
        """切换到折叠/展开态"""
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed

        if collapsed:
            # 进入折叠态：隐藏展开内容，显示图标条
            self._sync_icon_strip()
            self._icon_strip.show()
            # 隐藏展开态的所有内容子控件
            for child in self.findChildren(QWidget):
                if child is self._icon_strip or child.parent() is self._icon_strip:
                    continue
                if child is self._icon_strip:
                    continue
                child.hide()
        else:
            # 展开：隐藏图标条，显示所有内容
            self._icon_strip.hide()
            for child in self.findChildren(QWidget):
                if child is self._icon_strip:
                    continue
                child.show()

    def _sync_icon_strip(self):
        """同步图标条数据：从当前 Tab 列表生成图标数据"""
        icons = []
        for item in self._items:
            pix = item._icon_pixmap
            if pix is None or (hasattr(pix, 'isNull') and pix.isNull()):
                pix = QPixmap()
            icons.append((pix, item._streaming, item._stream_error, item._question))
        self._icon_strip.set_icons(icons)
        self._icon_strip.set_active_index(self._active_index)
```

**重要：** `for child in self.findChildren(QWidget)` 会过于激进，把所有孙控件也隐藏。改用 `layout` 遍历：

```python
    def set_collapsed(self, collapsed: bool):
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed

        if collapsed:
            self._sync_icon_strip()
            self._icon_strip.show()
            # 隐藏主布局中除 icon_strip 之外的所有子控件
            main_layout = self.layout()
            if main_layout:
                for i in range(main_layout.count()):
                    w = main_layout.itemAt(i).widget()
                    if w and w is not self._icon_strip:
                        w.hide()
        else:
            self._icon_strip.hide()
            main_layout = self.layout()
            if main_layout:
                for i in range(main_layout.count()):
                    w = main_layout.itemAt(i).widget()
                    if w and w is not self._icon_strip:
                        w.show()
```

- [ ] **Step 2: 同步图标条数据到 add_tab / remove_tab**

在 `add_tab` 中，`self._update_session_count()` 之后：
```python
        if self._collapsed:
            self._sync_icon_strip()
```

在 `remove_tab` 中，`self._update_session_count()` 之后：
```python
        if self._collapsed:
            self._sync_icon_strip()
```

- [ ] **Step 3: 同步图标条到 update_tab_streaming / update_tab_question**

在 `update_tab_streaming` 末尾：
```python
        if self._collapsed:
            self._sync_icon_strip()
```

在 `update_tab_question` 末尾：
```python
        if self._collapsed:
            self._sync_icon_strip()
```

在 `set_active_index` 末尾：
```python
        if self._collapsed:
            self._icon_strip.set_active_index(index)
```

在 `update_tab_icon` 末尾：
```python
        if self._collapsed:
            self._sync_icon_strip()
```

- [ ] **Step 4: Commit**

```bash
git add app/widgets/tab_panel.py
git commit -m "feat: tab-panel - set_collapsed toggle with icon strip sync"
```

---

### Task 7: TabManagerTitleBar — 图标改为折叠按钮

**Files:**
- Modify: `app/widgets/tab_manager_window.py`

- [ ] **Step 1: 在 TabManagerTitleBar 中添加 toggleSidebarRequested 信号**

在类体顶部添加：
```python
    toggleSidebarRequested = pyqtSignal()
```

- [ ] **Step 2: 替换 _icon_label QLabel 为可点击按钮**

找到 `_setup_ui` 中的图标部分（约行 99-104）：
```python
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(20, 20)
        self._icon_label.setStyleSheet("background: transparent;")
        icon = QIcon(":/icons/drifox.ico")
        pix = icon.pixmap(20, 20)
        self._icon_label.setPixmap(pix)
        layout.addWidget(self._icon_label)
```

替换为：
```python
        # ── 折叠/展开侧栏按钮 ──
        self._sidebar_toggle_btn = _SidebarToggleButton(self)
        self._sidebar_toggle_btn.setFixedSize(28, 26)
        self._sidebar_toggle_btn.clicked.connect(self.toggleSidebarRequested.emit)
        layout.addWidget(self._sidebar_toggle_btn)
```

**需要将以下导入添加到 `tab_manager_window.py` 文件顶部模块级导入中：**

找到：
```python
from PyQt5.QtGui import QCloseEvent, QIcon, QMouseEvent
```
替换为：
```python
from PyQt5.QtGui import QCloseEvent, QColor, QIcon, QMouseEvent, QPainter, QPen
```

- [ ] **Step 3: 在文件顶部添加 _SidebarToggleButton 类**

在 `TabManagerTitleBar` 类定义之前添加：

```python
class _SidebarToggleButton(QPushButton):
    """侧栏折叠/展开按钮 — 绘制 <| 图标"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("折叠侧栏")
        self.setStyleSheet("""
            QPushButton { background: transparent; border: none; border-radius: 5px; }
            QPushButton:hover { background: rgba(255,255,255,0.08); }
        """)

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self.setToolTip("展开侧栏" if collapsed else "折叠侧栏")
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w // 2, h // 2
        pen = QPen(QColor(Colors.TEXT_MUTED), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)

        if self._collapsed:
            # 展开图标: ▷|
            p.drawLine(cx - 3, cy - 5, cx - 3, cy + 5)  # 竖线
            p.drawLine(cx + 3, cy - 5, cx - 1, cy)       # >
            p.drawLine(cx + 3, cy + 5, cx - 1, cy)
        else:
            # 折叠图标: |◁
            p.drawLine(cx + 3, cy - 5, cx + 3, cy + 5)  # 竖线
            p.drawLine(cx - 3, cy - 5, cx + 1, cy)       # <
            p.drawLine(cx - 3, cy + 5, cx + 1, cy)
        p.end()
```

- [ ] **Step 4: 在 TabManagerTitleBar.refresh_style 中更新按钮样式**

在 `_apply_style` 中添加按钮颜色（如果不在 stylesheet 中）：
```python
        self._sidebar_toggle_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; border-radius: 5px; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.08); }}
        """)
```

- [ ] **Step 5: 更新 set_maximized 方法引用**

`set_maximized` 中不再需要操作 `_icon_label`，只需确保不引用它。

- [ ] **Step 6: Commit**

```bash
git add app/widgets/tab_manager_window.py
git commit -m "feat: tab-manager - replace titlebar icon with sidebar toggle button"
```

---

### Task 8: TabManagerWindow — 折叠逻辑 + splitter 动画

**Files:**
- Modify: `app/widgets/tab_manager_window.py`

- [ ] **Step 1: 连接标题栏折叠信号**

在 `_setup_signals` 中添加：
```python
        self._title_bar.toggleSidebarRequested.connect(self._on_toggle_sidebar)
```

- [ ] **Step 2: 实现 _on_toggle_sidebar 方法**

```python
    def _on_toggle_sidebar(self):
        """切换侧栏展开/折叠（带动画）"""
        from PyQt5.QtCore import QVariantAnimation, QEasingCurve

        target_collapsed = not Settings.get_instance().tab_sidebar_collapsed.value

        # 目标宽度
        if target_collapsed:
            target_left = 48
        else:
            saved = Settings.get_instance().tab_panel_width.value
            target_left = saved if saved and saved > 48 else 200

        current_sizes = self._splitter.sizes()
        start_left = current_sizes[0]
        total = sum(current_sizes)
        target_right = total - target_left - self._splitter.handleWidth()

        # 动画
        anim = QVariantAnimation(self)
        anim.setDuration(200)
        anim.setStartValue(start_left)
        anim.setEndValue(target_left)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def on_value_changed(val):
            self._splitter.setSizes([int(val), total - int(val) - self._splitter.handleWidth()])

        anim.valueChanged.connect(on_value_changed)

        def on_finished():
            Settings.get_instance().tab_sidebar_collapsed.value = target_collapsed
            self._tab_panel.set_collapsed(target_collapsed)
            self._title_bar._sidebar_toggle_btn.set_collapsed(target_collapsed)

        anim.finished.connect(on_finished)
        anim.start()
```

- [ ] **Step 3: 同步初始折叠状态**

在 `_setup_ui` 末尾（`_apply_theme_stylesheet()` 之后）：

```python
        # 恢复折叠状态
        if Settings.get_instance().tab_sidebar_collapsed.value:
            # 延迟执行，等窗口首次显示后再折叠
            QTimer.singleShot(0, lambda: self._restore_collapsed_state())
```

添加方法：
```python
    def _restore_collapsed_state(self):
        """恢复上次的折叠状态（无动画）"""
        Settings.get_instance().tab_sidebar_collapsed.value = True
        self._splitter.setSizes([48, self.width() - 48 - self._splitter.handleWidth()])
        self._tab_panel.set_collapsed(True)
        self._title_bar._sidebar_toggle_btn.set_collapsed(True)
```

- [ ] **Step 4: 验证无报错**

```bash
python -c "from app.widgets.tab_manager_window import TabManagerWindow; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add app/widgets/tab_manager_window.py
git commit -m "feat: tab-manager - sidebar collapse/expand with QVariantAnimation"
```

---

### Task 9: 样式收尾 — 更新 TabPanel.refresh_style 和 _apply_panel_stylesheet 覆盖全面板

**Files:**
- Modify: `app/widgets/tab_panel.py`

- [ ] **Step 1: 确保 _apply_panel_stylesheet 覆盖 TabPanel 自身背景**

在 `_apply_panel_stylesheet` 中 `self.setStyleSheet()` 改为使用 `#tabPanel` 的 objectName 选择器，并确保包含 `objectName`：

检查 `_setup_ui` 中 `self.setObjectName("tabPanel")` 已存在（line 376 左右已存在）。

- [ ] **Step 2: 确保 refresh_style 刷新所有子组件**

在 `refresh_style` 中确保：
```python
    def refresh_style(self):
        from app.utils.design_tokens import Colors as _Colors
        _Colors.refresh()
        self._apply_panel_stylesheet()
        for item in self._items:
            item.refresh_style()
            item.repaint()
        self._refresh_plugin_style()
        if self._gitee_account_row is not None:
            self._gitee_account_row.refresh_style()
        # 折叠态也需要刷新
        if self._collapsed and self._icon_strip:
            self._sync_icon_strip()
```

- [ ] **Step 3: Commit**

```bash
git add app/widgets/tab_panel.py
git commit -m "fix: tab-panel - refresh_style covers all expanded/collapsed states"
```

---

### Task 10: 测试

**Files:**
- Modify: `tests/widgets/test_tab_panel.py`

- [ ] **Step 1: 测试品牌区块和会话计数**

```python
    def test_brand_header_present(self, panel):
        """品牌区块存在且会话计数初始为 0"""
        assert panel._session_count_label is not None
        assert panel._session_count_label.text() == "0 会话"

    def test_session_count_updates(self, panel):
        """添加/移除 Tab 时会话计数更新"""
        panel.add_tab("A")
        assert panel._session_count_label.text() == "1 会话"
        panel.add_tab("B")
        assert panel._session_count_label.text() == "2 会话"
        panel.remove_tab(0)
        assert panel._session_count_label.text() == "1 会话"
```

- [ ] **Step 2: 测试折叠/展开不崩溃**

```python
    def test_set_collapsed_toggle(self, panel):
        """折叠/展开切换不崩溃"""
        panel.add_tab("A")
        panel.add_tab("B")
        panel.set_collapsed(True)
        assert panel._collapsed is True
        panel.set_collapsed(False)
        assert panel._collapsed is False

    def test_collapsed_icon_strip_sync(self, panel):
        """折叠后图标条同步 Tab 数据"""
        panel.add_tab("A")
        panel.add_tab("B")
        panel.set_collapsed(True)
        assert panel._icon_strip is not None
        assert panel._icon_strip._active_index == panel._active_index

    def test_collapsed_add_remove_sync(self, panel):
        """折叠态下添加/移除 Tab 图标条同步"""
        panel.add_tab("A")
        panel.set_collapsed(True)
        panel.add_tab("B")
        # 不崩溃即可
```

- [ ] **Step 3: 运行全部测试**

```bash
pytest tests/widgets/test_tab_panel.py -v
```

Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add tests/widgets/test_tab_panel.py
git commit -m "test: tab-panel - brand header, session count, collapse tests"
```

---

### Task 11: 最终整合 — ruff + 完整测试

**Files:**
- Review: `app/widgets/tab_panel.py`, `app/widgets/tab_manager_window.py`

- [ ] **Step 1: 运行 lint**

```bash
ruff check app/widgets/tab_panel.py app/widgets/tab_manager_window.py
```

修复任何新增的 lint 错误。

- [ ] **Step 2: 运行全部单测**

```bash
pytest tests/widgets/test_tab_panel.py -v -x
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: tab-panel - lint and final polish for gradient glass redesign"
```
