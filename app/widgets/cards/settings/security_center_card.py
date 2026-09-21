# -*- coding: utf-8 -*-
"""
安全中心设置页（LLMSettingsCard 内嵌分页）

布局（对齐 WorkBuddy 截图）：
- 沙箱安全卡：总开关 + 文件安全/命令安全/网络安全三个名单编辑区
- 数据安全卡：删除保护开关 + 备份入口（容量上限展示）
- 系统级工具卡：豁免开关
- 底部说明：能力边界（应用层拦截，非 OS 级沙箱）

所有开关/名单变更实时写回 SandboxConfig 并落盘；读取失败由 SandboxConfig
内部兜底，不阻断 UI。
"""

from loguru import logger

from app.tools.sandbox import SandboxConfig
from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

try:
    from qfluentwidgets import (
        BodyLabel,
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
    FIF = None

from PyQt5.QtCore import Qt
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = SandboxConfig.get_instance()
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── 沙箱安全卡 ──
        self.sandbox_switch = SwitchSettingCard(
            FIF.CERTIFICATE if FIF else None,
            "沙箱安全（默认关闭）",
            "命令与文件操作经检查",
        )
        self.sandbox_switch.setToolTip(
            "AI 执行命令与文件操作经沙箱检查；关闭后恢复无拦截行为。"
            "网络外传检测、删除保护快照与进程配额均依赖本开关。"
        )
        self.sandbox_switch.setChecked(bool(self._cfg.get("sandbox_enabled")))
        self.sandbox_switch.checkedChanged.connect(self._on_sandbox_toggled)
        layout.addWidget(self.sandbox_switch)

        def _expand(
            icon, title, content, editor: "_ListEditorCard", tip: str = ""
        ) -> "ExpandGroupSettingCard":
            """把名单编辑区包成折叠卡：收起一行，点击展开编辑（对齐设置页范式）"""
            card = ExpandGroupSettingCard(icon, title, content)
            if tip:
                card.setToolTip(tip)
            card.addGroupWidget(editor)

            from PyQt5.QtCore import QTimer

            # 条目增删后内容高度变化，展开态需要重算卡片高度，否则新行被滚动区吞掉。
            # 延迟一拍：deleteLater 的旧行 / 布局失效需先在事件循环落定，sizeHint 才准
            def _sync_height():
                QTimer.singleShot(0, card._adjustViewSize)

            editor.on_change = _sync_height
            return card

        self._path_white = _ListEditorCard(
            "写入白名单（放行 workdir 外的写入目录；读操作不受此限制）",
            "whitelist",
            "如 D:\\other_proj 或 %USERPROFILE%\\docs",
            show_title=False,
            tip="读操作不受此限制；相对路径按工作目录解析。",
        )
        self._path_black = _ListEditorCard(
            "文件黑名单（读写均拦截，如 .env/.ssh）",
            "blacklist",
            "如 D:\\secrets 或 .env",
            show_title=False,
            tip="⚠ 文件名需完全相等：.env 不拦 .env.local，建议一并添加。",
        )
        self._cmd_allow = _ListEditorCard(
            "命令安全 · 放行前缀（跳过审批；危险命令仍拦截）",
            "allow_prefixes",
            "如 git status、npm run build",
            show_title=False,
            tip="⚠ 按词边界匹配；填 git 可省常规子命令审批，"
            "但 git rm -rf . 等破坏性操作仍会确认。",
        )
        self._cmd_confirm = _ListEditorCard(
            "命令安全 · 强制审批前缀",
            "confirm_prefixes",
            "如 git push",
            show_title=False,
        )
        self._net_domains = _ListEditorCard(
            "网络安全 · 域名黑名单",
            "blacklist_domains",
            "如 evil.com",
            show_title=False,
        )

        self._path_white_card = _expand(
            FIF.FOLDER_ADD if FIF else None,
            "写入白名单",
            "放行工作目录外的写入",
            self._path_white,
            tip="放行 workdir 外的写入目录；读操作不受此限制。",
        )
        self._path_black_card = _expand(
            FIF.HIDE if FIF else None,
            "文件黑名单",
            "读写均拦截",
            self._path_black,
            tip="读写均拦截（如 .env/.ssh）。",
        )
        self._cmd_allow_card = _expand(
            FIF.ACCEPT if FIF else None,
            "命令安全 · 放行前缀",
            "跳过审批，危险命令仍拦截",
            self._cmd_allow,
            tip="跳过审批；危险命令仍拦截（示例见输入框提示）。",
        )
        self._cmd_confirm_card = _expand(
            FIF.CARE_RIGHT_SOLID if FIF else None,
            "命令安全 · 强制审批前缀",
            "命中即弹审批",
            self._cmd_confirm,
            tip="命中即弹审批（如 git push）。",
        )
        self._net_domains_card = _expand(
            FIF.GLOBE if FIF else None,
            "网络安全 · 域名黑名单",
            "命中即弹审批",
            self._net_domains,
            tip="命中域名的网络命令弹审批（如 evil.com）。",
        )
        for w in (
            self._path_white_card,
            self._path_black_card,
            self._cmd_allow_card,
            self._cmd_confirm_card,
            self._net_domains_card,
        ):
            layout.addWidget(w)

        # ── 数据安全卡 ──
        self.delete_switch = SwitchSettingCard(
            FIF.DELETE if FIF else None,
            "删除保护",
            "删除类命令强制审批，执行前自动快照",
        )
        self.delete_switch.setToolTip(
            "开启后：删除类命令强制审批，放行后执行前自动快照（可找回）。"
            "关闭后删除类命令不再审批；但命令黑名单（如 cacls）、网络外传、"
            "路径黑名单与手动加严的策略仍会拦截。"
        )
        self.delete_switch.setChecked(bool(self._cfg.get("delete_protection")))
        self.delete_switch.checkedChanged.connect(self._on_delete_toggled)
        layout.addWidget(self.delete_switch)

        # 删除豁免：这些路径内的删除不弹审批（如 tests/），也不做快照
        self._delete_exempt = _ListEditorCard(
            "删除豁免路径（这些目录内的删除不再弹审批，也不做快照，如 tests/）",
            "delete_exempt",
            "如 tests/ 或 D:/other_proj/logs",
            show_title=False,
            tip="相对路径按工作目录解析。",
        )
        self._delete_exempt_card = _expand(
            FIF.INFO if FIF else None,
            "删除豁免路径",
            "这些目录内的删除不再弹审批",
            self._delete_exempt,
            tip="这些目录内的删除不再弹审批，也不做快照。相对路径按工作目录解析（如 tests/）。",
        )
        layout.addWidget(self._delete_exempt_card)

        self.backup_dir_card = PushSettingCard(
            "打开备份目录",
            FIF.FOLDER if FIF else None,
            "自动备份",
            # C2：不含具体数值 —— 数值由下方 SpinBox 与 hint 承载，
            # 避免同屏两处显示同一配置但一处是构造期快照（改值后不一致）
            "修改前自动备份原文件（FileRecorder）",
        )
        self.backup_dir_card.clicked.connect(lambda: self._open_backup_dir())
        layout.addWidget(self.backup_dir_card)

        # ── EU-G10：备份容量上限编辑 + 立即清理（FileRecorder 侧，不含删除快照）──
        layout.addWidget(self._build_backup_limit_row())

        # ── EU-G11：删除快照容量展示 + 打开目录 / 清空快照 ──
        layout.addWidget(self._build_snapshot_row())

        # ── EU-G12：网络外传检测开关 ──
        self.net_switch = SwitchSettingCard(
            FIF.GLOBE if FIF else None,
            "网络外传检测",
            "拦截带 URL 或上传参数的命令",
        )
        self.net_switch.setToolTip("curl/wget 等；仅在沙箱安全开启时生效。")
        self.net_switch.setChecked(bool(self._cfg.get("network.enabled")))
        self.net_switch.checkedChanged.connect(self._on_network_toggled)
        layout.addWidget(self.net_switch)

        # ── 系统级工具卡 ──
        self.sys_switch = SwitchSettingCard(
            FIF.COMMAND_PROMPT if FIF else None,
            "系统级工具豁免",
            "这些命令绕过沙箱审批",
        )
        self.sys_switch.setToolTip(
            "wsl/wmic/sc/reg/schtasks/diskpart/bcdedit 等绕过沙箱审批，请谨慎启用。"
        )
        self.sys_switch.setChecked(bool(self._cfg.get("sys_tools_bypass")))
        self.sys_switch.checkedChanged.connect(self._on_sys_toggled)
        layout.addWidget(self.sys_switch)

        # ── EU-G9：受管进程资源配额（Job Object 三限额）──
        layout.addWidget(self._build_job_limits_row())

        # ── 底部说明 ──
        # N1：默认值提示（沙箱与删除保护默认关闭，用户需手动开启）
        # MCP / upload_file 说明：三类不受本页约束的操作，避免虚假安全感
        _note_text = (
            "说明：应用层拦截，非 OS 级沙箱隔离。\n"
            "沙箱安全与删除保护默认关闭，需手动开启后才会拦截；"
            "读操作除黑名单外不受路径约束，白名单与 workdir 边界仅约束写入。\n"
            "MCP 工具（mcp__*）与 upload_file / webfetch 由外部服务提供，"
            "不受本页约束（可在「工具」页关闭）。"
        )
        note = BodyLabel(_note_text) if BodyLabel else QLabel(_note_text)
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch(1)

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
        """删除快照：容量展示 + 打开目录 / 清空（清空需二次确认）"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 4, 16, 4)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        label = QLabel("删除保护快照")
        label.setObjectName("sciTitle")
        label.setToolTip(
            "快照是误删文件后的恢复手段，清空后不可找回。"
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
        title.setToolTip("0 = 不限；仅约束 Bash 与后台命令。")
        lay.addWidget(title)

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
                f"当前上限 {limit} MB；超出后从旧到新清理。"
                "改动上限后需重启才生效，可点「清理备份文件」立即清理。"
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

        d = get_app_data_dir() / "backups" / "deleted"
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
        count, size = self._deleted_dir_stats()
        mb = size / 1024 / 1024
        self.snapshot_label.setText(f"已有 {count} 项快照，占用 {mb:.1f} MB，可用于恢复误删。")
        self.snapshot_clear_btn.setEnabled(count > 0)
        self.snapshot_clear_btn.setToolTip("" if count > 0 else "当前没有快照")

    def _open_snapshot_dir(self):
        self._open_path_in_explorer(self._snapshot_dir())

    def _on_clear_snapshots(self):
        """清空删除快照（破坏性操作，需二次确认）"""
        count, _ = self._deleted_dir_stats()
        if count <= 0:
            return
        try:
            from app.widgets.common_dialogs import ConfirmDialog

            dialog = ConfirmDialog(
                title="清空删除快照",
                content=f"将永久删除 {count} 项删除快照，清空后无法找回。确认继续？",
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
            logger.info(f"[SecurityCenter] 已清空删除快照（{count} 项）")
            self._refresh_snapshot_stats()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[SecurityCenter] 清空快照失败: {e}")

    @staticmethod
    def _snapshot_dir():
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "backups" / "deleted"

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
        logger.info(f"[SecurityCenter] 沙箱开关 → {checked}")

    def _on_delete_toggled(self, checked: bool):
        self._cfg.set("delete_protection", bool(checked))
        self._cfg.save()

    def _on_sys_toggled(self, checked: bool):
        self._cfg.set("sys_tools_bypass", bool(checked))
        self._cfg.save()
        logger.warning(f"[SecurityCenter] 系统级工具豁免 → {checked}")

    def _on_network_toggled(self, checked: bool):
        self._cfg.set("network.enabled", bool(checked))
        self._cfg.save()

    def _open_backup_dir(self):
        """打开备份根目录（复用跨平台实现，避免两套 explorer 调用漂移）"""
        from app.utils.utils import get_app_data_dir

        self._open_path_in_explorer(get_app_data_dir() / "backups")
