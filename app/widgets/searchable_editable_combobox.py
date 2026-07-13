from PyQt5.QtCore import QStringListModel, Qt
from PyQt5.QtWidgets import QCompleter
from qfluentwidgets import EditableComboBox

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

MAX_COMBO_VISIBLE_ITEMS = 15  # 下拉框最大同时显示数量

class SearchableEditableComboBox(EditableComboBox):
    def __init__(self, parent=None, max_visible_items: int = MAX_COMBO_VISIBLE_ITEMS):
        super().__init__(parent)
        self._max_visible_items = max_visible_items

        # 1. 使用私有变量名 _search_completer，避免覆盖基类的 completer() 方法
        self._search_completer = QCompleter(self)

        # 设置匹配模式为：包含匹配
        self._search_completer.setFilterMode(Qt.MatchContains)
        # 设置补全模式：弹出列表
        self._search_completer.setCompletionMode(QCompleter.PopupCompletion)
        self._search_completer.setCaseSensitivity(Qt.CaseInsensitive)

        # 2. 使用标准的 setCompleter 方法注册
        self.setCompleter(self._search_completer)

        # 3. 应用主题感知样式（含 QLineEdit、下拉列表、补全弹窗）
        self._apply_theme_style()

        # 内部维护一个纯文本列表用于同步
        self._item_texts = []

        # 限制下拉列表同时显示的最大项数
        try:
            self.view().setMaxVisibleItems(self._max_visible_items)
        except Exception:
            pass

    def _combo_lineedit_style(self) -> str:
        """主题感知的 QLineEdit 内嵌输入框样式"""
        Colors.refresh()
        return f"""
            QLineEdit {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                selection-background-color: {Colors.TEXT_ACCENT};
                {font_size_css(12)}
                {get_font_family_css()}
            }}
            QLineEdit:focus {{
                border-color: {Colors.INPUT_FOCUS_BORDER};
            }}
            QLineEdit::placeholder {{
                color: {Colors.INPUT_PLACEHOLDER};
            }}
        """

    def _dropdown_style(self) -> str:
        """主题感知的下拉列表样式"""
        Colors.refresh()
        return f"""
            QListWidget#comboListWidget {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px;
                outline: none;
            }}
            QListWidget#comboListWidget::item {{
                padding: 6px 14px 6px 12px;
                min-height: 36px;
                border-radius: 3px;
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget#comboListWidget::item:hover {{
                background-color: {Colors.HOVER_BG};
            }}
            QListWidget#comboListWidget::item:selected {{
                background-color: {Colors.TEXT_ACCENT};
                color: {Colors.BUTTON_TEXT_ON_ACCENT};
            }}
        """

    def _completer_popup_style(self) -> str:
        """主题感知的补全弹窗样式"""
        Colors.refresh()
        return f"""
            QAbstractItemView {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 4px;
                outline: none;
            }}
            QAbstractItemView::item {{
                padding: 6px 14px 6px 12px;
                min-height: 36px;
                border-radius: 3px;
                color: {Colors.TEXT_PRIMARY};
            }}
            QAbstractItemView::item:hover {{
                background-color: {Colors.HOVER_BG};
            }}
            QAbstractItemView::item:selected {{
                background-color: {Colors.TEXT_ACCENT};
                color: {Colors.BUTTON_TEXT_ON_ACCENT};
            }}
        """

    def _apply_theme_style(self):
        """应用主题感知样式（QLineEdit + 下拉 + 补全弹窗）"""
        self.setStyleSheet(self._combo_lineedit_style())
        self._style_completer_popup()
        # 刷新已存在的下拉菜单样式
        try:
            if hasattr(self, "_combo_menu") and self._combo_menu:
                self._combo_menu.view.setStyleSheet(self._dropdown_style())
        except Exception:
            pass

    def refresh_style(self):
        """主题变更时刷新所有子控件样式"""
        self._apply_theme_style()

    def _style_completer_popup(self):
        """给补全弹出列表设置主题感知样式"""
        try:
            popup = self._search_completer.popup()
            popup.setStyleSheet(self._completer_popup_style())
        except Exception:
            pass

    def addItem(self, text: str, icon = None, userData=None):
        """重写单条添加"""
        super().addItem(text, icon, userData)
        # 去重处理（可选）
        if text not in self._item_texts:
            self._item_texts.append(text)
            self._update_completer_model()
        # 刷新最大显示项数
        self._apply_max_visible()

    def addItems(self, texts):
        """重写批量添加"""
        super().addItems(texts)
        # 这里的 texts 应该是从 Scanner 获取的所有类型列表
        self._item_texts = list(set(self._item_texts + list(texts)))
        self._update_completer_model()
        self._apply_max_visible()

    def _apply_max_visible(self):
        """应用最大显示项数限制"""
        try:
            self.view().setMaxVisibleItems(self._max_visible_items)
        except Exception:
            pass

    def _update_completer_model(self):
        """更新补全器的数据源"""
        model = QStringListModel(self._item_texts, self._search_completer)
        self._search_completer.setModel(model)

    def clear(self):
        """重写清空方法"""
        # 注意：qfluentwidgets 的 EditableComboBox.clear()
        # 内部可能只清空了菜单，我们也需要清空 LineEdit 内容和补全器
        super().clear()
        self._item_texts = []
        self._update_completer_model()
        self.setText("")

    def get_all_models(self):
        """获取当前模型列表中的所有模型名称"""
        models = []
        for i in range(self.count()):
            text = self.itemText(i)
            if text:
                models.append(text)
        return models

    def removeItemByText(self, text: str) -> bool:
        """按文本移除项"""
        idx = self.findText(text)
        if idx >= 0:
            self.removeItem(idx)
            return True
        return False

    def renameItem(self, old_text: str, new_text: str):
        """重命名项"""
        idx = self.findText(old_text)
        if idx >= 0:
            self.setItemText(idx, new_text)
            # 更新补全器
            if old_text in self._item_texts:
                idx_list = self._item_texts.index(old_text)
                self._item_texts[idx_list] = new_text
                self._update_completer_model()

    # ── 下拉菜单 ───────────────────────────────────────────

    def _createComboMenu(self):
        """重写：创建下拉菜单时注入主题感知样式"""
        menu = super()._createComboMenu()
        try:
            menu.view.setStyleSheet(self._dropdown_style())
        except Exception:
            pass
        return menu
