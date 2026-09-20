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
        SwitchSettingCard,
        FluentIcon as FIF,
    )
except ImportError:  # 兜底：qfluentwidgets 缺失时用 Qt 原生，保功能不保观感
    from PyQt5.QtWidgets import QCheckBox as SwitchSettingCard  # type: ignore

    BodyLabel = PushSettingCard = ExpandGroupSettingCard = None  # type: ignore
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

    def __init__(self, title: str, config_key: str, placeholder: str, parent=None, show_title: bool = True):
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
            "沙箱安全",
            "AI 执行命令与文件操作经沙箱检查；关闭后恢复无拦截行为",
        )
        self.sandbox_switch.setChecked(bool(self._cfg.get("sandbox_enabled")))
        self.sandbox_switch.checkedChanged.connect(self._on_sandbox_toggled)
        layout.addWidget(self.sandbox_switch)

        def _expand(icon, title, content, editor: "_ListEditorCard") -> "ExpandGroupSettingCard":
            """把名单编辑区包成折叠卡：收起一行，点击展开编辑（对齐设置页范式）"""
            card = ExpandGroupSettingCard(icon, title, content)
            card.addGroupWidget(editor)

            from PyQt5.QtCore import QTimer

            # 条目增删后内容高度变化，展开态需要重算卡片高度，否则新行被滚动区吞掉。
            # 延迟一拍：deleteLater 的旧行 / 布局失效需先在事件循环落定，sizeHint 才准
            def _sync_height():
                QTimer.singleShot(0, card._adjustViewSize)

            editor.on_change = _sync_height
            return card

        self._path_white = _ListEditorCard(
            "文件安全 · 白名单（放行 workdir 外的写入目录）",
            "whitelist",
            "如 D:\\other_proj 或 %USERPROFILE%\\docs",
            show_title=False,
        )
        self._path_black = _ListEditorCard(
            "文件安全 · 黑名单（读写都强制审批，如 .env/.ssh）",
            "blacklist",
            "如 D:\\secrets 或 .env",
            show_title=False,
        )
        self._cmd_allow = _ListEditorCard(
            "命令安全 · 放行前缀（跳过审批；block 命令仍拦截）",
            "allow_prefixes",
            "如 git、npm run",
            show_title=False,
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
            "文件安全 · 白名单",
            "放行 workdir 外的写入目录",
            self._path_white,
        )
        self._path_black_card = _expand(
            FIF.HIDE if FIF else None,
            "文件安全 · 黑名单",
            "读写都强制审批（如 .env/.ssh）",
            self._path_black,
        )
        self._cmd_allow_card = _expand(
            FIF.ACCEPT if FIF else None,
            "命令安全 · 放行前缀",
            "跳过审批；block 命令仍拦截（如 git、npm run）",
            self._cmd_allow,
        )
        self._cmd_confirm_card = _expand(
            FIF.CARE_RIGHT_SOLID if FIF else None,
            "命令安全 · 强制审批前缀",
            "命中即弹审批（如 git push）",
            self._cmd_confirm,
        )
        self._net_domains_card = _expand(
            FIF.GLOBE if FIF else None,
            "网络安全 · 域名黑名单",
            "命中域名的网络命令弹审批（如 evil.com）",
            self._net_domains,
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
            "删除类命令强制审批，放行后执行前自动快照（可找回）",
        )
        self.delete_switch.setChecked(bool(self._cfg.get("delete_protection")))
        self.delete_switch.checkedChanged.connect(self._on_delete_toggled)
        layout.addWidget(self.delete_switch)

        # 删除豁免：这些路径内的删除不弹审批（如 tests/），也不做快照
        self._delete_exempt = _ListEditorCard(
            "删除豁免路径（这些目录内的删除不再弹审批，如 tests/）",
            "delete_exempt",
            "如 tests/ 或 D:/other_proj/logs",
            show_title=False,
        )
        self._delete_exempt_card = _expand(
            FIF.INFO if FIF else None,
            "删除豁免路径",
            "这些目录内的删除不再弹审批（如 tests/）",
            self._delete_exempt,
        )
        layout.addWidget(self._delete_exempt_card)

        self.backup_dir_card = PushSettingCard(
            "打开备份目录",
            FIF.FOLDER if FIF else None,
            "自动备份",
            f"修改前自动备份原文件（FileRecorder），上限 {self._cfg.get('backup_limit_mb')} MB",
        )
        self.backup_dir_card.clicked.connect(lambda: self._open_backup_dir())
        layout.addWidget(self.backup_dir_card)

        # ── 系统级工具卡 ──
        self.sys_switch = SwitchSettingCard(
            FIF.COMMAND_PROMPT if FIF else None,
            "系统级工具豁免",
            "wsl/wmic/sc/reg/schtasks 等绕过沙箱审批，请谨慎启用",
        )
        self.sys_switch.setChecked(bool(self._cfg.get("sys_tools_bypass")))
        self.sys_switch.checkedChanged.connect(self._on_sys_toggled)
        layout.addWidget(self.sys_switch)

        # ── 底部说明 ──
        note = (
            BodyLabel("说明：以上拦截为应用层检查，非 OS 级沙箱隔离。")
            if BodyLabel
            else QLabel("说明：以上拦截为应用层检查，非 OS 级沙箱隔离。")
        )
        layout.addWidget(note)

        layout.addStretch(1)

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

    def _open_backup_dir(self):
        from app.utils.utils import get_app_data_dir

        backup_dir = get_app_data_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        import subprocess
        import sys

        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(backup_dir)])
