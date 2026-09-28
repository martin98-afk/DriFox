# -*- coding: utf-8 -*-
"""
极简模型列表编辑器（嵌入式 Widget）
Enter 新增，Delete 删除，双击编辑，拖拽排序；
顶部输入框可搜索过滤 / 回车快速添加（支持换行与逗号分隔批量粘贴）；
重复项自动标红；可展示「被过滤的非对话模型」并点击加回。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, get_unified_scrollbar_style, scale_font_size
from app.utils.utils import get_font_family_css

_DUP_COLOR = "#e05656"  # 重复项前景色


class _ItemEditorDelegate(QStyledItemDelegate):
    """给 QListWidget 内置编辑器注入主题样式。

    双击编辑的编辑器由 delegate 创建，parent 是 viewport；QSS 后代选择器
    （QListWidget QLineEdit）对其不可靠，浅色主题下默认选中文本样式叠上
    item 选中背景后文字几乎看不见。改在 createEditor 里直接 setStyleSheet。
    """

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            Colors.refresh()
            editor.setStyleSheet(
                f"""
                QLineEdit {{
                    background-color: {Colors.CONTENT_BG};
                    color: {Colors.TEXT_PRIMARY};
                    border: 1px solid {Colors.INPUT_FOCUS_BORDER};
                    border-radius: 3px;
                    padding: 2px 4px;
                    {get_font_family_css()}
                    font-size: {scale_font_size(13)}px;
                    selection-background-color: {Colors.TEXT_ACCENT};
                    selection-color: #ffffff;
                }}
                """
            )
        return editor


def _split_model_input(text: str) -> list:
    """把用户输入拆成模型名列表：换行/逗号（含中文逗号）分隔，去空去重保序"""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").replace("，", ",")
    tokens = [t.strip() for chunk in raw.split("\n") for t in chunk.split(",")]
    seen, out = set(), []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class ModelListEditorWidget(QWidget):
    """极简模型列表编辑器 — 可内嵌到表单卡片中，点击按钮切换显隐"""

    def __init__(self, models: list | None = None, parent=None):
        super().__init__(parent)
        self._init_ui(models or [])
        self.refresh_style()

    def _build_qss(self) -> str:
        """构建主题 QSS（refresh_style 时重建，保证颜色/字号随系统）"""
        Colors.refresh()
        return f"""
            QWidget {{
                background: transparent;
            }}
            QListWidget {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                {get_font_family_css()}
                font-size: {scale_font_size(13)}px;
                outline: none;
            }}
            QListWidget::item {{
                background-color: transparent;
                padding: 4px 8px;
                border-radius: 3px;
            }}
            QListWidget::item:hover {{
                background-color: {Colors.HOVER_BG};
            }}
            QListWidget::item:selected {{
                background-color: {Colors.INPUT_FOCUS_BORDER};
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item:selected:hover {{
                background-color: {Colors.HOVER_BG_STRONG};
            }}
            QLineEdit {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
            }}
            QLineEdit:focus {{
                border-color: {Colors.INPUT_FOCUS_BORDER};
            }}
        """ + get_unified_scrollbar_style(6)

    def refresh_style(self):
        """主题/字号变更时刷新样式（由宿主卡片 refresh_style 链调用）"""
        self.setStyleSheet(self._build_qss())
        self.searchEdit.setPlaceholderText("搜索过滤；输入后回车添加（支持多行/逗号分隔）")
        self.hint_label.setStyleSheet(
            f"background: transparent; border: none; {get_font_family_css()}"
            f" font-size: {scale_font_size(11)}px; padding: 0;"
        )
        self.filtered_title.setStyleSheet(
            f"background: transparent; border: none; {get_font_family_css()}"
            f" font-size: {scale_font_size(11)}px; padding: 0; color: {Colors.TEXT_SECONDARY};"
        )

    def _make_item(self, text: str) -> QListWidgetItem:
        """创建可编辑列表项（QListWidget.addItems 的默认项不含 Editable 标记）"""
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        return item

    def _init_ui(self, models: list):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 搜索/快速添加框
        self.searchEdit = QLineEdit()
        self.searchEdit.setClearButtonEnabled(True)
        self.searchEdit.textChanged.connect(lambda _t: self._apply_filter())
        self.searchEdit.returnPressed.connect(self._on_search_return)
        layout.addWidget(self.searchEdit)

        # 提示行
        self.hint_label = QLabel(
            "<span style='color:#808080;'>双击编辑</span> · <span style='color:#606060;'>Enter 新增</span>"
            " · <span style='color:#606060;'>Delete 删除</span> · <span style='color:#606060;'>拖拽排序</span>"
            " · <span style='color:#606060;'>Ctrl+V 批量粘贴</span>"
        )
        self.hint_label.setStyleSheet(
            f"background: transparent; border: none; {get_font_family_css()}"
            f" font-size: {scale_font_size(11)}px; padding: 0;"
        )
        layout.addWidget(self.hint_label)

        # 主列表
        self.listWidget = QListWidget()
        # 最大高度：内容少时自适应矮，超出封顶后内部滚动
        self.listWidget.setMaximumHeight(200)
        self.listWidget.setDragDropMode(QListWidget.InternalMove)
        self.listWidget.setDefaultDropAction(Qt.MoveAction)
        self.listWidget.setSelectionBehavior(QListWidget.SelectRows)
        self.listWidget.setEditTriggers(QListWidget.DoubleClicked | QListWidget.EditKeyPressed)
        self.listWidget.setItemDelegate(_ItemEditorDelegate(self.listWidget))
        self.listWidget.itemDoubleClicked.connect(self._start_edit)
        self.listWidget.itemChanged.connect(lambda _item: self._check_duplicates())
        for m in models:
            self.listWidget.addItem(self._make_item(m))
        layout.addWidget(self.listWidget)

        # 被过滤模型展示区（set_filtered_models 非空时显示）
        self.filteredWidget = QWidget()
        filtered_layout = QVBoxLayout(self.filteredWidget)
        filtered_layout.setContentsMargins(0, 0, 0, 0)
        filtered_layout.setSpacing(4)
        self.filtered_title = QLabel("")
        filtered_layout.addWidget(self.filtered_title)
        self.filteredList = QListWidget()
        self.filteredList.setMaximumHeight(90)
        self.filteredList.itemClicked.connect(self._restore_filtered)
        filtered_layout.addWidget(self.filteredList)
        self.filteredWidget.setVisible(False)
        layout.addWidget(self.filteredWidget)

        self.listWidget.setFocus()

    # ── 过滤与添加 ─────────────────────────────────────────

    def _apply_filter(self):
        """按搜索框关键词隐藏/显示主列表项（仅视觉过滤，不删数据）"""
        kw = self.searchEdit.text().strip().lower()
        for i in range(self.listWidget.count()):
            item = self.listWidget.item(i)
            item.setHidden(bool(kw) and kw not in item.text().lower())

    def _on_search_return(self):
        """搜索框回车：把框内文本（可多行/逗号分隔）作为新模型批量加入"""
        tokens = _split_model_input(self.searchEdit.text())
        self.searchEdit.clear()
        if not tokens:
            return
        added = self._add_tokens(tokens)
        if added < len(tokens):
            self._notify_skipped(len(tokens) - added)

    def _add_tokens(self, tokens: list) -> int:
        """批量添加模型，跳过与现有重复项；返回实际新增数"""
        existing = set(self.get_models())
        added = 0
        for t in tokens:
            if t in existing:
                continue
            self.listWidget.addItem(self._make_item(t))
            existing.add(t)
            added += 1
        if added:
            self._check_duplicates()
            self._apply_filter()
        return added

    def _notify_skipped(self, count: int):
        """提示被跳过的重复项数量（信息展示在提示行，不弹窗打断）"""
        self.hint_label.setText(
            f"<span style='color:{_DUP_COLOR};'>跳过 {count} 个重复项</span>"
            " · <span style='color:#606060;'>双击编辑 · Enter 新增 · Delete 删除 · 拖拽排序</span>"
        )

    # ── 编辑操作 ───────────────────────────────────────────

    def _start_edit(self, item):
        """双击开始编辑"""
        self.listWidget.editItem(item)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        if key in (Qt.Key_Return, Qt.Key_Enter) and not mods:
            self._add_new()
            return

        if key == Qt.Key_Delete:
            self._delete_selected()
            return

        if key == Qt.Key_V and (mods & Qt.ControlModifier):
            clipboard = self._clipboard_text()
            if clipboard:
                tokens = _split_model_input(clipboard)
                added = self._add_tokens(tokens)
                if added < len(tokens):
                    self._notify_skipped(len(tokens) - added)
                return
            event.ignore()
            return

        super().keyPressEvent(event)

    @staticmethod
    def _clipboard_text() -> str:
        """读剪贴板纯文本；不可用时返回空串"""
        try:
            from PyQt5.QtWidgets import QApplication

            mime = QApplication.clipboard().mimeData()
            return mime.text() if mime.hasText() else ""
        except Exception:
            return ""

    def _add_new(self):
        """添加新项并立即编辑"""
        item = self._make_item("新模型")
        self.listWidget.addItem(item)
        self.listWidget.setCurrentItem(item)
        self.listWidget.editItem(item)

    def _delete_selected(self):
        """删除选中项"""
        row = self.listWidget.currentRow()
        if row >= 0:
            self.listWidget.takeItem(row)
            self._check_duplicates()

    def _check_duplicates(self):
        """重复项标红提示（不阻止操作；写回下拉时仍会自动去重）"""
        counts: dict = {}
        for i in range(self.listWidget.count()):
            t = self.listWidget.item(i).text()
            counts[t] = counts.get(t, 0) + 1
        for i in range(self.listWidget.count()):
            item = self.listWidget.item(i)
            if counts.get(item.text(), 0) > 1:
                item.setForeground(QColor(_DUP_COLOR))
            else:
                item.setData(Qt.ForegroundRole, None)

    # ── 被过滤模型 ─────────────────────────────────────────

    def set_filtered_models(self, models: list):
        """设置「被过滤的非对话模型」展示区（空列表时隐藏）"""
        models = list(models or [])
        self.filteredWidget.setVisible(bool(models))
        self.filtered_title.setText(f"已过滤 {len(models)} 个非对话模型（点击加回）")
        self.filteredList.clear()
        for m in models:
            self.filteredList.addItem(QListWidgetItem(m))

    def get_filtered_models(self) -> list:
        """当前仍在过滤区的模型（加回后的项会从这里移除）"""
        return [self.filteredList.item(i).text() for i in range(self.filteredList.count())]

    def _restore_filtered(self, item):
        """点击过滤项：加回主列表并从过滤区移除"""
        row = self.filteredList.row(item)
        self.filteredList.takeItem(row)
        self._add_tokens([item.text()])
        remaining = self.filteredList.count()
        self.filtered_title.setText(f"已过滤 {remaining} 个非对话模型（点击加回）")
        self.filteredWidget.setVisible(remaining > 0)

    # ── 数据读写 ───────────────────────────────────────────

    def set_models(self, models: list):
        """装载模型列表（清空后填入）"""
        self.listWidget.clear()
        for m in models:
            self.listWidget.addItem(self._make_item(m))
        self._check_duplicates()
        self._apply_filter()

    def get_models(self) -> list:
        return [self.listWidget.item(i).text() for i in range(self.listWidget.count())]

    def closeEvent(self, event):
        """关闭时摘除列表内部拖拽模式，避免析构后 drop 回调触达已释放项。"""
        try:
            self.listWidget.setDragDropMode(QListWidget.NoDragDrop)
        except RuntimeError, AttributeError:
            pass
        super().closeEvent(event)
