# -*- coding: utf-8 -*-
"""权限审批浮动卡 —— 结构化决策回传（方案乙）

设计要点：
- **与提问卡完全解耦**（不 import question_floating_widget）：提问卡走文本协议
  （`问题「X」的回答：\\n【允许】`），审批卡走结构化信号，两者互不影响。
- 结构化回传：`answered(decision, remember, reason)` —— 消除文本标签反解析，
  避免标签文案改字导致静默 deny。
- 风险三档（danger/warn/info）驱动配色、图标、默认焦点、按钮延迟与记住菜单项。
- 安全默认：默认焦点在「拒绝」；任何异常/中断路径（Esc、关闭、无选择）一律 deny。

职责分离：本卡**不做命令解析**（不 import sandbox）。影响范围/风险档位/来源文案
全部由宿主计算后传入，卡只负责渲染。
"""

from loguru import logger
from PyQt5.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QScrollArea,
    QShortcut,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import Action, RoundMenu

from app.utils.design_tokens import BorderRadius, CardStyles, Colors, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css, get_unified_font
from app.widgets.cards.card_container import CardContainer

# 风险档位
RISK_DANGER = "danger"
RISK_WARN = "warn"
RISK_INFO = "info"

# 记住语义取值（与 PermissionCache 的 round/session 两级对齐）
REMEMBER_NONE = ""
REMEMBER_ROUND = "round"
REMEMBER_SESSION = "session"

# danger 档位「允许」按钮防误触延迟（ms）：连按 Enter 一路放行的现存风险点
_DANGER_ALLOW_DELAY_MS = 500

# 提示行默认文案（防误触拦截时临时改写，用后还原）
# 不含数字键直选：4 按钮卡片上价值有限，且需与视觉顺序强绑定（改布局即错位），
# 用户明确反馈"字体太多太杂乱"，收敛为 4 项核心快捷键。
_HINT_TEXT = "Enter 允许 · Esc 拒绝 · Ctrl+P 预览 · A 记住"


class PermissionApprovalWidget(QWidget):
    """权限审批卡：展示工具调用完整信息，回传结构化决策"""

    # decision("allow"/"deny"), remember(""/"round"/"session"), reason("")
    answered = pyqtSignal(str, str, str)
    cancelled = pyqtSignal()
    previewRequested = pyqtSignal(object)
    heightChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tool_name = ""
        self._arguments: dict = {}
        self._source = ""
        self._source_text = ""
        self._workdir = ""
        self._impact: dict = {}
        self._tool_call_id = ""
        self._risk = RISK_INFO
        self._preview_payload = None
        self._request_time = ""
        # 防误触：danger 档位允许按钮的解锁定时器
        self._allow_unlock_timer: QTimer | None = None
        # 已就当前请求做出决策（防重复 emit：按钮点击 + Esc 竞争）
        self._decided = False
        # danger 防误触拦截提示是否已显示（避免连按时反复改写提示行）
        self._lock_hint_shown = False
        # 高度通知合并标志（对齐提问卡，防布局自激循环）
        self._height_emit_pending = False

        # ① 高度严格跟随内容
        self.setProperty(CardContainer.FOLLOW_CONTENT_PROP, True)
        # ② 跳过容器展开/折叠动画：动画期间容器高度滞后会让内容被压
        self.setProperty(CardContainer.NO_ANIMATION_PROP, True)
        # ③ 自绘表面必备：不设则 QSS 的 background/border/border-radius 全部静默失效
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._setup_ui()
        self._setup_shortcuts()

    # ══════════════════ 公开接口 ══════════════════

    def show_request(
        self,
        tool_name: str,
        arguments: dict,
        source: str,
        source_text: str,
        risk: str,
        workdir: str,
        impact: dict,
        preview_payload=None,
    ) -> None:
        """填充并展示一次审批请求

        Args:
            tool_name: 工具英文名（用于中文名/图标查询与记住菜单文案）
            arguments: 工具原始参数（主参数优先展示，完整不截断）
            source: 来源 key（"sandbox"/"delete"/"policy"）
            source_text: 来源人类可读文案（宿主给出）
            risk: 风险档位（"danger"/"warn"/"info"）
            workdir: 当前工作目录
            impact: {"paths": [...], "missing": [...], "domains": [...], "writes": bool}
            preview_payload: 预览载荷（非空才显示预览按钮）
        """
        self._tool_name = tool_name or ""
        self._arguments = dict(arguments or {})
        self._source = source or ""
        self._source_text = source_text or ""
        self._tool_call_id = (
            str((preview_payload or {}).get("tool_call_id") or "") if isinstance(preview_payload, dict) else ""
        )
        self._risk = risk if risk in (RISK_DANGER, RISK_WARN, RISK_INFO) else RISK_INFO
        self._workdir = workdir or ""
        self._impact = dict(impact or {})
        self._preview_payload = preview_payload
        self._decided = False
        from datetime import datetime

        self._request_time = datetime.now().strftime("%H:%M:%S")

        self._render()
        self._apply_card_style()
        self.updateGeometry()
        self._emit_height_changed()

    def clear(self) -> None:
        """会话切换/流结束清理：复位状态并隐藏"""
        self._cancel_allow_unlock()
        self._tool_name = ""
        self._arguments = {}
        self._preview_payload = None
        self._decided = True  # 清理后不再允许 emit
        self.setVisible(False)

    def set_opacity(self, opacity: float) -> None:
        """设置整体不透明度（对齐宿主 _refresh 约定）"""
        self.setWindowOpacity(opacity)

    def refresh_style(self) -> None:
        """主题/深浅切换时刷新样式（色值必须每次重新取，禁止缓存为模块常量）"""
        self._apply_card_style()

    # ══════════════════ UI 构建 ══════════════════

    def _setup_ui(self) -> None:
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 6, 10, 8)
        main_layout.setSpacing(6)

        main_layout.addWidget(self._build_header())
        main_layout.addWidget(self._build_body())
        main_layout.addWidget(self._build_meta())
        main_layout.addWidget(self._build_hint())
        main_layout.addWidget(self._build_footer())

    def _build_header(self) -> QWidget:
        """① 头部：风险图标 + 风险文案 + 工具中文名(英文) + 来源标签 + 序号"""
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._risk_icon = QLabel("")
        self._risk_icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._risk_icon)

        self._title_label = QLabel("")
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._title_label)

        self._source_label = QLabel("")
        self._source_label.setObjectName("papSource")
        self._source_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._source_label)

        lay.addStretch()

        w.setFixedHeight(24)
        return w

    def _build_body(self) -> QWidget:
        """② 主体：命令/参数块（等宽、可滚动、完整不截断）+ 来源说明 + 影响范围 + 工作目录"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._cmd_view = QScrollArea()
        self._cmd_view.setWidgetResizable(True)
        self._cmd_view.setFrameShape(QScrollArea.NoFrame)
        self._cmd_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._cmd_view.setMaximumHeight(160)
        self._cmd_label = QLabel("")
        self._cmd_label.setObjectName("papCommand")
        self._cmd_label.setWordWrap(True)
        self._cmd_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._cmd_view.setWidget(self._cmd_label)
        lay.addWidget(self._cmd_view)

        self._source_desc = QLabel("")
        self._source_desc.setObjectName("papDesc")
        self._source_desc.setWordWrap(True)
        lay.addWidget(self._source_desc)

        self._impact_label = QLabel("")
        self._impact_label.setObjectName("papImpact")
        self._impact_label.setWordWrap(True)
        lay.addWidget(self._impact_label)

        self._workdir_label = QLabel("")
        self._workdir_label.setObjectName("papHint")
        self._workdir_label.setWordWrap(True)
        lay.addWidget(self._workdir_label)

        return w

    def _build_meta(self) -> QWidget:
        """③ 元信息（折叠，默认收起）：tool_call_id + 请求时间"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._meta_toggle = QPushButton("▸ 详情")
        self._meta_toggle.setObjectName("papMetaToggle")
        self._meta_toggle.setCursor(Qt.PointingHandCursor)
        self._meta_toggle.setFlat(True)
        self._meta_toggle.clicked.connect(self._toggle_meta)
        lay.addWidget(self._meta_toggle)

        self._meta_label = QLabel("")
        self._meta_label.setObjectName("papHint")
        self._meta_label.setWordWrap(True)
        self._meta_label.setVisible(False)
        lay.addWidget(self._meta_label)
        return w

    def _build_hint(self) -> QWidget:
        """④ 提示行：快捷键说明"""
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self._hint_label = QLabel(_HINT_TEXT)
        self._hint_label.setObjectName("papHint")
        self._hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._hint_label)
        lay.addStretch()
        return w

    def _build_footer(self) -> QWidget:
        """⑤ 尾部：左 [预览] [记住 ▼]　右 [拒绝] [允许]（主操作在最右）

        用 addStretch() 分隔两区。用户明确要求"预览和记住放到左边，右侧是拒绝+允许"。
        """
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # ── 左区：辅助操作 ──
        self._preview_btn = QPushButton("预览")
        self._preview_btn.setObjectName("papSecondary")
        self._preview_btn.setCursor(Qt.PointingHandCursor)
        self._preview_btn.setFixedHeight(26)
        self._preview_btn.setVisible(False)
        self._preview_btn.clicked.connect(self._on_preview)
        lay.addWidget(self._preview_btn)

        self._remember_btn = QPushButton("记住 ▾")
        self._remember_btn.setObjectName("papSecondary")
        self._remember_btn.setCursor(Qt.PointingHandCursor)
        self._remember_btn.setFixedHeight(26)
        self._remember_btn.clicked.connect(self._show_remember_menu)
        lay.addWidget(self._remember_btn)

        lay.addStretch()

        # ── 右区：决策操作（主操作「允许」在最右）──
        self._deny_btn = QPushButton("拒绝")
        self._deny_btn.setObjectName("papDeny")
        self._deny_btn.setCursor(Qt.PointingHandCursor)
        self._deny_btn.setFixedHeight(26)
        self._deny_btn.clicked.connect(self._on_deny)
        # 右键 → 拒绝并说明原因（不做卡内嵌入式输入框：输入区显隐是全局共享方法）
        self._deny_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self._deny_btn.customContextMenuRequested.connect(self._on_deny_with_reason)
        lay.addWidget(self._deny_btn)

        self._allow_btn = QPushButton("允许")
        self._allow_btn.setObjectName("papAllow")
        self._allow_btn.setCursor(Qt.PointingHandCursor)
        self._allow_btn.setFixedHeight(26)
        self._allow_btn.clicked.connect(lambda: self._decide("allow", REMEMBER_NONE))
        lay.addWidget(self._allow_btn)

        return w

    # ══════════════════ 渲染 ══════════════════

    def _render(self) -> None:
        """按当前请求数据填充各字段"""
        meta = self._query_tool_meta(self._tool_name)
        cn_name = meta.get("cn_name") or self._tool_name
        title = f"{cn_name}（{self._tool_name}）" if cn_name != self._tool_name else self._tool_name
        self._title_label.setText(title)
        self._title_label.setFont(get_unified_font(12, True))

        icon, risk_text = self._risk_visual()
        self._risk_icon.setText(icon)
        self._source_label.setText(f"{risk_text} · {self._source_text}")

        self._cmd_label.setText(self._format_arguments())
        self._source_desc.setText(self._source_description())
        self._impact_label.setText(self._format_impact())
        self._workdir_label.setText(f"工作目录：{self._workdir}" if self._workdir else "")

        self._meta_label.setText(f"tool_call_id：{self._tool_call_id or '—'}\n请求时间：{self._request_time or '—'}")

        # 预览按钮仅在宿主给了载荷时显示
        self._preview_btn.setVisible(self._preview_payload is not None)

        # danger 档位：会话级记住项禁用 + 允许按钮延迟解锁
        self._deny_btn.setEnabled(True)
        if self._risk == RISK_DANGER:
            self._allow_btn.setEnabled(False)
            self._allow_unlock_timer = QTimer(self)
            self._allow_unlock_timer.setSingleShot(True)
            self._allow_unlock_timer.timeout.connect(self._unlock_allow)
            self._allow_unlock_timer.start(_DANGER_ALLOW_DELAY_MS)
        else:
            self._cancel_allow_unlock()
            self._allow_btn.setEnabled(True)

    @staticmethod
    def _query_tool_meta(tool_name: str) -> dict:
        """查 registry 元数据（失败回退空 dict，不阻断审批渲染）"""
        try:
            from app.tools.registry import ToolRegistry

            return ToolRegistry.get_instance().get_meta(tool_name) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[PermissionCard] registry 元数据查询失败({tool_name}): {e}")
            return {}

    def _risk_visual(self) -> tuple:
        """风险档位 → (图标, 文案)。色值由 _apply_card_style 单独取（每次 refresh）"""
        if self._risk == RISK_DANGER:
            return "⚠️", "危险操作"
        if self._risk == RISK_WARN:
            return "⚡", "需要确认"
        return "●", "权限请求"

    def _risk_color(self) -> str:
        if self._risk == RISK_DANGER:
            return Colors.REALTIME_ERROR
        if self._risk == RISK_WARN:
            return Colors.REALTIME_ACCENT_WARM
        return Colors.REALTIME_BORDER

    def _format_arguments(self) -> str:
        """格式化参数：主参数优先（command → path → file_path），否则逐行 key: value

        禁止 `str(arguments)[:160]` 式截断 dict 字面量（现状最影响可读性的一点）。
        """
        args = self._arguments or {}
        for key in ("command", "path", "file_path"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return val
        if not args:
            return "（无参数）"
        lines = []
        for k, v in args.items():
            if isinstance(v, (list, tuple)):
                rendered = "\n".join(f"  - {item}" for item in v)
                lines.append(f"{k}:\n{rendered}")
            elif isinstance(v, dict):
                lines.append(f"{k}: {v}")
            else:
                lines.append(f"{k}: {v}")
        return "\n".join(lines)

    def _source_description(self) -> str:
        """来源一句话说明"""
        if self._source == "sandbox":
            return "触发了安全中心的沙箱拦截规则（命令/路径/网络边界）。"
        if self._source == "delete":
            return "这是删除类命令，确认后将先快照再执行（可找回）。"
        return "该工具当前策略为「关闭后询问」，需要你确认后才能执行。"

    def _format_impact(self) -> str:
        """影响范围：路径 / 缺失路径 / 域名 / 写操作"""
        parts = []
        impact = self._impact or {}
        paths = impact.get("paths") or []
        if paths:
            shown = "\n".join(f"  • {p}" for p in paths[:10])
            extra = f"\n  • …等共 {len(paths)} 项" if len(paths) > 10 else ""
            parts.append(f"影响路径：\n{shown}{extra}")
        missing = impact.get("missing") or []
        if missing:
            parts.append(f"（另有 {len(missing)} 项路径当前不存在）")
        domains = impact.get("domains") or []
        if domains:
            parts.append("涉及域名：" + "、".join(str(d) for d in domains[:10]))
        if impact.get("writes"):
            parts.append("该操作会写入文件。")
        return "\n".join(parts)

    # ══════════════════ 样式 ══════════════════

    def _apply_card_style(self) -> None:
        """应用主题化样式（色值每次从 Colors 取，支持主题热切换）"""
        Colors.refresh()
        risk_color = self._risk_color()
        self.setStyleSheet(CardStyles.floating("PermissionApprovalWidget", alpha=250, border=risk_color))
        self._risk_icon.setStyleSheet(f"color:{risk_color};background:transparent;{font_size_css(12)}")
        self._title_label.setStyleSheet(f"color:{Colors.REALTIME_TEXT};background:transparent;{get_font_family_css()}")
        self._source_label.setStyleSheet(
            f"QLabel#papSource {{ color:{risk_color}; background:{Colors.REALTIME_TAG_BG};"
            f" border-radius:{BorderRadius.XS}; padding:1px 6px; {font_size_css(10)}"
            f" {get_font_family_css()} }}"
        )
        self._cmd_view.setStyleSheet(
            f"QScrollArea {{ background:{Colors.CONTENT_BG}; border:1px solid {Colors.BORDER};"
            f" border-radius:{BorderRadius.SM}; }}"
        )
        # 全卡字号收敛为三档（用户反馈"字体太多太杂乱"）：
        # 12 = 标题/按钮　11 = 正文·命令·路径·元信息　10 = 徽标·提示行
        # 命令块靠等宽字形本身区分，不再放大（原先 13 > 正文 11 造成辅助信息压过正文）
        self._cmd_label.setStyleSheet(
            f"QLabel#papCommand {{ color:{Colors.REALTIME_TEXT}; background:transparent;"
            f" padding:6px 8px; {get_font_family_css()};"
            f" font-family:'{get_unified_font(11).family()}',Consolas,monospace;"
            f" font-size:{scale_font_size(11)}px; }}"
        )
        self._source_desc.setStyleSheet(
            f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;{font_size_css(11)}"
        )
        self._impact_label.setStyleSheet(f"color:{Colors.REALTIME_TEXT};background:transparent;{font_size_css(11)}")
        self._workdir_label.setStyleSheet(
            f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;{font_size_css(11)}"
        )
        self._meta_label.setStyleSheet(
            f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;{font_size_css(11)}"
        )
        # 提示行 9 → 10（原字号偏小）
        self._hint_label.setStyleSheet(
            f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;{font_size_css(10)}"
        )
        self._meta_toggle.setStyleSheet(
            f"QPushButton#papMetaToggle {{ color:{Colors.TEXT_SECONDARY}; background:transparent;"
            f" border:none; padding:0; text-align:left; {font_size_css(10)} }}"
            f" QPushButton#papMetaToggle:hover {{ color:{Colors.TEXT_PRIMARY}; }}"
        )
        # 拒绝：透明底 + 次要文字；danger 档位改错误色文字
        deny_color = Colors.REALTIME_ERROR if self._risk == RISK_DANGER else Colors.TEXT_SECONDARY
        self._deny_btn.setStyleSheet(
            f"QPushButton#papDeny {{ background:transparent; color:{deny_color}; border:none;"
            f" padding:0 12px; {font_size_css(12)} }}"
            f" QPushButton#papDeny:hover {{ color:{Colors.TEXT_PRIMARY}; }}"
        )
        # 次要按钮：REALTIME_TAG_BG 底 + REALTIME_TEXT 字（禁止硬编码白字）
        for btn in (self._preview_btn, self._remember_btn):
            btn.setStyleSheet(
                f"QPushButton#papSecondary {{ background:{Colors.REALTIME_TAG_BG};"
                f" color:{Colors.REALTIME_TEXT}; border:none; border-radius:{BorderRadius.SM};"
                f" padding:0 12px; {font_size_css(12)} }}"
                f" QPushButton#papSecondary:hover {{ background:{Colors.HOVER_BG}; }}"
            )
        self._allow_btn.setStyleSheet(
            f"QPushButton#papAllow {{ background:{Colors.REALTIME_ACCENT}; color:#ffffff;"
            f" border:none; border-radius:{BorderRadius.SM}; padding:0 16px; font-weight:bold;"
            f" {font_size_css(12)} }}"
            f" QPushButton#papAllow:hover {{ background:{Colors.REALTIME_ACCENT_WARM}; }}"
            f" QPushButton#papAllow:disabled {{ background:{Colors.HOVER_BG}; color:{Colors.TEXT_SECONDARY}; }}"
        )

    # ══════════════════ 交互 ══════════════════

    def _setup_shortcuts(self) -> None:
        """Esc 用 WidgetWithChildrenShortcut：子控件聚焦时也生效"""
        for seq, slot in (
            (Qt.Key_Escape, self._on_deny),
            ("Ctrl+P", self._on_preview),
            ("Ctrl+Return", lambda: self._decide("allow", REMEMBER_NONE)),
            ("Ctrl+Enter", lambda: self._decide("allow", REMEMBER_NONE)),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        if not mods and key in (Qt.Key_Return, Qt.Key_Enter):
            self._decide("allow", REMEMBER_NONE)
            return
        if not mods and key == Qt.Key_A:
            self._show_remember_menu()
            return
        # 数字键直选已移除：新布局改为左[预览·记住] 右[拒绝·允许]，
        # 数字映射若不同步重排会与视觉顺序错位（按 1 得到的 ≠ 视觉第一个），
        # 且 4 按钮卡片上价值有限。保留 Enter/Esc/Ctrl+P/A 四项核心快捷键。
        super().keyPressEvent(event)

    def showEvent(self, event):
        """显示时把焦点给「拒绝」按钮

        **焦点给拒绝** 与 **Enter = 允许** 是两个独立设计，不是矛盾：
        - 焦点给拒绝：用户按**空格**即可直接拒绝（实测 QPushButton 在
          StrongFocus 下 Space 触发 clicked → deny）。这是"默认安全"的
          实际价值所在。
        - Enter 仍映射为允许：按钮式 UI 下 Enter = 主操作的通行惯例
          （经用户确认保留）。因此 Enter 无论焦点在哪个子控件都由
          `keyPressEvent` 统一接管，不走聚焦按钮的默认激活。

        提问卡"强制聚焦提交按钮 → 连按 Enter 一路放行"的问题在本卡不复现：
        拒绝有独立焦点 + Esc 快捷键，且 danger 档位另有 500ms 防误触闸门。
        """
        super().showEvent(event)
        if event.isAccepted():
            QTimer.singleShot(0, self._focus_deny)

    def _focus_deny(self) -> None:
        if self.isVisible():
            self._deny_btn.setFocus()

    def _unlock_allow(self) -> None:
        self._allow_btn.setEnabled(True)

    def _cancel_allow_unlock(self) -> None:
        if self._allow_unlock_timer is not None:
            self._allow_unlock_timer.stop()
            self._allow_unlock_timer = None

    def _toggle_meta(self) -> None:
        visible = not self._meta_label.isVisible()
        self._meta_label.setVisible(visible)
        self._meta_toggle.setText("▾ 详情" if visible else "▸ 详情")
        self.updateGeometry()
        self._emit_height_changed()

    def _on_preview(self) -> None:
        if self._preview_payload is not None:
            self.previewRequested.emit(self._preview_payload)

    def _on_deny(self) -> None:
        self._decide("deny", REMEMBER_NONE)

    def _on_deny_with_reason(self, pos) -> None:
        """拒绝按钮右键 → 输入理由后拒绝（不做卡内嵌入输入框）"""
        text, ok = QInputDialog.getText(self, "拒绝并说明原因", "告诉 AI 应该怎么做：")
        if not ok:
            return
        self._decide("deny", REMEMBER_NONE, (text or "").strip())

    def _build_remember_menu(self):
        """构建记住菜单（供测试直调断言启用态与项数）

        三项作用域：本轮（可用）/ 本次会话（danger 档位禁用）/ 删除类会话豁免（恒禁用）。
        最后一项仅作说明性占位——删除类不给会话级豁免，故永远禁用。

        用 `qfluentwidgets.RoundMenu` 而非原生 `QMenu`：原生菜单不跟随主题，
        深色主题下是白底系统样式，与卡片割裂（用户反馈"记住的弹窗样式很丑"）。
        RoundMenu 自动跟随 qfluentwidgets 主题，与设置页菜单观感一致。
        """
        menu = RoundMenu(parent=self)
        act_round = Action("本轮对话内不再询问", menu)
        act_session = Action("本次会话内不再询问", menu)
        act_danger = Action("⚠ 本次会话内允许删除类命令", menu)
        # danger 档位不给会话级豁免（删除类风险最高，仅允许逐次确认）
        act_session.setEnabled(self._risk != RISK_DANGER)
        act_danger.setEnabled(False)
        menu.addAction(act_round)
        menu.addAction(act_session)
        menu.addSeparator()
        menu.addAction(act_danger)
        # 显式留存引用：不依赖 actions() 索引（separator 可能改变索引语义）
        menu._act_round = act_round  # type: ignore[attr-defined]
        menu._act_session = act_session  # type: ignore[attr-defined]
        return menu

    def _show_remember_menu(self) -> None:
        """弹出记住菜单（不平铺：平铺会让「扩大授权」与「允许」视觉同权，用户易误点）"""
        menu = self._build_remember_menu()
        act_round = menu._act_round  # type: ignore[attr-defined]
        act_session = menu._act_session  # type: ignore[attr-defined]
        chosen = menu.exec_(self._remember_btn.mapToGlobal(self._remember_btn.rect().bottomLeft()))
        if chosen is act_round:
            self._decide("allow", REMEMBER_ROUND)
        elif chosen is act_session and act_session.isEnabled():
            self._decide("allow", REMEMBER_SESSION)

    def _decide(self, decision: str, remember: str, reason: str = "") -> None:
        """统一出口：危险操作防误触闸门 + 防重复 emit + 清理定时器

        danger 档位在解锁前对 **allow 类决策**统一拦截——鼠标路径本就被
        setEnabled(False) 挡住，但键盘 Enter 会直达此处；连按 Enter 恰恰是
        最典型的误触形态（手还停在键盘上，弹卡瞬间 Enter 就来了），
        若只挡鼠标等于给最快路径开后门。
        """
        if self._decided:
            return
        if decision == "allow" and self._risk == RISK_DANGER and not self._allow_btn.isEnabled():
            # 不静默失败的可见反馈：提示行临时改为等待文案
            logger.debug(f"[PermissionCard] danger 防误触期内拦截 allow（remember={remember}）")
            if not getattr(self, "_lock_hint_shown", False):
                self._lock_hint_shown = True
                self._hint_label.setText(f"⚠ 危险操作，请确认后稍候 {_DANGER_ALLOW_DELAY_MS}ms 再执行")
                QTimer.singleShot(_DANGER_ALLOW_DELAY_MS, self._restore_hint_text)
            return
        self._decided = True
        self._cancel_allow_unlock()
        self.answered.emit(decision, remember, reason)

    def _restore_hint_text(self) -> None:
        """恢复提示行文案（防误触拦截提示用后即还原）"""
        self._lock_hint_shown = False
        if not self._decided:
            self._hint_label.setText(_HINT_TEXT)

    def closeEvent(self, event):
        """关闭（任何非显式允许路径）→ 由宿主按 deny 处理

        安全默认：不因「没选」而变成允许。

        ⚠ 只 emit `cancelled`，**不再同时 emit answered("deny")**：
        宿主 `_on_permission_cancelled` 已完整处理 deny 语义（读 id → decide deny）。
        若再发 answered，宿主第二次进来时 id 已被首次消费清空 → 会打出一条
        "决策无对应 tool_call_id" 的**误导性 WARNING**（决策本身是正确的），
        排障时容易被误判为异常。消除冗余优于降级日志级别（降级会让真正的
        id 丢失场景也更难发现）。
        """
        if not self._decided:
            self._decided = True
            self._cancel_allow_unlock()
            self.cancelled.emit()
        super().closeEvent(event)

    # ══════════════════ 尺寸 ══════════════════

    def resizeEvent(self, event):
        super().resizeEvent(event)
        new_w = event.size().width()
        if new_w != getattr(self, "_last_layout_width", -1):
            self._last_layout_width = new_w
            self.updateGeometry()
            self._emit_height_changed()

    def heightForWidth(self, w):
        lay = self.layout()
        return lay.heightForWidth(w) if lay is not None else super().heightForWidth(w)

    def sizeHint(self):
        lay = self.layout()
        if lay is not None:
            return QSize(self.width() or 0, lay.sizeHint().height())
        return super().sizeHint()

    def _emit_height_changed(self):
        """合并同一轮事件循环内的多次 heightChanged（防布局自激循环）"""
        if self._height_emit_pending:
            return
        self._height_emit_pending = True
        QTimer.singleShot(0, self._flush_height_changed)

    def _flush_height_changed(self):
        self._height_emit_pending = False
        self.heightChanged.emit()
