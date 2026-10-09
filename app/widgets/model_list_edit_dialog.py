# -*- coding: utf-8 -*-
"""
模型列表编辑器（OpenCode 风格清单，P1-9）

行内启停开关：每行 = 双行文本（模型名 + 灰字能力摘要）+ 能力徽章 + SwitchButton。
- 「模型列表」管存在性（get_models() 返回全部）；
- 「模型关闭列表」管可见性（get_disabled_models()；关闭的模型不在选择器显示，
  但仍在列表里，随时可开回来）。
- 保留：搜索过滤 / Enter 新增 / Ctrl+V 批量粘贴 / Delete 删除（删除 = 移出主
  列表回到候选区，可找回）。
- 裁剪（OpenCode 无此二者，且与 setItemWidget 技术互斥）：拖拽排序、双击行内编辑。
- 「设默认模型」语义退役：「模型名称」由保存链自动维护（见 provider_save_plan）。
"""

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import PushButton, SwitchButton

from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style, scale_font_size
from app.utils.utils import get_font_family_css
from app.widgets.capability_badges import ModelCapabilityBadges
from app.widgets.flow_layout import FlowLayout  # noqa: F401  （候选区/外部沿用导入路径）

_DUP_COLOR = "#e05656"  # 重复项前景色

_ROW_H = 46  # 行高：双行文本（名称 + 灰字摘要）+ 开关 + 底部分隔线
# 表格列宽（行控件与表头共用）：价格/开关列固定；徽章列随字号缩放（_badges_col_width）
_COL_PRICE_W = 76  # 单个价格列（右对齐）
_COL_SWITCH_W = 68  # 开关列
_PRICE_NA = "-"  # models.dev 无价格占位（对齐 opencode 的空价列）


def _fmt_price(v) -> str:
    """价格显示：$/M tokens 两位小数；models.dev 无数据返回占位符"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return _PRICE_NA
    return f"${v:.2f}"


def _badges_col_width() -> int:
    """能力徽章列宽：随系统字号缩放（三个文字 chip 最宽组合）。

    固定基准值在高字号档位下会截断 chip（用户实测「开关思考」被裁）。
    估宽：11 个字 ×(11+delta) + 三 chip padding ≈36 + 间距/边距 ≈20，再留余量。
    """
    delta = scale_font_size(11) - 11
    return 11 * (11 + delta) + 56



def _accent_rgba(alpha_pct: int) -> str:
    """accent 色 → rgba() 串（QSS 不支持 #RRGGBBAA，透明度按百分比）"""
    color = QColor(Colors.SYSTEM_ACCENT)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha_pct / 100:.2f})"


class _RowSwitch(SwitchButton):
    """无文字间隔开关：qfw 的 SWITCH_BUTTON.qss 带 ``qproperty-spacing: 12``，
    polish / 主题刷新时 Qt 在 C++ 元对象层直写 spacing（setSpacing/property
    都拦不住），空文本 label 会被推开，开关右侧留 12px 悬空空白（用户实测）。
    治法：直接隐藏空 label——hBox 只剩 indicator 一个元素，spacing 无作用
    对象，qss 写多少都无视觉影响。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOnText("")
        self.setOffText("")
        self.label.hide()
        # 定宽 = indicator 实宽（2 + 42）：无文字后不再吃 adjustSize 的动态宽度，
        # 右对齐放置时指示器始终贴列右缘
        self.setFixedWidth(44)
        # 触发一次 _updateText → setText("")+adjustSize：同步布局状态
        self._updateText()


class _ModelRowWidget(QWidget):
    """单个模型行（setItemWidget 真控件）：双行文本 + 能力徽章 + 启停开关。"""

    def __init__(
        self,
        name: str,
        caps: dict,
        enabled: bool,
        on_toggle,  # Callable[[str, bool], None]
        parent=None,
    ):
        super().__init__(parent)
        self.model_name = str(name)
        self._on_toggle = on_toggle
        self._duplicate = False
        caps = caps or {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        row = QHBoxLayout()
        row.setContentsMargins(12, 4, 8, 4)
        row.setSpacing(0)

        # 名称列：模型名 + 灰字上下文（双行；第二行空则隐藏）
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        self.name_label = QLabel(self.model_name, self)
        self.sub_label = QLabel(self._summary_text(caps), self)
        text_col.addWidget(self.name_label)
        if self.sub_label.text():
            text_col.addWidget(self.sub_label)
        else:
            self.sub_label.setVisible(False)
        row.addLayout(text_col, 1)

        # 能力徽章列：固定宽（保证价格/开关列跨行严格对齐，无徽章也占位）；
        # 宽度随系统字号缩放，字号变更时 refresh_style 同步；
        # chip 自带 tooltip 在本列表一律清除（用户要求：此列表不要悬浮提示）
        self.badges = ModelCapabilityBadges(caps, parent=self)
        for attr in ("think_label", "effort_label", "vision_label"):
            chip = getattr(self.badges, attr, None)
            if chip is not None:
                chip.setToolTip("")
        self.badge_holder = QWidget(self)
        self.badge_holder.setFixedWidth(_badges_col_width())
        badge_lay = QHBoxLayout(self.badge_holder)
        badge_lay.setContentsMargins(0, 0, 8, 0)
        if self.badges.has_any_badge():
            badge_lay.addWidget(self.badges, 0, Qt.AlignLeft | Qt.AlignVCenter)
        row.addWidget(self.badge_holder, 0, Qt.AlignVCenter)

        # 价格四列（caps["cost"] 来自 models.dev，$/M tokens；无数据显示 "-"；
        # 不设独立 tooltip：行 tooltip 已含能力细节，多重悬浮反而乱）
        cost = caps.get("cost") if isinstance(caps.get("cost"), dict) else {}
        self.price_labels: list = []
        for key in ("input", "output", "cache_read", "cache_write"):
            lbl = QLabel(_fmt_price(cost.get(key)), self)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl.setFixedWidth(_COL_PRICE_W)
            self.price_labels.append(lbl)
            row.addWidget(lbl)

        # 开关列：开关旁不显 On/Off 文字（QLabel 恒占宽且与双行文本行打架；
        # 启用态已由行淡化表达）；_RowSwitch 隐藏空 label 消除右侧悬空空白
        self.switch = _RowSwitch(self)
        self.switch.setChecked(enabled)
        self.switch.checkedChanged.connect(self._emit_toggle)
        switch_holder = QWidget(self)
        switch_holder.setFixedWidth(_COL_SWITCH_W)
        switch_lay = QHBoxLayout(switch_holder)
        switch_lay.setContentsMargins(0, 0, 0, 0)
        switch_lay.addWidget(self.switch, 0, Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(switch_holder, 0, Qt.AlignVCenter)

        outer.addLayout(row)

        # 行底分隔线（表格行界线，opencode 风格）
        self._line = QFrame(self)
        self._line.setFixedHeight(1)
        outer.addWidget(self._line)

        self._apply_text_style()

    @staticmethod
    def _summary_text(caps: dict) -> str:
        """灰字摘要：只留上下文长度。能力短语（开关思考/思考强度档位等）与右侧
        徽章重复且长文本挤压行内布局，细节收进行 tooltip（用户反馈列表「混乱」）"""
        caps = caps or {}
        ctx = caps.get("context_limit")
        if isinstance(ctx, int) and ctx > 0:
            return f"上下文 {ctx // 1000}K" if ctx >= 1000 else f"上下文 {ctx}"
        return ""

    def _emit_toggle(self, checked):
        if callable(self._on_toggle):
            self._on_toggle(self.model_name, bool(checked))

    def set_enabled_state(self, enabled: bool, animate: bool = False):
        """外部同步开关态（不触发回调；供 set_disabled_models 批量刷新）"""
        self.switch.blockSignals(True)
        self.switch.setChecked(enabled)
        self.switch.blockSignals(False)
        self._apply_text_style()

    def set_duplicate(self, duplicate: bool):
        """重复项标红（不阻止操作；保存链仍会去重）"""
        self._duplicate = bool(duplicate)
        self._apply_text_style()

    def _apply_text_style(self):
        Colors.refresh()
        enabled = self.switch.isChecked()
        muted = self._duplicate
        name_color = _DUP_COLOR if muted else (Colors.TEXT_MUTED if not enabled else Colors.TEXT_PRIMARY)
        self.name_label.setStyleSheet(
            f"color: {name_color}; font-weight: 600; {get_font_family_css()} "
            f"{font_size_css(14)}; background: transparent; border: none;"
        )
        self.sub_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}; "
            f"background: transparent; border: none;"
        )
        # 价格列：启用态正文色、关闭/重复态灰化（opencode 关闭模型整行变灰的等价表达）
        price_color = _DUP_COLOR if muted else (Colors.TEXT_MUTED if not enabled else Colors.TEXT_SECONDARY)
        price_style = (
            f"color: {price_color}; {get_font_family_css()} {font_size_css(12)}; "
            f"background: transparent; border: none;"
        )
        for lbl in self.price_labels:
            lbl.setStyleSheet(price_style)
        self._line.setStyleSheet(f"background-color: {Colors.BORDER}; border: none;")

    def refresh_style(self):
        self._apply_text_style()
        # 字号档位变更 → 徽章列宽同步（防高字号下 chip 截断）
        try:
            self.badge_holder.setFixedWidth(_badges_col_width())
        except RuntimeError:
            pass


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
    """模型列表编辑器（OpenCode 风格清单）— 可内嵌到表单卡片中。

    交互：Enter 新增 / Delete 删除（回候选区可找回）/ Ctrl+V 批量粘贴 /
    搜索过滤 / 行内开关启停。无拖拽、无双击编辑（setItemWidget 互斥 + OpenCode
    形态裁剪）；「设默认」语义退役，「模型名称」由保存链自动维护。
    """

    def __init__(self, models: list | None = None, parent=None, default_model: str = ""):
        super().__init__(parent)
        # default_model 参数保留接收但已废弃（星标语义退役，P1-9）：调用方
        # （provider_edit_card）构造点未同步清理，收下不用的参数避免破签名。
        # 候选区状态（折叠/展开 + 数据源）
        self._disabled: set = set()
        self._caps_cache: dict = {}
        self._candidate_models: list = []
        self._candidate_expanded = False
        self._candidate_folded_hint = "其他"
        self._init_ui(self._dedupe(models or []))
        self.refresh_style()

    @staticmethod
    def _dedupe(models) -> list:
        """去重保序（P0：磁盘「模型列表」可能积累重复，任何写主列表的入口
        都过这里——重复行会触发重复名场景的一切边界问题）"""
        seen: set = set()
        out: list = []
        for m in models or []:
            name = str(m or "").strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def _build_header(self) -> QWidget:
        """表头行：与行控件共用列宽常量（改列宽两处同步）；样式走 objectName QSS"""
        header = QWidget(self)
        lay = QHBoxLayout(header)
        # 左右比行控件多 1px：QListWidget QSS 边框把 viewport 内容整体推移 1px，
        # 表头直接在 layout 里不吃这 1px，补偿后列线才对齐
        lay.setContentsMargins(13, 6, 9, 6)
        lay.setSpacing(0)
        lay.addWidget(QLabel("模型", header), 1)
        self.header_feats_label = QLabel("能力", header)
        self.header_feats_label.setFixedWidth(_badges_col_width())
        lay.addWidget(self.header_feats_label)
        for cn in ("输入", "输出", "缓存读", "缓存写"):
            lbl = QLabel(cn, header)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl.setFixedWidth(_COL_PRICE_W)
            lay.addWidget(lbl)
        en = QLabel("启用", header)
        en.setAlignment(Qt.AlignCenter)
        en.setFixedWidth(_COL_SWITCH_W)
        lay.addWidget(en)
        return header

    def _build_qss(self) -> str:
        """构建主题 QSS（refresh_style 时重建，保证颜色/字号随系统）"""
        Colors.refresh()
        # 选中/hover 用 accent 低透明度浅色（用户反馈：原实底色太深）。
        # 三档递进：hover 6% < 选中 10% < 选中+hover 14%，文字一律 TEXT_PRIMARY。
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
                padding: 0px;
                border-radius: 3px;
            }}
            QListWidget::item:hover {{
                background-color: {_accent_rgba(6)};
            }}
            QListWidget::item:selected {{
                background-color: {_accent_rgba(10)};
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item:selected:hover {{
                background-color: {_accent_rgba(14)};
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
            QLabel#editorHint {{
                color: {Colors.TEXT_MUTED};
                {font_size_css(10)} {get_font_family_css()}
                background: transparent; border: none; padding: 0;
            }}
            QLabel#candidateLink {{
                color: {Colors.TEXT_MUTED};
                {font_size_css(11)} {get_font_family_css()}
                background: transparent; border: none; padding: 0;
                text-decoration: underline;
            }}
            QWidget#modelTableHeader {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {Colors.BORDER};
            }}
            QWidget#modelTableHeader QLabel {{
                color: {Colors.TEXT_MUTED};
                {font_size_css(11)} {get_font_family_css()}
                background: transparent; border: none; font-weight: 600;
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
            search.setPlaceholderText("搜索过滤；输入后回车或点「添加」新增（支持多行/逗号分隔）")
        title = getattr(self, "candidate_title", None)
        if title is not None:
            title.setStyleSheet("")  # 样式走 QSS objectName 选择器
        # 字号档位变更 → 表头徽章列宽同步（行内列宽由各行 refresh_style 自行更新）
        feats = getattr(self, "header_feats_label", None)
        if feats is not None:
            try:
                feats.setFixedWidth(_badges_col_width())
            except RuntimeError:
                pass
        lw = getattr(self, "listWidget", None)
        if lw is not None:
            for i in range(lw.count()):
                row = lw.itemWidget(lw.item(i))
                if row is not None and hasattr(row, "refresh_style"):
                    try:
                        row.refresh_style()
                    except RuntimeError:
                        pass

    def _caps_for(self, name: str) -> dict:
        """查模型能力（refresh 时批量查一次缓存复用；几十行规模可接受）"""
        if name not in self._caps_cache:
            try:
                from app.core.modelmeta.model_capabilities import get_model_capabilities

                self._caps_cache[name] = get_model_capabilities(name) or {}
            except Exception:
                self._caps_cache[name] = {}
        return self._caps_cache[name]

    def _make_row_item(self, name: str, insert_at: int = -1) -> QListWidgetItem:
        """创建一行：模型名存 Qt.UserRole（item 本体**零绘制文本**，防与行容器
        双绘叠印——P0 用户实测）；行控件负责全部显示；insert_at>=0 时头插"""
        item = QListWidgetItem()
        item.setData(Qt.UserRole, str(name))
        item.setSizeHint(QSize(0, _ROW_H))
        row = _ModelRowWidget(
            str(name),
            self._caps_for(name),
            enabled=name not in self._disabled,
            on_toggle=self._on_row_toggle,
            parent=self.listWidget,
        )
        # 行容器横向 Expanding：跟随 viewport 拉满（防窄窗口下挤压叠印）
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        if insert_at < 0:
            self.listWidget.addItem(item)
        else:
            self.listWidget.insertItem(insert_at, item)
        self.listWidget.setItemWidget(item, row)
        return item

    def _row_of(self, name: str):
        """按模型名即时反查行控件（不建 name→widget 索引：重复名场景会互相覆盖）"""
        for i in range(self.listWidget.count()):
            item = self.listWidget.item(i)
            if item.data(Qt.UserRole) == name:
                return self.listWidget.itemWidget(item)
        return None

    def _init_ui(self, models: list):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 搜索/快速添加行：显式「添加」按钮给新建一个肉眼可见的入口——只靠
        # placeholder 里「回车添加」用户找不到新建路径，且中文输入法首次回车
        # 会被候选词吞掉，按钮彻底绕开这两个坑（用户反馈「无法手动新建」）
        self.searchEdit = QLineEdit()
        self.searchEdit.setClearButtonEnabled(True)
        self.searchEdit.textChanged.connect(lambda _t: self._apply_filter())
        self.searchEdit.returnPressed.connect(self._on_search_return)
        self.addBtn = PushButton("添加")
        self.addBtn.clicked.connect(self._on_search_return)
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        search_row.addWidget(self.searchEdit, 1)
        search_row.addWidget(self.addBtn)
        layout.addLayout(search_row)

        # 表头（opencode 表格风；列宽常量与行控件共享 → 跨行严格对齐）
        self.headerWidget = self._build_header()
        layout.addWidget(self.headerWidget)

        # 候选模型区（默认折叠：只显示标题行，点击展开；右侧「全部加回」一键批量）
        # 布局在主列表**上方**：获取模型列表后自动展开，新模型直接出现在眼前
        self.candidateWidget = QWidget()
        candidate_layout = QVBoxLayout(self.candidateWidget)
        candidate_layout.setContentsMargins(0, 0, 0, 0)
        candidate_layout.setSpacing(4)
        # 标题行：左侧「其他 N 个（点击展开）」+ 右侧「全部加回」（拉取几十个模型后逐个点太累）
        candidate_header = QHBoxLayout()
        candidate_header.setContentsMargins(0, 0, 0, 0)
        self.candidate_title = QLabel("")
        self.candidate_title.setObjectName("candidateLink")
        self.candidate_title.setCursor(Qt.PointingHandCursor)
        self.candidate_title.mousePressEvent = self._toggle_candidates
        candidate_header.addWidget(self.candidate_title)
        candidate_header.addStretch(1)
        self.candidate_add_all = QLabel("全部加回")
        self.candidate_add_all.setObjectName("candidateLink")
        self.candidate_add_all.setCursor(Qt.PointingHandCursor)
        self.candidate_add_all.mousePressEvent = self._add_all_candidates
        candidate_header.addWidget(self.candidate_add_all)
        candidate_layout.addLayout(candidate_header)
        self.candidateList = QListWidget()
        # 高度随候选数自适应全部展开，不内滚（点行即加回主列表）
        self.candidateList.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.candidateList.itemClicked.connect(self._restore_candidate)
        candidate_layout.addWidget(self.candidateList)
        self.candidateWidget.setVisible(False)
        layout.addWidget(self.candidateWidget)

        # 主列表（行内开关启停；无拖拽、无双击编辑；高度全展开不内滚，滚动交给外层卡片）
        self.listWidget = QListWidget()
        self.listWidget.setSelectionBehavior(QListWidget.SelectRows)
        self.listWidget.setSelectionMode(QListWidget.SingleSelection)
        self.listWidget.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.listWidget.itemChanged.connect(lambda _item: self._check_duplicates())
        for m in models:
            self._make_row_item(str(m))
        self._update_list_height()
        layout.addWidget(self.listWidget)

        # 属性别名：候选区的前身是「被过滤模型」区，旧名仍有调用方/测试引用
        self.filteredWidget = self.candidateWidget
        self.filteredList = self.candidateList
        self.filtered_title = self.candidate_title

        self.listWidget.setFocus()

    # ── 行内启停（「模型关闭列表」）────────────────────────

    def _on_row_toggle(self, name: str, checked: bool):
        """开关切换：更新关闭集 + 行淡化（不重建整列表）"""
        if checked:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)
        row = self._row_of(name)
        if row is not None:
            row._apply_text_style()

    def set_disabled_models(self, models: list):
        """装载「模型关闭列表」（磁盘值；装载后同步各行开关态）"""
        self._disabled = {str(m) for m in (models or [])}
        for i in range(self.listWidget.count()):
            row = self.listWidget.itemWidget(self.listWidget.item(i))
            if row is not None and hasattr(row, "set_enabled_state"):
                row.set_enabled_state(row.model_name not in self._disabled)

    def get_disabled_models(self) -> list:
        """关闭的模型（按主列表序返回；恒为主列表子集）"""
        return [m for m in self.get_models() if m in self._disabled]

    # ── 过滤与添加 ─────────────────────────────────────────

    def _apply_filter(self):
        """按搜索框关键词隐藏/显示主列表项（仅视觉过滤，不删数据）；隐藏后重算高度"""
        kw = self.searchEdit.text().strip().lower()
        for i in range(self.listWidget.count()):
            name = str(self.listWidget.item(i).data(Qt.UserRole) or "")
            self.listWidget.item(i).setHidden(bool(kw) and kw not in name.lower())
        self._update_list_height()

    def _on_search_return(self):
        """搜索框回车：把框内文本（可多行/逗号分隔）作为新模型批量加入"""
        tokens = _split_model_input(self.searchEdit.text())
        self.searchEdit.clear()
        if not tokens:
            return
        self._add_tokens(tokens)

    def _add_tokens(self, tokens: list) -> int:
        """批量添加模型到列表**头部**（新模型置顶，用户要求），跳过重复项。

        成功新增时滚动到顶部并选中首行（新增直接可见）。
        """
        existing = set(self.get_models())
        added = 0
        # 逆序遍历头插：最终顺序与输入一致（首个 token 在最上）
        for t in reversed(tokens):
            if t in existing:
                continue
            self._make_row_item(t, insert_at=0)
            existing.add(t)
            added += 1
        if added:
            self._check_duplicates()
            self._apply_filter()
            self.listWidget.scrollToTop()
            self.listWidget.setCurrentRow(0)
        return added

    def add_models(self, models: list) -> int:
        """公开入口：批量并入模型到列表头部（拉取结果直接进表，不走候选区）"""
        clean = [str(m or "").strip() for m in (models or [])]
        return self._add_tokens([m for m in clean if m])

    def _update_list_height(self):
        """主列表高度随可见行数全部展开，不内滚（滚动交给外层编辑卡）；表头随行数显隐"""
        visible = sum(
            1 for i in range(self.listWidget.count()) if not self.listWidget.item(i).isHidden()
        )
        self.listWidget.setFixedHeight(max(visible, 1) * _ROW_H + 8)
        header = getattr(self, "headerWidget", None)
        if header is not None:
            header.setVisible(self.listWidget.count() > 0)

    def _notify_skipped(self, count: int):
        """提示被跳过的重复项数量（信息展示在提示行，不弹窗打断）"""
        self.hint_label.setText(
            f"<span style='color:{_DUP_COLOR};'>跳过 {count} 个重复项</span>"
            "  ·  Enter 或点添加 新增 · Delete 删除 · Ctrl+V 粘贴"
        )

    # ── 编辑操作 ───────────────────────────────────────────

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        if key in (Qt.Key_Return, Qt.Key_Enter) and not mods:
            self._on_search_return()
            return

        if key == Qt.Key_Delete:
            self._delete_selected()
            return

        if key == Qt.Key_V and (mods & Qt.ControlModifier):
            clipboard = self._clipboard_text()
            if clipboard:
                tokens = _split_model_input(clipboard)
                self._add_tokens(tokens)
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

    def _delete_selected(self):
        """删除选中行 = 移出「模型列表」（候选区可找回）；关闭态随之清除"""
        row = self.listWidget.currentRow()
        if row < 0:
            return
        item = self.listWidget.item(row)
        name = str(item.data(Qt.UserRole) or "") if item is not None else ""
        self._disabled.discard(name)
        self.listWidget.takeItem(row)
        self._check_duplicates()
        self._update_list_height()

    def _check_duplicates(self):
        """重复项标红提示（不阻止操作；写回下拉时仍会自动去重）"""
        counts: dict = {}
        for i in range(self.listWidget.count()):
            t = str(self.listWidget.item(i).data(Qt.UserRole) or "")
            counts[t] = counts.get(t, 0) + 1
        for i in range(self.listWidget.count()):
            item = self.listWidget.item(i)
            row = self.listWidget.itemWidget(item)
            if row is not None and hasattr(row, "set_duplicate"):
                row.set_duplicate(counts.get(str(item.data(Qt.UserRole) or ""), 0) > 1)

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
                it = QListWidgetItem(m)
                it.setSizeHint(QSize(0, 30))
                self.candidateList.addItem(it)
            # 高度按与 setSizeHint 一致的项高累计（30/项），否则内容不足
            # 设定高度 → 底部大片空白（用户实测截图）
            self.candidateList.setFixedHeight(max(self.candidateList.count(), 1) * 30 + 10)
        self.candidateList.setVisible(self._candidate_expanded)

    def _toggle_candidates(self, _event=None):
        """点击标题行：展开/收起候选区"""
        self._candidate_expanded = not self._candidate_expanded
        self._render_candidates()

    def _add_all_candidates(self, _event=None):
        """候选区一键全部加回主列表（获取模型列表后逐个点击太累，批量入口）"""
        if not self._candidate_models:
            return
        models = list(self._candidate_models)
        self._candidate_models.clear()
        self._add_tokens(models)
        self._render_candidates()
        self.candidateWidget.setVisible(False)

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

    def refresh_candidates(
        self, provider_name: str = "", fetched_models: list | None = None, expand: bool = False
    ):
        """重建候选区数据源：词典 ∪ 已拉取 ∪ 候选区残留 − 已在主列表。

        - 词典：`get_merged_provider_models()[provider_name]`（插件声明 + models.dev）
        - 已拉取：`fetched_models`（编辑卡最近一次「获取模型列表」的结果）
        - 残留：当前候选区里还没被加回的项（重算时不能丢）
        - expand：True 时候选非空则自动展开（获取成功后免得再点一次标题）

        去重保序：词典 → 已拉取 → 残留。关闭的模型不过滤（仍可见可找回）。
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
        # set_candidate_models 会重置折叠态，展开要求在其后补（候选为空时无意义）
        if expand and ordered:
            self._candidate_expanded = True
            self._render_candidates()

    def set_models(self, models: list):
        """装载模型列表（去重保序；清空后填入；关闭集保留交集，行开关按其回显）"""
        self.listWidget.clear()
        for m in self._dedupe(models):
            self._make_row_item(str(m))
        self._check_duplicates()
        self._apply_filter()
        self._update_list_height()

    def get_models(self) -> list:
        return [
            str(self.listWidget.item(i).data(Qt.UserRole) or "")
            for i in range(self.listWidget.count())
        ]
