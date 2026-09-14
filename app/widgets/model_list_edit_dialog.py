# -*- coding: utf-8 -*-
"""模型列表编辑器（内嵌 Widget）。

三段式布局：工具栏（搜索 / 粘贴导入 / 全选 / 全不选）+ 模型列表 + 操作提示。
每行含启用勾选框（取消勾选 = 隐藏，模型仍保留在列表里）、模型名（可双击改别名）、
元数据摘要（上下文 / 能力 / 价格）。

对外接口：set_models / get_result / import_text / set_model_checked /
set_alias / set_search_text / visible_models / remove_model / set_all_checked。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, LineEdit, PushButton

from app.utils.design_tokens import Colors, get_unified_scrollbar_style, scale_font_size
from app.utils.model_list_ops import display_name, merge_fetched, normalize_hidden
from app.utils.utils import get_font_family_css

# 列表最大高度：内容少时自适应矮，超出封顶后内部滚动
_LIST_MAX_HEIGHT = 320

# 模型 id 存在 UserRole+1（UserRole 留给 Qt 内部使用）
_ROLE_MODEL_ID = Qt.UserRole + 1


class ModelListEditorWidget(QWidget):
    """模型列表编辑器 — 可内嵌到表单卡片中，点击按钮切换显隐"""

    def __init__(self, models: list | None = None, parent=None):
        super().__init__(parent)
        self._models: list[str] = []
        self._hidden: list[str] = []
        self._aliases: dict[str, str] = {}
        self._search_text = ""
        self._init_ui()
        self.refresh_style()
        if models:
            self.set_models(models)

    # ── 样式 ──────────────────────────────────────────────

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
        """ + get_unified_scrollbar_style(6)

    def refresh_style(self):
        """主题/字号变更时刷新样式（由宿主卡片 refresh_style 链调用）"""
        self.setStyleSheet(self._build_qss())
        self.hint_label.setStyleSheet(
            f"background: transparent; border: none; {get_font_family_css()}"
            f" font-size: {scale_font_size(11)}px; padding: 0;"
        )

    # ── 构建 ──────────────────────────────────────────────

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)

        toolbar.addWidget(BodyLabel("搜索:"))
        self.searchEdit = LineEdit()
        self.searchEdit.setPlaceholderText("按模型名或别名过滤")
        self.searchEdit.textChanged.connect(self.set_search_text)
        toolbar.addWidget(self.searchEdit, 1)

        self.importBtn = PushButton("粘贴导入")
        self.importBtn.clicked.connect(self._on_import_clicked)
        toolbar.addWidget(self.importBtn)

        self.checkAllBtn = PushButton("全选")
        self.checkAllBtn.clicked.connect(lambda: self.set_all_checked(True))
        toolbar.addWidget(self.checkAllBtn)

        self.uncheckAllBtn = PushButton("全不选")
        self.uncheckAllBtn.clicked.connect(lambda: self.set_all_checked(False))
        toolbar.addWidget(self.uncheckAllBtn)

        layout.addLayout(toolbar)

        # 列表
        self.listWidget = QListWidget()
        self.listWidget.setMaximumHeight(_LIST_MAX_HEIGHT)
        self.listWidget.setDragDropMode(QListWidget.InternalMove)
        self.listWidget.setDefaultDropAction(Qt.MoveAction)
        self.listWidget.setSelectionBehavior(QListWidget.SelectRows)
        # 禁用 Qt 内置编辑：item.text 含元数据摘要，内置编辑器会把整串当作模型名。
        # 改名统一走双击 → QInputDialog（单一写路径，避免两套编辑器打架）。
        self.listWidget.setEditTriggers(QListWidget.NoEditTriggers)
        self.listWidget.itemDoubleClicked.connect(self._start_edit)
        self.listWidget.itemChanged.connect(self._on_item_changed)
        self.listWidget.model().rowsMoved.connect(self._on_rows_moved)
        layout.addWidget(self.listWidget)

        # 提示行
        self.hint_label = QLabel(
            "<span style='color:#808080;'>双击改别名</span> · <span style='color:#606060;'>Enter 新增</span>"
            " · <span style='color:#606060;'>Delete 删除</span> · <span style='color:#606060;'>拖拽排序</span>"
            "<br><span style='color:#606060;'>取消勾选 = 隐藏（保留在列表，可在模型选择里屏蔽）</span>"
        )
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.listWidget.setFocus()

    # ── 数据装载 ──────────────────────────────────────────

    def set_models(self, models: list, hidden: list | None = None, aliases: dict | None = None):
        """装载模型列表（清空后填入）"""
        self._models = [str(m).strip() for m in (models or []) if str(m).strip()]
        self._aliases = {str(k): str(v).strip() for k, v in (aliases or {}).items() if str(v).strip()}
        self._hidden = normalize_hidden(self._models, hidden)
        self._rebuild_list()

    def get_result(self) -> tuple[list, list, dict]:
        """返回 (模型列表, 隐藏列表, 别名映射)"""
        return list(self._models), list(self._hidden), dict(self._aliases)

    def get_models(self) -> list:
        """兼容旧接口：只要模型列表"""
        return list(self._models)

    # ── 列表重建 ──────────────────────────────────────────

    def _rebuild_list(self):
        """按当前数据重建列表项（保留滚动位置）"""
        scroll = self.listWidget.verticalScrollBar().value()
        self.listWidget.blockSignals(True)
        self.listWidget.clear()
        hidden_lower = {h.lower() for h in self._hidden}
        for model_id in self._models:
            if not self._matches_search(model_id):
                continue
            item = QListWidgetItem()
            item.setData(_ROLE_MODEL_ID, model_id)
            # 不置 ItemIsEditable：编辑走 QInputDialog，见 setEditTriggers 注释
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked if model_id.lower() in hidden_lower else Qt.Checked)
            item.setText(self._item_text(model_id))
            self.listWidget.addItem(item)
        self.listWidget.blockSignals(False)
        self.listWidget.verticalScrollBar().setValue(scroll)

    def _item_text(self, model_id: str) -> str:
        """列表项文本：显示名 + 元数据摘要"""
        name = display_name(model_id, self._aliases)
        meta = self._metadata_summary(model_id)
        return f"{name}    {meta}" if meta else name

    def _metadata_summary(self, model_id: str) -> str:
        """元数据摘要：上下文 · 能力 · 价格。缺失的部分跳过。"""
        try:
            from app.core.model_capabilities import get_model_capabilities

            caps = get_model_capabilities(model_id)
        except Exception:
            return ""
        parts = []
        limit = caps.get("context_limit")
        if limit:
            try:
                n = int(limit)
                parts.append(f"{n // 1000}K" if n >= 1000 else str(n))
            except ValueError, TypeError:
                pass
        if caps.get("supports_thinking"):
            parts.append("思考")
        if caps.get("supports_vision"):
            parts.append("多模态")
        cost = caps.get("cost") or {}
        vals = [cost.get("input"), cost.get("output")]
        if any(v is not None for v in vals):
            try:
                from app.widgets.cards.settings.model_selector_card import _format_cost_number

                parts.append("/".join(_format_cost_number(v) if v is not None else "-" for v in vals))
            except Exception:
                pass
        return " · ".join(parts)

    def _matches_search(self, model_id: str) -> bool:
        if not self._search_text:
            return True
        haystack = f"{model_id} {display_name(model_id, self._aliases)}".lower()
        return self._search_text.lower() in haystack

    # ── 公开操作 API（宿主与测试用）────────────────────────

    def set_search_text(self, text: str):
        """设置搜索过滤文本（只影响显示，不改数据）"""
        self._search_text = str(text or "").strip()
        if self.searchEdit.text() != text:
            self.searchEdit.blockSignals(True)
            self.searchEdit.setText(str(text or ""))
            self.searchEdit.blockSignals(False)
        self._rebuild_list()

    def visible_models(self) -> list:
        """当前可见（通过搜索过滤）的模型 id 列表"""
        return [m for m in self._models if self._matches_search(m)]

    def set_model_checked(self, model_id: str, checked: bool):
        """设置某模型的启用状态（False = 进隐藏集）"""
        hidden_lower = {h.lower() for h in self._hidden}
        key = model_id.lower()
        if checked:
            hidden_lower.discard(key)
        else:
            hidden_lower.add(key)
        self._hidden = [m for m in self._models if m.lower() in hidden_lower]
        self._rebuild_list()

    def set_all_checked(self, checked: bool):
        """批量切换勾选状态（只作用于当前可见项）"""
        if checked:
            hidden_lower = {h.lower() for h in self._hidden}
            for m in self.visible_models():
                hidden_lower.discard(m.lower())
            self._hidden = [m for m in self._models if m.lower() in hidden_lower]
        else:
            self._hidden = normalize_hidden(self._models, list(self._hidden) + self.visible_models())
        self._rebuild_list()

    def set_alias(self, model_id: str, alias: str):
        """设置某模型的别名（空串 = 清除）"""
        text = str(alias or "").strip()
        if text:
            self._aliases[model_id] = text
        else:
            self._aliases.pop(model_id, None)
        self._rebuild_list()

    def remove_model(self, model_id: str):
        """从模型列表中彻底移除（区别于隐藏）"""
        self._models = [m for m in self._models if m != model_id]
        self._aliases.pop(model_id, None)
        self._hidden = normalize_hidden(self._models, self._hidden)
        self._rebuild_list()

    def import_text(self, text: str) -> list:
        """批量导入：按行 / 逗号 / 空格切分，增量合并。返回本次新增的模型。"""
        raw = str(text or "")
        for sep in (",", "，", "\t"):
            raw = raw.replace(sep, "\n")
        tokens = [t for line in raw.splitlines() for t in line.split()]
        merged = merge_fetched(self._models, tokens)
        existing_lower = {m.lower() for m in self._models}
        added = [m for m in merged if m.lower() not in existing_lower]
        self._models = merged
        self._rebuild_list()
        return added

    # ── 交互回调 ──────────────────────────────────────────

    def _on_import_clicked(self):
        """弹出多行输入框做批量导入"""
        parent = self.window()
        default_text, ok = QInputDialog.getMultiLineText(
            parent, "粘贴导入模型", "每行一个模型名（也支持逗号或空格分隔）：", ""
        )
        if not ok:
            return
        added = self.import_text(default_text)
        if added:
            from qfluentwidgets import InfoBar, InfoBarPosition

            InfoBar.success(
                "导入完成",
                f"新增 {len(added)} 个模型",
                parent=parent,
                duration=2500,
                position=InfoBarPosition.BOTTOM,
            )

    def _on_item_changed(self, item: QListWidgetItem):
        """勾选状态变化 -> 更新隐藏集"""
        model_id = item.data(_ROLE_MODEL_ID)
        if model_id is None:
            return
        checked = item.checkState() == Qt.Checked
        hidden_lower = {h.lower() for h in self._hidden}
        key = model_id.lower()
        if checked:
            hidden_lower.discard(key)
        else:
            hidden_lower.add(key)
        self._hidden = [m for m in self._models if m.lower() in hidden_lower]

    def _on_rows_moved(self, *_args):
        """拖拽排序后同步底层列表顺序"""
        order = []
        for i in range(self.listWidget.count()):
            model_id = self.listWidget.item(i).data(_ROLE_MODEL_ID)
            if model_id is not None:
                order.append(model_id)
        if not order:
            return
        rank = {m: i for i, m in enumerate(order)}
        # 被搜索过滤掉、不在可见列表里的项保持相对位置，排在可见项之后
        self._models = sorted(self._models, key=lambda m: rank.get(m, len(rank)))

    def _start_edit(self, item: QListWidgetItem):
        """双击弹输入框改别名"""
        model_id = item.data(_ROLE_MODEL_ID)
        if model_id is None:
            return
        parent = self.window()
        current = display_name(model_id, self._aliases)
        text, ok = QInputDialog.getText(
            parent,
            "模型别名",
            f"「{model_id}」的显示名（留空恢复原名）：",
            text=current if current != model_id else "",
        )
        if ok:
            self.set_alias(model_id, text)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        if key in (Qt.Key_Return, Qt.Key_Enter) and not mods:
            self._add_new()
            return

        if key == Qt.Key_Delete:
            self._delete_selected()
            return

        super().keyPressEvent(event)

    def _add_new(self):
        """回车新增：弹输入框要名字，直接入列（不用 Qt 内置编辑态）。"""
        parent = self.window()
        text, ok = QInputDialog.getText(parent, "新增模型", "模型名（API 请求用的真实 id）：")
        if not ok:
            return
        model_id = str(text or "").strip()
        if not model_id:
            return
        if any(m.lower() == model_id.lower() for m in self._models):
            return
        self._models.append(model_id)
        self._rebuild_list()
        target = self._find_item(model_id)
        if target is not None:
            self.listWidget.setCurrentItem(target)

    def _delete_selected(self):
        """删除选中项（彻底移除，非隐藏）"""
        item = self.listWidget.currentItem()
        if item is None:
            return
        model_id = item.data(_ROLE_MODEL_ID)
        if model_id is not None:
            self.remove_model(model_id)

    def _find_item(self, model_id: str) -> QListWidgetItem | None:
        for i in range(self.listWidget.count()):
            item = self.listWidget.item(i)
            if item.data(_ROLE_MODEL_ID) == model_id:
                return item
        return None
