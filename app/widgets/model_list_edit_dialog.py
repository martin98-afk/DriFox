# -*- coding: utf-8 -*-
"""
极简模型列表编辑器（嵌入式 Widget）
Enter 新增，Delete 删除，双击编辑，拖拽排序；
顶部输入框可搜索过滤 / 回车快速添加（支持换行与逗号分隔批量粘贴）；
重复项自动标红；可展示「被过滤的非对话模型」并点击加回。

行首星标（★/☆）标记**默认模型**（点击切换），行尾 tooltip 展示模型能力。
候选区（``set_candidate_models``）默认折叠，展开后可点击加回主列表。
"""

from PyQt5.QtCore import QEvent, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, get_unified_scrollbar_style, scale_font_size
from app.utils.utils import get_font_family_css

_DUP_COLOR = "#e05656"  # 重复项前景色

# 星标列宽（行首保留区）：paint 画在这里，editorEvent 也在这里拦截
_STAR_COL_W = 24


class _StarDelegate(QStyledItemDelegate):
    """列表项 delegate：行首画默认模型星标 + 注入编辑器主题样式。

    继承原 `_ItemEditorDelegate` 的编辑器样式逻辑（双击编辑时主题化输入框）。

    ⚠ 点击处理的分寸：星标区（0-24px）内的鼠标按下被 consume（切换默认模型）；
    **其余区域必须放行** —— 否则 QListWidget 收不到按下事件，双击编辑/拖拽全失效。
    """

    # (model_name) — 星标区内点击时发射
    defaultChanged = pyqtSignal(str)

    def __init__(self, parent=None, is_default=None):
        super().__init__(parent)
        # 回调：模型名 → 是否当前默认（由宿主列表维护状态）
        self._is_default = is_default or (lambda name: False)

    def set_default_checker(self, checker):
        self._is_default = checker or (lambda name: False)

    # ── 绘制 ──

    def paint(self, painter: QPainter, option, index):
        # 文本绘制区左边界右移一个星标列宽：super().paint 画的文本/选中态
        # 从 option.rect.left() 起，不偏移的话星标会叠在首字符上（用户实测反馈）。
        # 星标画在腾出的 0-_STAR_COL_W 保留区，与 editorEvent 命中区同坐标系。
        text_option = QStyleOptionViewItem(option)
        text_option.rect = QRect(option.rect)
        text_option.rect.setLeft(option.rect.left() + _STAR_COL_W)
        super().paint(painter, text_option, index)
        name = str(index.data(Qt.DisplayRole) or "")
        if not name:
            return
        painter.save()
        Colors.refresh()
        star = "★" if self._is_default(name) else "☆"
        color = Colors.TEXT_ACCENT if self._is_default(name) else Colors.TEXT_MUTED
        painter.setPen(QColor(color))
        font = painter.font()
        font.setPointSize(max(9, scale_font_size(12)))
        painter.setFont(font)
        rect = QRect(option.rect.left() + 2, option.rect.top(), _STAR_COL_W - 4, option.rect.height())
        painter.drawText(rect, Qt.AlignVCenter | Qt.AlignHCenter, star)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.MouseButtonRelease:
            x = event.pos().x() - option.rect.left()
            if 0 <= x < _STAR_COL_W:
                # 星标区：切换默认模型并吃掉事件（不再触发选中/编辑）
                name = str(index.data(Qt.DisplayRole) or "")
                if name:
                    self.defaultChanged.emit(name)
                return True
        # 文本区及任何其它事件：交回基类（双击编辑 / 拖拽 / 选中均依赖此路径）
        return super().editorEvent(event, model, option, index)

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

    # (model_name) — 默认模型变更（含被动切换，如删除默认行后自动切首项）
    defaultChanged = pyqtSignal(str)

    def __init__(self, models: list | None = None, parent=None, default_model: str = ""):
        super().__init__(parent)
        # 默认模型（行首★标记；保存时写入「模型名称」）
        self._default_model = str(default_model or "")
        # 候选区状态（折叠/展开 + 数据源）
        self._candidate_models: list = []
        self._candidate_expanded = False
        self._candidate_folded_hint = "其他"
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
        """主题/字号变更时刷新样式（由宿主卡片 refresh_style 链调用）。

        ⚠ 防御式取属性：本方法可能在构造链早期被调用（宿主 refresh 链 / 主题广播），
        此时部分子控件尚未创建 → 直接 `self.xxx` 会 AttributeError 崩在构造路径上
        （用户实测：卡片墙选预置服务商进表单即崩）。用 getattr 逐个守卫，缺谁跳谁。
        """
        self.setStyleSheet(self._build_qss())
        search = getattr(self, "searchEdit", None)
        if search is not None:
            search.setPlaceholderText("搜索过滤；输入后回车添加（支持多行/逗号分隔）")
        hint = getattr(self, "hint_label", None)
        if hint is not None:
            hint.setStyleSheet(
                f"background: transparent; border: none; {get_font_family_css()}"
                f" font-size: {scale_font_size(11)}px; padding: 0;"
            )
        title = getattr(self, "candidate_title", None)
        if title is not None:
            title.setStyleSheet(
                f"background: transparent; border: none; {get_font_family_css()}"
                f" font-size: {scale_font_size(11)}px; padding: 0; color: {Colors.TEXT_SECONDARY};"
            )
        # 星标重绘（主题色可能变）
        lw = getattr(self, "listWidget", None)
        if lw is not None:
            try:
                lw.viewport().update()
            except RuntimeError:
                pass

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
        # 星标 delegate（行首★=默认模型，点击切换；文本区事件放行保双击编辑）
        self._star_delegate = _StarDelegate(self.listWidget, self._is_default_model)
        self._star_delegate.defaultChanged.connect(self.setDefaultModel)
        self.listWidget.setItemDelegate(self._star_delegate)
        self.listWidget.itemDoubleClicked.connect(self._start_edit)
        self.listWidget.itemChanged.connect(lambda _item: self._check_duplicates())
        for m in models:
            self.listWidget.addItem(self._make_item(m))
        layout.addWidget(self.listWidget)

        # 候选模型区（默认折叠：只显示标题行，点击展开）
        self.candidateWidget = QWidget()
        candidate_layout = QVBoxLayout(self.candidateWidget)
        candidate_layout.setContentsMargins(0, 0, 0, 0)
        candidate_layout.setSpacing(4)
        self.candidate_title = QLabel("")
        self.candidate_title.setCursor(Qt.PointingHandCursor)
        self.candidate_title.mousePressEvent = self._toggle_candidates
        candidate_layout.addWidget(self.candidate_title)
        self.candidateList = QListWidget()
        self.candidateList.setMaximumHeight(90)
        self.candidateList.itemClicked.connect(self._restore_candidate)
        candidate_layout.addWidget(self.candidateList)
        self.candidateWidget.setVisible(False)
        layout.addWidget(self.candidateWidget)

        # 属性别名：候选区的前身是「被过滤模型」区，旧名仍有调用方/测试引用
        self.filteredWidget = self.candidateWidget
        self.filteredList = self.candidateList
        self.filtered_title = self.candidate_title

        self.listWidget.setFocus()

    # ── 默认模型（星标）────────────────────────────────────

    def _is_default_model(self, name: str) -> bool:
        """给 delegate 的查询回调：该模型是否当前默认"""
        return bool(self._default_model) and str(name) == self._default_model

    def setDefaultModel(self, model_name: str):
        """设置默认模型（星标切换；不校验存在性，由调用方保证）"""
        self._default_model = str(model_name or "")
        self.listWidget.viewport().update()  # 重绘星标
        self.defaultChanged.emit(self._default_model)

    def getDefaultModel(self) -> str:
        return self._default_model

    def _ensure_default_after_change(self):
        """主列表变化后校正默认：不在列表里 → 取首项；列表空 → 空串"""
        models = self.get_models()
        if not models:
            if self._default_model:
                self.setDefaultModel("")
            return
        if self._default_model not in models:
            self.setDefaultModel(models[0])

    # ── 过滤与添加 ─────────────────────────────────────────

    def _apply_filter(self):
        """按搜索框关键词隐藏/显示主列表项（仅视觉过滤，不删数据）"""
        kw = self.searchEdit.text().strip().lower()
        for i in range(self.listWidget.count()):
            item = self.listWidget.item(i)
            item.setHidden(bool(kw) and kw not in item.text().lower())

    def _apply_item_tooltip(self, item: QListWidgetItem):
        """行 tooltip：模型能力（复用波6 的纯函数，与选择卡/模型按钮同源）"""
        try:
            from app.core.modelmeta.model_capabilities import get_model_capabilities
            from app.widgets.capability_badges import build_capability_tooltip

            caps = get_model_capabilities(item.text()) or {}
            tip = build_capability_tooltip(caps)
            if tip:
                item.setToolTip(tip)
        except Exception:
            pass

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
            item = self._make_item(t)
            self._apply_item_tooltip(item)
            self.listWidget.addItem(item)
            existing.add(t)
            added += 1
        if added:
            self._check_duplicates()
            self._apply_filter()
            self._ensure_default_after_change()
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
        """删除选中项；删的是默认模型时自动切换到首项并提示"""
        row = self.listWidget.currentRow()
        if row < 0:
            return
        removed_item = self.listWidget.item(row)
        removed_name = removed_item.text() if removed_item is not None else ""
        was_default = bool(removed_name) and self._is_default_model(removed_name)
        self.listWidget.takeItem(row)
        self._check_duplicates()
        if not was_default:
            self._ensure_default_after_change()
            return
        # 默认行被删 → 显式提示新默认（静默切换会让用户困惑）
        self._ensure_default_after_change()
        new_default = self._default_model
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            from PyQt5.QtWidgets import QApplication

            parent = self.window() or QApplication.activeWindow()
            msg = f"默认切换为 {new_default}" if new_default else "列表已清空，默认模型置空"
            InfoBar.warning(
                f"已移除默认模型 {removed_name}",
                msg,
                parent=parent,
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
        except Exception:
            pass

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

    # ── 候选区（默认折叠，点击展开）────────────────────────

    def set_candidate_models(self, models: list, folded_hint: str = "其他"):
        """设置候选模型区（默认**隐藏**，标题「{folded_hint} N 个（点击展开）」）。

        候选 = 未被加入主列表的模型（词典/远程拉取/被过滤项），点展开后可点击加回。
        """
        models = list(models or [])
        self._candidate_models = models
        self._candidate_folded_hint = folded_hint
        self._candidate_expanded = False
        self._render_candidates()
        self.candidateWidget.setVisible(bool(models))

    def _render_candidates(self):
        """按当前展开态渲染候选区（折叠时只留标题行）"""
        n = len(self._candidate_models)
        hint = self._candidate_folded_hint
        if self._candidate_expanded:
            self.candidate_title.setText(f"{hint} {n} 个（点击收起）")
        else:
            self.candidate_title.setText(f"{hint} {n} 个（点击展开）")
        self.candidateList.clear()
        if self._candidate_expanded:
            for m in self._candidate_models:
                self.candidateList.addItem(QListWidgetItem(m))
        self.candidateList.setVisible(self._candidate_expanded)

    def _toggle_candidates(self, _event=None):
        """点击标题行：展开/收起候选区"""
        self._candidate_expanded = not self._candidate_expanded
        self._render_candidates()

    def get_candidate_models(self) -> list:
        """当前仍在候选区的模型（加回后的项会从这里移除）"""
        return list(self._candidate_models)

    def _restore_candidate(self, item):
        """点击候选项：加回主列表并从候选区移除"""
        name = item.text()
        if name in self._candidate_models:
            self._candidate_models.remove(name)
        self._add_tokens([name])
        self._render_candidates()
        if not self._candidate_models:
            self.candidateWidget.setVisible(False)

    def _restore_filtered(self, item):
        """薄别名：旧名 _restore_filtered → _restore_candidate"""
        self._restore_candidate(item)

    # 兼容旧调用点（fetch 链仍按 set_filtered_models / get_filtered_models 调用）
    def set_filtered_models(self, models: list):
        """薄别名：候选区（旧名 set_filtered_models，语义已泛化为「候选模型」）"""
        self.set_candidate_models(models, folded_hint="其他")

    def get_filtered_models(self) -> list:
        """薄别名：候选区（旧名 get_filtered_models）"""
        return self.get_candidate_models()

    def refresh_candidates(self, provider_name: str = "", fetched_models: list | None = None):
        """重建候选区数据源：词典 ∪ 已拉取 ∪ 候选区残留 − 已在主列表。

        - 词典：`get_merged_provider_models()[provider_name]`（插件声明 + models.dev）
        - 已拉取：`fetched_models`（编辑卡最近一次「获取模型列表」的结果）
        - 残留：当前候选区里还没被加回的项（重算时不能丢）

        去重保序：词典 → 已拉取 → 残留。
        """
        in_main = set(self.get_models())
        ordered: list = []
        seen: set = set()

        def _push(name: str):
            name = str(name or "").strip()
            if not name or name in in_main or name in seen:
                return
            seen.add(name)
            ordered.append(name)

        try:
            from app.constants import get_merged_provider_models

            merged = get_merged_provider_models() or {}
            if provider_name and provider_name in merged:
                for m in merged[provider_name]:
                    _push(m)
        except Exception:
            pass
        for m in fetched_models or []:
            _push(m)
        for m in self._candidate_models:
            _push(m)

        self.set_candidate_models(ordered, folded_hint="其他")

    # ── 数据读写 ───────────────────────────────────────────

    def set_models(self, models: list):
        """装载模型列表（清空后填入）"""
        self.listWidget.clear()
        for m in models:
            item = self._make_item(m)
            self._apply_item_tooltip(item)
            self.listWidget.addItem(item)
        self._check_duplicates()
        self._apply_filter()
        self._ensure_default_after_change()
        self.listWidget.viewport().update()

    def get_models(self) -> list:
        return [self.listWidget.item(i).text() for i in range(self.listWidget.count())]

    def closeEvent(self, event):
        """关闭时摘除列表内部拖拽模式，避免析构后 drop 回调触达已释放项。"""
        try:
            self.listWidget.setDragDropMode(QListWidget.NoDragDrop)
        except RuntimeError, AttributeError:
            pass
        super().closeEvent(event)
