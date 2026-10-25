# -*- coding: utf-8 -*-
"""
安全中心设置页（LLMSettingsCard 内嵌分页）

布局（对齐 WorkBuddy 截图）：
- 沙箱安全卡：总开关 + 文件安全/命令安全/网络安全三个名单编辑区
- 数据安全卡：删除审批开关 + 垃圾桶 + 备份入口（容量上限展示）
- 系统级工具卡：豁免开关
- 底部说明：能力边界（应用层拦截，非 OS 级沙箱）

所有开关/名单变更实时写回 SandboxConfig 并落盘；读取失败由 SandboxConfig
内部兜底，不阻断 UI。
"""

import threading

from loguru import logger

from app.tools.sandbox import SandboxConfig, read_approval_policy
from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

try:
    from qfluentwidgets import (
        BodyLabel,
        ComboBox,
        ExpandGroupSettingCard,
        PushSettingCard,
        SpinBox,
        SwitchSettingCard,
        FluentIcon as FIF,
    )
except ImportError:  # 兜底：qfluentwidgets 缺失时用 Qt 原生，保功能不保观感
    from PyQt5.QtWidgets import QCheckBox as SwitchSettingCard  # type: ignore
    from PyQt5.QtWidgets import QSpinBox  # type: ignore

    BodyLabel = PushSettingCard = ExpandGroupSettingCard = None  # type: ignore
    SpinBox = QSpinBox  # type: ignore
    ComboBox = None  # type: ignore
    FIF = None

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _ListEditorCard(QWidget):
    """名单编辑区：标题 + 条目行（删除）+ 追加行（输入+添加按钮）

    样式：容器级 setStyleSheet（Colors token），子控件统一继承；
    条目行在 reload() 重建时同样继承，无需逐行设样式。
    """

    def __init__(
        self,
        title: str,
        config_key: str,
        placeholder: str,
        parent=None,
        show_title: bool = True,
        tip: str = "",
    ):
        super().__init__(parent)
        self._key = config_key
        # 内容变化回调：折叠卡模式下由 SecurityCenterCard 绑定到
        # ExpandGroupSettingCard._adjustViewSize，条目增删后重算展开高度
        self.on_change = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if show_title:
            # 折叠卡模式下标题由卡片头承载，内嵌模式不再重复显示
            title_label = QLabel(title)
            title_label.setObjectName("sciTitle")
            layout.addWidget(title_label)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 2, 0, 0)
        self._rows_layout.setSpacing(4)
        layout.addLayout(self._rows_layout)
        add_row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText(placeholder)
        if tip:
            # 长说明走悬停提示，输入框只留示例，避免说明文字堆在界面上
            self._input.setToolTip(tip)
        self._input.returnPressed.connect(self._add_current)
        add_btn = QPushButton("添加")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._add_current)
        add_row.addWidget(self._input, 1)
        add_row.addWidget(add_btn)
        layout.addLayout(add_row)
        self.setStyleSheet(self._theme_qss())
        self.reload()

    @staticmethod
    def _theme_qss() -> str:
        """主题化样式：修复裸控件黑块/黑字（浅色主题下按钮默认样式不可读）"""
        return f"""
            QLabel#sciTitle {{
                color: {Colors.TEXT_SECONDARY};
                background: transparent;
                {font_size_css(13)}
                font-weight: bold;
                {get_font_family_css()}
            }}
            QLabel#sciEntry {{
                color: {Colors.TEXT_PRIMARY};
                background: transparent;
                {font_size_css(12)}
                {get_font_family_css()}
            }}
            QWidget#sciRow {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
            }}
            QWidget#sciRow:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.TEXT_ACCENT};
            }}
            QPushButton#sciDelete {{
                background: transparent;
                color: {Colors.TEXT_SECONDARY};
                border: none;
                padding: 2px 8px;
                {font_size_css(11)}
                {get_font_family_css()}
            }}
            QPushButton#sciDelete:hover {{
                color: {Colors.TEXT_ACCENT};
                background: transparent;
                border: none;
            }}
            QLineEdit {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 5px 10px;
                {font_size_css(12)}
                {get_font_family_css()}
            }}
            QLineEdit:focus {{
                border: 1px solid {Colors.TEXT_ACCENT};
            }}
            QPushButton {{
                background-color: {Colors.SELECTED_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 5px 14px;
                {font_size_css(12)}
                {get_font_family_css()}
            }}
            QPushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.TEXT_ACCENT};
            }}
            QPushButton:pressed {{
                background-color: {Colors.TAB_ACTIVE_BG};
            }}
        """

    # ── 配置联动（供测试直接调用的稳定接口见 _add_path_entry/_remove_path_entry）──
    def _dotted_key(self) -> str:
        # 各编辑区的完整配置键（plan 参考实现的两分法漏了 network 前缀）
        return {
            "whitelist": "path.whitelist",
            "blacklist": "path.blacklist",
            "allow_prefixes": "command.allow_prefixes",
            "confirm_prefixes": "command.confirm_prefixes",
            "blacklist_domains": "network.blacklist_domains",
            "delete_exempt": "delete.exempt_paths",
        }[self._key]

    def _entries(self) -> list:
        return list(SandboxConfig.get_instance().get(self._dotted_key()) or [])

    def _write_entries(self, entries: list) -> None:
        cfg = SandboxConfig.get_instance()
        cfg.set(self._dotted_key(), entries)
        cfg.save()

    def _add_current(self):
        text = self._input.text().strip()
        if not text:
            return
        self._add_path_entry(self._key, text)
        self._input.clear()

    def _remove_at(self, index: int):
        entries = self._entries()
        if 0 <= index < len(entries):
            entries.pop(index)
            self._write_entries(entries)
        self.reload()

    # 供测试直接调用的稳定接口
    def _add_path_entry(self, key: str, value: str):
        entries = self._entries()
        if value not in entries:
            entries.append(value)
            self._write_entries(entries)
        self.reload()

    def _remove_path_entry(self, key: str, index: int):
        self._remove_at(index)

    def reload(self):
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, entry in enumerate(self._entries()):
            row = QHBoxLayout()
            row.setContentsMargins(10, 0, 6, 0)
            entry_label = QLabel(entry)
            entry_label.setObjectName("sciEntry")
            row.addWidget(entry_label, 1)
            del_btn = QPushButton("删除")
            del_btn.setObjectName("sciDelete")
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.clicked.connect(lambda _=False, idx=i: self._remove_at(idx))
            row.addWidget(del_btn)
            wrapper = QWidget()
            wrapper.setObjectName("sciRow")
            # QWidget 默认不画 QSS 背景，必须显式开启
            wrapper.setAttribute(Qt.WA_StyledBackground, True)
            wrapper.setFixedHeight(32)
            wrapper.setLayout(row)
            self._rows_layout.addWidget(wrapper)
        if callable(self.on_change):
            self.on_change()


class SecurityCenterCard(QWidget):
    """安全中心页主体"""

    # 后台统计完成 → 主线程回填（参数：文件数, 总字节）。rglob 大目录不能在
    # 主线程做：2026-10-25 垃圾桶里躺着整个 C 盘副本时，同步统计把启动后的
    # 设置面板构建冻死（py-spy 实锤 MainThread 卡在 pathlib.stat）。
    _snapshot_stats_ready = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = SandboxConfig.get_instance()
        self._snapshot_cache = None  # (count, size_bytes) 最近一次后台统计结果
        self._snapshot_stats_ready.connect(self._on_snapshot_stats_ready)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── 沙箱安全卡 ──
        self.sandbox_switch = SwitchSettingCard(
            FIF.CERTIFICATE if FIF else None,
            "沙箱安全（默认关闭）",
            "命令与文件操作经检查",
        )
        self.sandbox_switch.setToolTip(
            "AI 执行命令与文件操作经本沙箱检查；关闭后文件、命令、删除审批、网络四层全部失效。"
            "进程配额与垃圾桶不受本开关影响，始终按各自设置生效。"
        )
        self.sandbox_switch.setChecked(bool(self._cfg.get("sandbox_enabled")))
        self.sandbox_switch.checkedChanged.connect(self._on_sandbox_toggled)
        layout.addWidget(self.sandbox_switch)

        # 名单编辑区（挂入下方三大分组卡，不再各自折叠）
        self._path_white = _ListEditorCard(
            "写入白名单（放行 workdir 外写入）",
            "whitelist",
            "如 D:\\other_proj 或 %USERPROFILE%\\docs",
            tip="读操作不受此限制；相对路径按工作目录解析。",
        )
        self._path_black = _ListEditorCard(
            "文件黑名单（读写均拦截）",
            "blacklist",
            "如 D:\\secrets 或 .env",
            tip="⚠ 文件名需完全相等：.env 不拦 .env.local，建议一并添加。",
        )
        self._cmd_allow = _ListEditorCard(
            "命令放行前缀（跳过审批；危险命令仍拦截）",
            "allow_prefixes",
            "如 git status、npm run build",
            tip="⚠ 按词边界匹配；填 git 可省常规子命令审批，但 git rm -rf . 等破坏性操作仍会确认。",
        )
        self._cmd_confirm = _ListEditorCard(
            "命令强制审批前缀",
            "confirm_prefixes",
            "如 git push",
        )
        self._net_domains = _ListEditorCard(
            "网络域名黑名单",
            "blacklist_domains",
            "如 evil.com",
        )

        # ── 数据安全卡（开关先构造，稍后由「删除与备份」分组收纳）──
        self.delete_switch = SwitchSettingCard(
            FIF.DELETE if FIF else None,
            "删除审批",
            "删除类命令强制审批",
        )
        self.delete_switch.setToolTip(
            "开启后：删除类命令强制审批（放行/拒绝由你决定）。关闭后删除类命令不再审批。\n"
            "是否保留被删文件看「垃圾桶」开关（独立生效）。\n"
            "命令黑名单（如 cacls）、网络外传、文件黑名单与手动加严的前缀仍会拦。\n"
            "⚠ 需先开启最上方的「沙箱安全」，否则本开关不生效。"
        )
        self.delete_switch.setChecked(bool(self._cfg.get("delete_protection")))
        self.delete_switch.checkedChanged.connect(self._on_delete_toggled)

        # 垃圾桶软删除开关（与审批独立；组内收纳，见 _build_data_group）
        self.trash_switch = SwitchSettingCard(
            FIF.DELETE if FIF else None,
            "垃圾桶",
            "删除内容移入垃圾箱目录（软删除，可找回）",
        )
        self.trash_switch.setToolTip(
            "开启后：删除命令执行前，目标先移动到垃圾箱目录（backups/deletes），原命令不再实际删除。\n"
            "关闭时：不保留，删除即真删（原命令正常删除目标）。"
        )
        self.trash_switch.setChecked(bool(self._cfg.get("delete.trash_enabled")))
        self.trash_switch.checkedChanged.connect(self._on_trash_toggled)

        # 删除豁免：这些路径内的删除不弹审批（如 tests/），也不做快照
        self._delete_exempt = _ListEditorCard(
            "删除豁免路径",
            "delete_exempt",
            "如 tests/ 或 D:/other_proj/logs",
            tip="这些目录内的删除不再弹审批，也不进垃圾箱。相对路径按工作目录解析（如 tests/）。"
            "\n⚠ 与文件黑名单重叠时以黑名单为准（拉黑的路径不会被本豁免放行）。",
        )

        self.backup_dir_card = PushSettingCard(
            "打开目录",
            FIF.FOLDER if FIF else None,
            "原文件备份位置（backups/edits）",
            # C2：不含具体数值 —— 数值由下方 SpinBox 与 hint 承载，
            # 避免同屏两处显示同一配置但一处是构造期快照（改值后不一致）
            "每次改文件前自动留一份原文件",
        )
        self.backup_dir_card.clicked.connect(lambda: self._open_backup_dir())

        # ── EU-G12：网络外传检测开关（外层）──
        self.net_switch = SwitchSettingCard(
            FIF.GLOBE if FIF else None,
            "网络外传检测",
            "联网命令带 URL 或上传参数时审批",
        )
        self.net_switch.setToolTip(
            "对具备网络能力的命令生效：curl/wget/nc/scp/ftp 等、git 远程子命令（clone/fetch/pull/push）、"
            "以及代码里的 requests/urllib/Invoke-WebRequest 调用。\n"
            "普通命令里的 URL 只当文本（如 commit message 里的链接），不会触发审批。\n"
            "⚠ 需先开启最上方的「沙箱安全」。"
        )
        self.net_switch.setChecked(bool(self._cfg.get("network.enabled")))
        self.net_switch.checkedChanged.connect(self._on_network_toggled)

        # ── 系统级工具豁免开关（外层）──
        self.sys_switch = SwitchSettingCard(
            FIF.COMMAND_PROMPT if FIF else None,
            "系统级工具豁免",
            "这些命令绕过沙箱审批",
        )
        self.sys_switch.setToolTip(
            "wsl/wmic/sc/reg/schtasks/diskpart/bcdedit 等命令绕过沙箱审批，含其删除类操作，请谨慎启用。\n"
            "不影响 deny 类危险命令（如 cacls，仍硬拦）。\n"
            "⚠ 需先开启最上方的「沙箱安全」。"
        )
        self.sys_switch.setChecked(bool(self._cfg.get("sys_tools_bypass")))
        self.sys_switch.checkedChanged.connect(self._on_sys_toggled)

        # ── 首屏结构：生效状态 + 开关区（全部最外层）+ 折叠明细区 + 说明 ──
        layout.addWidget(self._build_status_row())
        layout.addWidget(self.sandbox_switch)
        layout.addWidget(self.delete_switch)
        layout.addWidget(self.trash_switch)
        layout.addWidget(self.net_switch)
        layout.addWidget(self.sys_switch)
        # ── 审核处理卡（处理方法/审核模型/超时/超时处理，全页仅此一份）──
        layout.addWidget(self._build_approval_card())
        # 折叠明细区按「作用对象」分组，不再把无关配置拼进同一组
        layout.addWidget(self._build_files_group())
        layout.addWidget(self._build_command_group())
        layout.addWidget(self._build_delete_group())
        layout.addWidget(self._build_network_group())
        layout.addWidget(self._build_quota_group())
        layout.addWidget(self._build_backup_group())

        # ── 底部说明 ──
        # N1：默认值提示（沙箱与删除审批默认关闭，用户需手动开启）
        # MCP / upload_file 说明：三类不受本页约束的操作，避免虚假安全感
        _note_text = (
            "说明：应用层拦截，非 OS 级沙箱隔离。\n"
            "沙箱安全与删除审批默认关闭，需手动开启后才会拦截；"
            "读操作除黑名单外不受路径约束，白名单与 workdir 边界仅约束写入。\n"
            "进程配额与垃圾桶不受沙箱开关影响，始终按各自设置生效。\n"
            "MCP 工具与 upload_file / webfetch 不受本页约束。"
        )
        note = BodyLabel(_note_text) if BodyLabel else QLabel(_note_text)
        note.setWordWrap(True)
        note.setToolTip(
            "MCP 工具（mcp__*）由外部服务提供，其读写/外传行为不在本页检查范围内；"
            "upload_file（上传到 Gitee）与 webfetch（抓取网页）不受域名黑名单约束。"
            "如需拦截，请在「工具」页关闭对应工具（关闭后会走审批确认）。"
        )
        layout.addWidget(note)

        layout.addStretch(1)

    # ══════════════════ 分组收纳 ══════════════════

    def _build_status_row(self) -> QWidget:
        """生效状态条：一眼看出哪几层规则真正在跑

        存在的理由：本页 5 个开关里有 4 个依赖「沙箱安全」总开关，
        开关看着是开的但总开关关掉时全部无效。原界面把这个依赖藏在 tooltip 里，
        用户配完白名单发现不生效，无处可查。
        """
        w = QWidget()
        w.setObjectName("sciStatus")
        w.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        self._status_title = QLabel("")
        self._status_title.setObjectName("sciStatusTitle")
        lay.addWidget(self._status_title)

        self._status_chips: dict = {}
        self._status_layout = QHBoxLayout()
        self._status_layout.setSpacing(6)
        for key, text in (
            ("files", "文件"),
            ("command", "命令"),
            ("delete", "删除"),
            ("network", "网络"),
        ):
            chip = QLabel(text)
            chip.setObjectName("sciChip")
            chip.setAttribute(Qt.WA_StyledBackground, True)
            self._status_chips[key] = chip
            self._status_layout.addWidget(chip)
        lay.addLayout(self._status_layout)
        lay.addStretch(1)

        w.setStyleSheet(self._status_qss())
        self._refresh_status()
        return w

    def _status_qss(self) -> str:
        Colors.refresh()
        return f"""
            QWidget#sciStatus {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
            QLabel#sciStatusTitle {{
                color: {Colors.TEXT_PRIMARY};
                background: transparent;
                {font_size_css(12)}
                font-weight: bold;
                {get_font_family_css()}
            }}
            QLabel#sciChip {{
                border-radius: 9px;
                padding: 1px 9px;
                {font_size_css(11)}
                {get_font_family_css()}
            }}
        """

    def _refresh_status(self) -> None:
        """按配置刷新状态条（总开关/各层开关任一变化时调用）"""
        if not hasattr(self, "_status_title"):
            return
        sandbox_on = bool(self._cfg.get("sandbox_enabled"))
        # 每层的真实生效条件：沙箱总开关 && 该层自身开关
        # 「删除」层的自身开关是 delete_protection（垃圾桶独立，另在提示行说明）
        layers = {
            "files": True,  # 路径规则无独立开关，随总开关
            "command": True,  # 命令规则同理
            "delete": bool(self._cfg.get("delete_protection")),
            "network": bool(self._cfg.get("network.enabled")),
        }
        active = [k for k, own in layers.items() if sandbox_on and own]
        if sandbox_on:
            self._status_title.setText("安全中心已开启")
        else:
            self._status_title.setText("安全中心已关闭")
        Colors.refresh()
        on_qss = (
            f"background-color: {Colors.SELECTED_BG}; color: {Colors.TEXT_PRIMARY}; "
            f"border: 1px solid {Colors.BORDER};"
        )
        off_qss = (
            f"background-color: transparent; color: {Colors.TEXT_MUTED}; "
            f"border: 1px solid {Colors.BORDER};"
        )
        labels = {"files": "文件", "command": "命令", "delete": "删除", "network": "网络"}
        for key, chip in self._status_chips.items():
            on = key in active
            chip.setText(f"{labels[key]} {'✓' if on else '✗'}")
            chip.setStyleSheet(on_qss if on else off_qss)
            chip.setToolTip(
                self._status_tip(key, sandbox_on, layers[key])
            )
    def _status_tip(self, key: str, sandbox_on: bool, own_on: bool) -> str:
        """状态条 chip 的悬停解释（说明为何无效）"""
        names = {"files": "文件规则", "command": "命令规则", "delete": "删除审批", "network": "网络外传"}
        if not sandbox_on:
            return f"{names[key]}未生效：需先开启最上方的「沙箱安全」。"
        if not own_on:
            return f"{names[key]}未生效：该层开关已关闭。"
        return f"{names[key]}生效中。"

    def _bind_editor_growth(self, card: "ExpandGroupSettingCard", *editors: "_ListEditorCard") -> None:
        """名单编辑区行数变化 → 冒泡重算分组卡展开高度（延迟一拍等布局落定）

        ⚠ 先强制 editor 自身布局激活再逐 widget 求和（原版 _adjustViewSize 策略）：
        viewLayout.sizeHint() 有陈旧缓存（子项变化不传播，实测恒旧值），不可用。
        """
        from PyQt5.QtCore import QTimer

        def _sync():
            def _do():
                for editor in editors:
                    editor.layout().invalidate()
                    editor.layout().activate()
                card._adjustViewSize()

            QTimer.singleShot(0, _do)

        for editor in editors:
            editor.on_change = _sync

    def _build_rules_group(self) -> "ExpandGroupSettingCard":
        """（已废弃，保留别名兼容旧测试）文件与命令规则 → 已拆为 _build_files_group/_build_command_group"""
        return self._files_group

    def _build_files_group(self) -> "ExpandGroupSettingCard":
        """文件写入：白名单 / 文件黑名单（都是路径匹配，同组同语义）"""
        card = ExpandGroupSettingCard(
            FIF.FOLDER_ADD if FIF else None,
            "文件写入",
            "改哪些路径要问过你 · 读操作只受黑名单约束",
        )
        for editor in (self._path_white, self._path_black):
            card.addGroupWidget(editor)
        self._bind_editor_growth(card, self._path_white, self._path_black)
        self._rules_group = card
        self._files_group = card
        return card

    def _build_command_group(self) -> "ExpandGroupSettingCard":
        """命令审批：放行前缀 / 强制审批前缀（都是命令串匹配）"""
        card = ExpandGroupSettingCard(
            FIF.COMMAND_PROMPT if FIF else None,
            "命令审批",
            "跑哪些命令要问过你 · 按词边界匹配",
        )
        for editor in (self._cmd_allow, self._cmd_confirm):
            card.addGroupWidget(editor)
        self._bind_editor_growth(card, self._cmd_allow, self._cmd_confirm)
        self._command_group = card
        return card

    def _build_delete_group(self) -> "ExpandGroupSettingCard":
        """删除与恢复：删除豁免路径 + 删除类备份统计（拦截与善后同一作用对象）"""
        card = ExpandGroupSettingCard(
            FIF.DELETE if FIF else None,
            "删除与恢复",
            "哪些目录不必问 · 误删了从哪里找回",
        )
        card.addGroupWidget(self._delete_exempt)
        card.addGroupWidget(self._build_snapshot_row())
        self._bind_editor_growth(card, self._delete_exempt)
        self._data_group = card
        self._delete_group = card
        return card

    def _build_network_group(self) -> "ExpandGroupSettingCard":
        """网络外传：域名黑名单（与进程配额无关，已拆开单列）"""
        card = ExpandGroupSettingCard(
            FIF.GLOBE if FIF else None,
            "网络外传",
            "连哪些地址要问过你 · 仅对具备网络能力的命令生效",
        )
        card.addGroupWidget(self._net_domains)
        self._bind_editor_growth(card, self._net_domains)
        self._net_group = card
        self._net_proc_group = card  # 旧名别名：原先该组合并了域名与配额
        return card

    def _build_quota_group(self) -> "ExpandGroupSettingCard":
        """进程配额：Job Object 三限额（不受沙箱总开关影响，独立生效）"""
        card = ExpandGroupSettingCard(
            FIF.SPEED_HIGH if FIF else None,
            "进程配额",
            "内存与进程数上限 · 不受上面任何开关影响",
        )
        card.addGroupWidget(self._build_job_limits_row())
        self._quota_group = card
        return card

    def _build_backup_group(self) -> "ExpandGroupSettingCard":
        """编辑类备份（独立于删除类备份）：备份卡 + 容量上限 + 清理"""
        card = ExpandGroupSettingCard(
            FIF.FOLDER if FIF else None,
            "编辑类备份",
            "改文件前自动留一份 · 容量上限与清理",
        )
        card.addGroupWidget(self.backup_dir_card)
        card.addGroupWidget(self._build_backup_limit_row())
        self._backup_group = card
        return card

    # ══════════════════ 审核处理组 ══════════════════

    def _build_approval_card(self) -> "ExpandGroupSettingCard":
        """审核处理组合配置（一个折叠项收纳四个关联配置）"""
        card = ExpandGroupSettingCard(FIF.CERTIFICATE if FIF else None, "审核处理", "")
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(6)

        # 处理方法
        self.approval_mode_combo = ComboBox()
        self.approval_mode_combo.addItem("人工审批", userData="manual")
        self.approval_mode_combo.addItem("AI审批", userData="ai")
        lay.addLayout(self._approval_row("处理方法", self.approval_mode_combo, ""))

        # 审核模型（mode=ai 或 超时处理=ai 时生效）
        self.approval_model_combo = ComboBox()
        self.approval_model_combo.setMinimumWidth(220)
        self._reload_approval_model_options()
        lay.addLayout(
            self._approval_row("审核模型", self.approval_model_combo, "AI审批与超时AI审核使用的模型；跟随主模型=当前会话模型。")
        )

        # 超时时间
        self.approval_timeout_spin = SpinBox()
        self.approval_timeout_spin.setRange(0, 86400)
        self.approval_timeout_spin.setSingleStep(30)
        self.approval_timeout_spin.setSuffix(" 秒")
        lay.addLayout(self._approval_row("超时时间", self.approval_timeout_spin, "人工审批等待超时；0 = 不超时。"))

        # 超时处理
        self.approval_timeout_action_combo = ComboBox()
        self.approval_timeout_action_combo.addItem("拒绝", userData="deny")
        self.approval_timeout_action_combo.addItem("允许", userData="allow")
        self.approval_timeout_action_combo.addItem("AI审核", userData="ai")
        lay.addLayout(
            self._approval_row("超时处理", self.approval_timeout_action_combo, "人工审批超时未决时的处理方式。")
        )

        card.addGroupWidget(body)

        # 初值反映配置
        policy = read_approval_policy(self._cfg)
        self._set_combo_by_data(self.approval_mode_combo, policy["mode"])
        self._set_combo_by_data(self.approval_timeout_action_combo, policy["timeout_action"])
        if policy["ai_model"]:
            self._set_combo_by_data(self.approval_model_combo, policy["ai_model"])
        self.approval_timeout_spin.setValue(policy["timeout_sec"])

        # 联动 + 写回
        self.approval_mode_combo.currentIndexChanged.connect(self._on_approval_changed)
        self.approval_model_combo.currentIndexChanged.connect(self._on_approval_changed)
        self.approval_timeout_spin.valueChanged.connect(self._on_approval_changed)
        self.approval_timeout_action_combo.currentIndexChanged.connect(self._on_approval_changed)
        self._approval_card = card
        self._sync_approval_ui()
        return card

    @staticmethod
    def _approval_row(label: str, editor: QWidget, tip: str) -> QHBoxLayout:
        """审核处理组行：左标签右控件"""
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label)
        lbl.setObjectName("sciEntry")
        if tip:
            lbl.setToolTip(tip)
            editor.setToolTip(tip)
        row.addWidget(lbl, 1)
        row.addWidget(editor)
        return row

    def _reload_approval_model_options(self):
        """审核模型下拉：已保存服务商的 display_name:模型名称 + 跟随主模型"""
        from app.utils.config import Settings

        combo = self.approval_model_combo
        current = combo.currentData()
        combo.clear()
        combo.addItem("跟随主模型", userData="")
        providers = Settings.get_instance().llm_saved_providers.value or {}
        for info in providers.values():
            if not isinstance(info, dict):
                continue
            display = info.get("display_name") or info.get("provider_name") or ""
            model = str(info.get("模型名称") or "")
            if not display or not model:
                continue
            value = f"{display}:{model}"
            combo.addItem(value, userData=value)
        if current:
            self._set_combo_by_data(combo, current)

    @staticmethod
    def _set_combo_by_data(combo, data):
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _on_approval_changed(self, *_):
        self._cfg.set(
            "approval",
            {
                "mode": self.approval_mode_combo.currentData() or "manual",
                "timeout_sec": int(self.approval_timeout_spin.value()),
                "timeout_action": self.approval_timeout_action_combo.currentData() or "deny",
                "ai_model": self.approval_model_combo.currentData() or "",
            },
        )
        self._cfg.save()
        self._sync_approval_ui()

    def _sync_approval_ui(self):
        """联动：AI 审批模式禁用超时两行；摘要行跟随配置"""
        mode = self.approval_mode_combo.currentData() or "manual"
        is_ai = mode == "ai"
        for w in (self.approval_timeout_spin, self.approval_timeout_action_combo):
            w.setEnabled(not is_ai)
        self.approval_timeout_spin.setToolTip(
            "AI审批模式下无人工等待，不适用超时。" if is_ai else "人工审批等待超时；0 = 不超时。"
        )
        card = getattr(self, "_approval_card", None)
        if card is None:
            return
        # 摘要行在卡片头（HeaderSettingCard，SettingCard 子类提供 setContent）
        if is_ai:
            card.card.setContent("AI审批 · 由审核模型自动判定放行/拒绝，失败回落人工")
        else:
            sec = self.approval_timeout_spin.value()
            action = self.approval_timeout_action_combo.currentData() or "deny"
            action_label = {"deny": "拒绝", "allow": "允许", "ai": "AI审核"}[action]
            timeout_text = "不超时" if sec <= 0 else f"超时 {sec}s 后{action_label}"
            card.card.setContent(f"人工审批 · {timeout_text}")

    # ══════════════════ EU-G10 / G11 / G9：构建各控件的独立入口 ══════════════════

    def _int_cfg(self, key: str, default: int = 0) -> int:
        """读取配置中的整数（类型安全：cfg.get 返回 Unknown 联合类型）

        统一收窄避免每处 `int(cfg.get(...) or 0)` 触发 pyright reportArgumentType。
        """
        raw = self._cfg.get(key)
        if raw is None or isinstance(raw, (dict, list)):
            return default
        try:
            return int(raw)
        except TypeError, ValueError:
            return default

    def _build_backup_limit_row(self) -> QWidget:
        """备份容量上限（0 = 不限）+ 立即清理按钮（只清 FileRecorder 侧）"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 4, 16, 4)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        label = QLabel("备份容量上限")
        label.setObjectName("sciTitle")
        label.setWordWrap(True)
        label.setToolTip("0 = 不限，不自动清理。")
        head.addWidget(label, 1)

        self.backup_limit_spin = SpinBox()
        self.backup_limit_spin.setRange(0, 102400)
        self.backup_limit_spin.setSingleStep(500)
        self.backup_limit_spin.setSuffix(" MB")
        self.backup_limit_spin.setToolTip("0 = 不限，不自动清理。")
        self.backup_limit_spin.setValue(self._int_cfg("backup_limit_mb"))
        self.backup_limit_spin.valueChanged.connect(self._on_backup_limit_changed)
        head.addWidget(self.backup_limit_spin)

        self.clean_btn = QPushButton("清理备份文件")
        self.clean_btn.setCursor(Qt.PointingHandCursor)
        self.clean_btn.clicked.connect(self._on_clean_backups)
        head.addWidget(self.clean_btn)
        lay.addLayout(head)

        self.backup_hint = QLabel("")
        self.backup_hint.setObjectName("sciHint")
        self.backup_hint.setWordWrap(True)
        lay.addWidget(self.backup_hint)

        self._sync_clean_btn_enabled()
        w.setStyleSheet(self._row_qss())
        return w

    def _build_snapshot_row(self) -> QWidget:
        """删除类备份：容量展示 + 打开目录 / 清空（清空需二次确认）"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 4, 16, 4)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        label = QLabel("删除类备份（垃圾箱）")
        label.setObjectName("sciTitle")
        label.setToolTip(
            "AI 删除文件前自动移入/快照到此目录，是误删后的恢复手段，清空后不可找回。"
            "独立配额 = 备份上限的 1/4（下限 100 MB），不受「清理备份文件」影响。"
        )
        head.addWidget(label, 1)

        self.snapshot_btn = QPushButton("打开目录")
        self.snapshot_btn.setCursor(Qt.PointingHandCursor)
        self.snapshot_btn.clicked.connect(self._open_snapshot_dir)
        head.addWidget(self.snapshot_btn)

        self.snapshot_clear_btn = QPushButton("清空快照")
        self.snapshot_clear_btn.setObjectName("sciDanger")
        self.snapshot_clear_btn.setCursor(Qt.PointingHandCursor)
        self.snapshot_clear_btn.clicked.connect(self._on_clear_snapshots)
        head.addWidget(self.snapshot_clear_btn)
        lay.addLayout(head)

        self.snapshot_label = QLabel("")
        self.snapshot_label.setObjectName("sciHint")
        self.snapshot_label.setWordWrap(True)
        lay.addWidget(self.snapshot_label)

        self._refresh_snapshot_stats()
        w.setStyleSheet(self._row_qss())
        return w

    def _build_job_limits_row(self) -> QWidget:
        """受管进程资源配额（Job Object）：三限额，0 = 不限"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 4, 16, 4)
        lay.setSpacing(4)

        title = QLabel("受管进程资源配额")
        title.setObjectName("sciTitle")
        title.setWordWrap(True)
        title.setToolTip(
            "0 = 不限。仅约束 AI 通过 bash / bg_start 与持久 shell 会话启动的进程；"
            "DriFox 自身界面进程与用户手动启动的程序不受约束。"
        )
        lay.addWidget(title)

        # 口径说明：内存按单进程计，不是整棵进程树总和（用户最容易误解的一点）
        hint = QLabel(
            "内存上限按单个进程计，不是进程树总和。仅约束 bash、后台命令与持久 shell；"
            "不受沙箱开关影响，始终生效。"
        )
        hint.setObjectName("sciHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.job_spins = {}
        spec = (
            ("memory_mb", "内存上限", " MB", 0, 65536, 256),
            ("active_process", "进程数上限", " 个", 0, 1024, 8),
            ("cpu_time_ms", "CPU 时间上限", " 毫秒", 0, 3600000, 1000),
        )
        for key, text, suffix, lo, hi, step in spec:
            row = QHBoxLayout()
            row.setSpacing(8)
            lbl = QLabel(text)
            lbl.setObjectName("sciEntry")
            lbl.setWordWrap(True)
            row.addWidget(lbl, 1)
            spin = SpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
            spin.setSuffix(suffix)
            cur = (self._cfg.get("job_limits") or {}).get(key)
            spin.setValue(self._int_cfg(f"job_limits.{key}"))
            spin.valueChanged.connect(lambda val, k=key: self._on_job_limit_changed(k, val))
            row.addWidget(spin)
            self.job_spins[key] = spin
            lay.addLayout(row)

        w.setStyleSheet(self._row_qss())
        return w

    @staticmethod
    def _row_qss() -> str:
        """本卡新增行的统一主题样式（每次调用重新取 token）"""
        Colors.refresh()
        return f"""
            QLabel#sciTitle {{
                color: {Colors.TEXT_SECONDARY};
                background: transparent;
                {font_size_css(13)}
                font-weight: bold;
                {get_font_family_css()}
            }}
            QLabel#sciEntry {{
                color: {Colors.TEXT_PRIMARY};
                background: transparent;
                {font_size_css(12)}
                {get_font_family_css()}
            }}
            QLabel#sciHint {{
                color: {Colors.TEXT_SECONDARY};
                background: transparent;
                {font_size_css(11)}
                {get_font_family_css()}
            }}
            QPushButton {{
                background-color: {Colors.SELECTED_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px 12px;
                {font_size_css(12)}
                {get_font_family_css()}
            }}
            QPushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.TEXT_ACCENT};
            }}
            QPushButton:disabled {{
                color: {Colors.TEXT_SECONDARY};
                background-color: {Colors.CONTENT_BG};
            }}
            QPushButton#sciDanger {{
                color: {Colors.TEXT_ACCENT};
            }}
        """

    # ══════════════════ EU-G10/G11/G9 回调 ══════════════════

    def _on_backup_limit_changed(self, value: int):
        self._cfg.set("backup_limit_mb", int(value))
        self._cfg.save()
        self._sync_clean_btn_enabled()
        self._refresh_backup_hint()

    def _sync_clean_btn_enabled(self) -> None:
        """0 = 不限 → 按钮禁用 + tooltip 说明（避免点了没反应以为坏了）"""
        limit = self._int_cfg("backup_limit_mb")
        self.clean_btn.setEnabled(limit > 0)
        self.clean_btn.setToolTip("" if limit > 0 else "未设上限（0 = 不限），清理已停用")
        self._refresh_backup_hint()

    def _refresh_backup_hint(self) -> None:
        limit = self._int_cfg("backup_limit_mb")
        if limit > 0:
            self.backup_hint.setText(
                f"当前上限 {limit} MB；超出后在下次启动或点「清理备份文件」时按从旧到新清理。"
                f"改动上限不影响已产生的备份，需手动点清理才立即生效。"
            )
        else:
            self.backup_hint.setText("未设上限：不自动清理，也不执行手动清理。")

    def _on_clean_backups(self):
        """立即清理（只清 FileRecorder 侧，不含删除快照）"""
        limit = self._int_cfg("backup_limit_mb")
        if limit <= 0:
            return
        try:
            from app.utils.file_operation_recorder import cleanup_backups_partitioned
            from app.utils.utils import get_app_data_dir
            from qfluentwidgets import InfoBar, InfoBarPosition

            result = cleanup_backups_partitioned(get_app_data_dir() / "backups", limit)
            removed = len(result.get("file_backups_removed") or [])
            InfoBar.success(
                "清理完成",
                f"已清理 {removed} 个备份文件（删除快照不受影响）。",
                duration=3000,
                parent=self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            self._refresh_snapshot_stats()
        except Exception as e:  # noqa: BLE001 - 清理失败不应崩设置页
            logger.warning(f"[SecurityCenter] 备份清理失败: {e}")

    # ── EU-G11 快照 ──

    @staticmethod
    def _deleted_dir_stats() -> tuple:
        """返回 (文件数, 总字节)；目录不存在返回 (0, 0)"""
        from app.utils.utils import get_app_data_dir

        d = get_app_data_dir() / "backups" / "deletes"
        if not d.exists():
            return (0, 0)
        files = [f for f in d.rglob("*") if f.is_file()]
        total = 0
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                continue
        return (len(files), total)

    def _refresh_snapshot_stats(self) -> None:
        """异步统计删除备份体积；先占位，完成后信号回填（禁止主线程 rglob）"""
        self.snapshot_label.setText("正在统计删除备份…")
        threading.Thread(target=self._snapshot_stats_worker, daemon=True).start()

    def _snapshot_stats_worker(self) -> None:
        try:
            count, size = self._deleted_dir_stats()
        except Exception:  # noqa: BLE001 - 统计失败静默放弃，下次刷新重试
            return
        self._snapshot_stats_ready.emit(count, size)

    def _on_snapshot_stats_ready(self, count: int, size: int) -> None:
        # 卡片可能已随设置弹窗销毁（PyQt5 已删 wrapper 属性访问抛 RuntimeError）
        try:
            self._snapshot_cache = (count, size)
            mb = size / 1024 / 1024
            self.snapshot_label.setText(f"已有 {count} 项删除备份，占用 {mb:.1f} MB，可用于恢复误删。")
            self.snapshot_clear_btn.setEnabled(count > 0)
            self.snapshot_clear_btn.setToolTip("" if count > 0 else "当前没有删除备份")
        except RuntimeError:
            pass

    def _open_snapshot_dir(self):
        self._open_path_in_explorer(self._snapshot_dir())

    def _on_clear_snapshots(self):
        """清空删除类备份（破坏性操作，需二次确认）"""
        # 用后台统计缓存，不在主线程重新 rglob（2026-10-25 卡死事故）；
        # 缓存未就绪时按 0 处理（按钮此时本就禁用），等统计回填后再操作。
        count = self._snapshot_cache[0] if self._snapshot_cache else 0
        if count <= 0:
            return
        try:
            from app.widgets.common_dialogs import ConfirmDialog

            dialog = ConfirmDialog(
                title="清空删除类备份",
                content=f"将永久删除 {count} 项删除备份，清空后无法找回。确认继续？",
                parent=self.window(),
            )
            if not dialog.exec_():
                return
        except Exception as e:  # noqa: BLE001 - 对话框不可用时保守放弃
            logger.warning(f"[SecurityCenter] 快照清空确认框不可用，已放弃: {e}")
            return
        try:
            import shutil

            d = self._snapshot_dir()
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            logger.info(f"[SecurityCenter] 已清空删除类备份（{count} 项）")
            # 立即同步置空态（不等后台重统计回填），再触发后台刷新文案
            self._snapshot_cache = (0, 0)
            self.snapshot_clear_btn.setEnabled(False)
            self._refresh_snapshot_stats()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[SecurityCenter] 清空快照失败: {e}")

    @staticmethod
    def _snapshot_dir():
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "backups" / "deletes"

    @staticmethod
    def _open_path_in_explorer(path) -> None:
        """在系统文件管理器中打开目录（跨平台）

        ⚠ 必须覆盖三平台：旧实现只有 win32 分支，mac/linux 上点击完全静默
        无反应（用户以为按钮坏了）。DriFox 有 mac 打包（dmgbuild），属功能缺失。
        """
        import subprocess
        import sys

        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:  # noqa: BLE001 - 打开失败不应崩设置页
            logger.warning(f"[SecurityCenter] 打开目录失败: {path} ({e})")

    # ── EU-G9 job_limits 回写 ──

    def _on_job_limit_changed(self, key: str, value: int):
        limits = dict(self._cfg.get("job_limits") or {})
        limits[key] = int(value)
        self._cfg.set("job_limits", limits)
        self._cfg.save()

    # ── 名单编辑的 card 级稳定接口（按 key 路由到对应编辑区）──
    def _editor_for(self, key: str) -> _ListEditorCard:
        return {
            "whitelist": self._path_white,
            "blacklist": self._path_black,
            "allow_prefixes": self._cmd_allow,
            "confirm_prefixes": self._cmd_confirm,
            "blacklist_domains": self._net_domains,
            "delete_exempt": self._delete_exempt,
        }[key]

    def _add_path_entry(self, key: str, value: str):
        self._editor_for(key)._add_path_entry(key, value)

    def _remove_path_entry(self, key: str, index: int):
        self._editor_for(key)._remove_path_entry(key, index)

    # ── 开关回调：写回配置 ──
    def _on_sandbox_toggled(self, checked: bool):
        self._cfg.set("sandbox_enabled", bool(checked))
        self._cfg.save()
        self._refresh_status()
        logger.info(f"[SecurityCenter] 沙箱开关 → {checked}")

    def _on_delete_toggled(self, checked: bool):
        self._cfg.set("delete_protection", bool(checked))
        self._cfg.save()
        self._refresh_status()

    def _on_trash_toggled(self, checked: bool):
        self._cfg.set("delete.trash_enabled", bool(checked))
        self._cfg.save()
        logger.info(f"[SecurityCenter] 垃圾桶软删除 → {checked}")

    def _on_sys_toggled(self, checked: bool):
        self._cfg.set("sys_tools_bypass", bool(checked))
        self._cfg.save()
        logger.warning(f"[SecurityCenter] 系统级工具豁免 → {checked}")

    def _on_network_toggled(self, checked: bool):
        self._cfg.set("network.enabled", bool(checked))
        self._cfg.save()
        self._refresh_status()

    def _open_backup_dir(self):
        """打开编辑类备份目录（复用跨平台实现，避免两套 explorer 调用漂移）"""
        from app.utils.utils import get_app_data_dir

        self._open_path_in_explorer(get_app_data_dir() / "backups" / "edits")
