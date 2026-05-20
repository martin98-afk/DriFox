# -*- coding: utf-8 -*-
"""
Gateway 通讯平台设置卡片

接入企业微信/钉钉，让 AI 能够通过这些平台与用户对话。
参考 MCPListSettingCard 模式：展开配置界面。
"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
    QFormLayout,
    QStackedWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    ExpandSettingCard,
    PrimaryPushButton,
    PushButton,
    SwitchButton,
    StrongBodyLabel,
    ToolButton,
    FluentIcon,
)
from qfluentwidgets import InfoBar, InfoBarPosition

from app.utils.design_tokens import Colors, ButtonStyles, SwitchStyles, scale_font_size, Sizes
from app.utils.utils import get_icon, get_font_family_css


# ═══════════════════════════════════════════════════════════
# 共用表单样式
# ═══════════════════════════════════════════════════════════

GATEWAY_EDIT_STYLE = f"""
QWidget {{
    background: transparent;
}}
QLineEdit {{
    background-color: rgba(61, 61, 61, 180);
    color: #ffffff;
    border: 1px solid rgba(85, 85, 85, 150);
    border-radius: 4px;
    padding: 6px 10px;
    {get_font_family_css()}
    font-size: 13px;
}}
QLineEdit:focus {{
    border-color: rgba(0, 120, 212, 200);
}}
QLineEdit::placeholder {{
    color: rgba(255, 255, 255, 0.35);
}}
"""


# ═══════════════════════════════════════════════════════════
# PlatformEditCard — 平台配置编辑表单
# ═══════════════════════════════════════════════════════════

class PlatformEditCard(QWidget):
    """平台配置编辑卡片"""
    
    saved = pyqtSignal(str, dict)  # platform, config
    closed = pyqtSignal()
    
    def __init__(self, platform: str, parent=None):
        super().__init__(parent)
        self._platform = platform
        self._load_config()
        self._init_ui()
    
    def _load_config(self):
        """加载配置"""
        try:
            from app.gateway.config import get_gateway_config
            from app.gateway.base import Platform
            
            config = get_gateway_config()
            platform_enum = Platform.WECOM if self._platform == "wecom" else Platform.DINGTALK
            self._config = config.get_platform_config(platform_enum)
        except Exception as e:
            print(f"[PlatformEditCard] Load config error: {e}")
            self._config = None
    
    def _init_ui(self):
        self.setStyleSheet(GATEWAY_EDIT_STYLE)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 4, 8, 4)
        main_layout.setSpacing(12)
        
        # 标题
        title = StrongBodyLabel("平台配置")
        title.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-weight: bold;")
        main_layout.addWidget(title)
        
        if self._platform == "wecom":
            self._setup_wecom_form(main_layout)
        else:
            self._setup_dingtalk_form(main_layout)
        
        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.save_btn = PrimaryPushButton("保存", self)
        self.save_btn.setFixedWidth(80)
        self.save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(self.save_btn)
        
        self.cancel_btn = PushButton("取消", self)
        self.cancel_btn.setFixedWidth(80)
        self.cancel_btn.clicked.connect(self.closed.emit)
        btn_layout.addWidget(self.cancel_btn)
        
        main_layout.addLayout(btn_layout)
    
    def _setup_wecom_form(self, parent_layout):
        """企业微信表单"""
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        
        # Bot ID
        self.bot_id_input = QLineEdit()
        self.bot_id_input.setPlaceholderText("企业微信机器人 BotID")
        self.bot_id_input.setText(self._config.bot_id if self._config else "")
        row, label = self._make_row("Bot ID:", self.bot_id_input)
        form.addRow(label, self.bot_id_input)
        
        # Secret
        self.secret_input = QLineEdit()
        self.secret_input.setPlaceholderText("机器人密钥 Secret")
        self.secret_input.setEchoMode(QLineEdit.Password)
        self.secret_input.setText(self._config.secret if self._config else "")
        form.addRow("Secret:", self.secret_input)
        
        # WebSocket URL
        self.ws_url_input = QLineEdit()
        self.ws_url_input.setPlaceholderText("wss://openws.work.weixin.qq.com")
        self.ws_url_input.setText(
            self._config.websocket_url if self._config and self._config.websocket_url 
            else "wss://openws.work.weixin.qq.com"
        )
        form.addRow("WebSocket:", self.ws_url_input)
        
        # 提示
        hint = BodyLabel(
            "💡 需要先在企业微信管理后台创建 AI 机器人，\n"
            "   并部署 AI Bot WebSocket Gateway。"
        )
        hint.setStyleSheet(f"color: {Colors.TEXT_MUTED}; padding: 8px 0; {get_font_family_css()} font-size: 11px;")
        form.addRow("", hint)
        
        parent_layout.addLayout(form)
    
    def _setup_dingtalk_form(self, parent_layout):
        """钉钉表单"""
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        
        # AppKey
        self.appkey_input = QLineEdit()
        self.appkey_input.setPlaceholderText("钉钉应用 AppKey")
        self.appkey_input.setText(self._config.client_id if self._config else "")
        form.addRow("AppKey:", self.appkey_input)
        
        # AppSecret
        self.appsecret_input = QLineEdit()
        self.appsecret_input.setPlaceholderText("钉钉应用 AppSecret")
        self.appsecret_input.setEchoMode(QLineEdit.Password)
        self.appsecret_input.setText(self._config.client_secret if self._config else "")
        form.addRow("AppSecret:", self.appsecret_input)
        
        # 提示
        hint = BodyLabel(
            "💡 需要在钉钉开放平台创建应用，\n"
            "   并启用 Stream Mode。"
        )
        hint.setStyleSheet(f"color: {Colors.TEXT_MUTED}; padding: 8px 0; {get_font_family_css()} font-size: 11px;")
        form.addRow("", hint)
        
        parent_layout.addLayout(form)
    
    def _make_row(self, label_text: str, widget: QWidget) -> tuple:
        """构造一行"""
        row = QHBoxLayout()
        row.setSpacing(8)
        label = BodyLabel(label_text)
        label.setFixedWidth(70)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(label)
        row.addWidget(widget, 1)
        return row, label
    
    def _on_save(self):
        """保存配置"""
        try:
            from app.gateway.config import get_gateway_config
            from app.gateway.base import Platform, PlatformConfig
            
            config = get_gateway_config()
            
            if self._platform == "wecom":
                platform_config = PlatformConfig(
                    enabled=self._config.enabled if self._config else False,
                    platform=Platform.WECOM,
                    bot_id=self.bot_id_input.text().strip(),
                    secret=self.secret_input.text().strip(),
                    websocket_url=self.ws_url_input.text().strip() or "wss://openws.work.weixin.qq.com",
                )
            else:
                platform_config = PlatformConfig(
                    enabled=self._config.enabled if self._config else False,
                    platform=Platform.DINGTALK,
                    client_id=self.appkey_input.text().strip(),
                    client_secret=self.appsecret_input.text().strip(),
                )
            
            config.set_platform_config(
                Platform.WECOM if self._platform == "wecom" else Platform.DINGTALK,
                platform_config
            )
            
            InfoBar.success(
                title="保存成功",
                content=f"{'企业微信' if self._platform == 'wecom' else '钉钉'} 配置已保存",
                parent=self.window(),
                duration=2000,
                position=InfoBarPosition.BOTTOM,
            )
            
            self.saved.emit(self._platform, platform_config.__dict__)
            self.closed.emit()
            
        except Exception as e:
            InfoBar.error(
                title="保存失败",
                content=str(e),
                parent=self.window()
            )


# ═══════════════════════════════════════════════════════════
# PlatformStatusRow — 平台状态行
# ═══════════════════════════════════════════════════════════

class PlatformStatusRow(CardWidget):
    """平台状态行"""
    
    editRequested = pyqtSignal(str)  # platform
    enabledChanged = pyqtSignal(str, bool)
    
    def __init__(self, platform: str, name: str, icon: str, parent=None):
        super().__init__(parent)
        self._platform = platform
        self._name = name
        self._icon = icon
        self._setup_ui()
        self._load_config()
    
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        
        # 平台图标
        icon_label = QLabel(self._icon)
        icon_label.setFixedWidth(30)
        layout.addWidget(icon_label)
        
        # 名称
        self.name_label = StrongBodyLabel(self._name)
        self.name_label.setFixedWidth(80)
        layout.addWidget(self.name_label)
        
        # 状态
        self.status_label = BodyLabel("未连接")
        self.status_label.setStyleSheet(f"color: {Colors.TEXT_MUTED};")
        layout.addWidget(self.status_label, 1)
        
        # 开关
        self.enable_switch = SwitchButton()
        SwitchStyles.configure(self.enable_switch)
        self.enable_switch.setOffText("")
        self.enable_switch.setOnText("")
        self.enable_switch.checkedChanged.connect(self._on_enabled_changed)
        layout.addWidget(self.enable_switch)
        
        # 编辑按钮
        self.edit_btn = ToolButton(FluentIcon.EDIT)
        self.edit_btn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        self.edit_btn.setStyleSheet(ButtonStyles.tool_button())
        self.edit_btn.clicked.connect(self._on_edit)
        layout.addWidget(self.edit_btn)
        
        # 连接按钮
        self.connect_btn = PushButton("连接")
        self.connect_btn.setFixedWidth(60)
        self.connect_btn.clicked.connect(self._on_connect)
        layout.addWidget(self.connect_btn)
    
    def _load_config(self):
        """加载配置"""
        try:
            from app.gateway.config import get_gateway_config
            from app.gateway.base import Platform
            
            config = get_gateway_config()
            platform_enum = Platform.WECOM if self._platform == "wecom" else Platform.DINGTALK
            p_config = config.get_platform_config(platform_enum)
            
            self.enable_switch.setChecked(p_config.enabled)
            self._has_config = bool(p_config.bot_id or p_config.client_id)
            
        except Exception as e:
            print(f"[PlatformStatusRow] Load config error: {e}")
            self._has_config = False
    
    def _on_enabled_changed(self, checked: bool):
        self._save_enabled(checked)
        self.enabledChanged.emit(self._platform, checked)
    
    def _on_edit(self):
        self.editRequested.emit(self._platform)
    
    def _on_connect(self):
        """连接平台（在后台线程运行，不阻塞 UI）"""
        from app.gateway.base import Platform
        import threading

        platform_enum = Platform.WECOM if self._platform == "wecom" else Platform.DINGTALK

        def _do_connect():
            try:
                from app.gateway.manager import get_platform_manager
                manager = get_platform_manager()
                if not manager:
                    self.update_status(connected=False, error="管理器未就绪")
                    return
                success = manager.start_platform(platform_enum)
                if success:
                    self.update_status(connected=True)
                else:
                    error = getattr(manager, "_last_error", None) or "连接失败"
                    self.update_status(connected=False, error=str(error)[:30])
            except Exception as e:
                self.update_status(connected=False, error=str(e)[:30])

        t = threading.Thread(target=_do_connect, daemon=True)
        t.start()
        self.update_status(connected=False, error="连接中...")
    
    def _save_enabled(self, enabled: bool):
        try:
            from app.gateway.config import get_gateway_config
            from app.gateway.base import Platform, PlatformConfig
            
            config = get_gateway_config()
            platform_enum = Platform.WECOM if self._platform == "wecom" else Platform.DINGTALK
            
            # 读取现有配置
            p_config = config.get_platform_config(platform_enum)
            
            # 更新启用状态
            if self._platform == "wecom":
                new_config = PlatformConfig(
                    enabled=enabled,
                    platform=Platform.WECOM,
                    bot_id=p_config.bot_id,
                    secret=p_config.secret,
                    websocket_url=p_config.websocket_url,
                )
            else:
                new_config = PlatformConfig(
                    enabled=enabled,
                    platform=Platform.DINGTALK,
                    client_id=p_config.client_id,
                    client_secret=p_config.client_secret,
                )
            
            config.set_platform_config(platform_enum, new_config)
            
        except Exception as e:
            print(f"[PlatformStatusRow] Save enabled error: {e}")
    
    def update_status(self, connected: bool, error: str = None):
        if connected:
            self.status_label.setText("已连接 ✓")
            self.status_label.setStyleSheet("color: #52c41a;")
        elif error:
            self.status_label.setText(f"错误")
            self.status_label.setStyleSheet("color: #ff4d4f;")
            self.status_label.setToolTip(error)
        else:
            self.status_label.setText("未连接")
            self.status_label.setStyleSheet(f"color: {Colors.TEXT_MUTED};")
            self.status_label.setToolTip("")
    
    def set_enabled(self, enabled: bool):
        self.enable_switch.setChecked(enabled)


# ═══════════════════════════════════════════════════════════
# GatewaySettingCard — 主卡片
# ═══════════════════════════════════════════════════════════

class GatewaySettingCard(ExpandSettingCard):
    """
    Gateway 通讯平台设置卡片
    
    管理企业微信和钉钉的连接配置。
    """
    
    def __init__(self, icon, title: str, content: str = None, parent=None, home=None):
        super().__init__(icon, title, content, parent)
        self._home = home
        
        # 编辑卡片引用
        self._current_edit_card: PlatformEditCard = None
        self._current_platform: str = None
        
        self._setup_ui()
        self._refresh()
    
    def _setup_ui(self):
        self.viewLayout.setSpacing(2)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)
        self.view.setStyleSheet("background-color: transparent;")
        
        # 企业微信行
        self.wecom_row = PlatformStatusRow("wecom", "企业微信", "💼", self.view)
        self.wecom_row.editRequested.connect(self._show_edit_card)
        self.wecom_row.enabledChanged.connect(self._on_platform_enabled_changed)
        self.viewLayout.addWidget(self.wecom_row)
        
        # 钉钉行
        self.dingtalk_row = PlatformStatusRow("dingtalk", "钉钉", "🔔", self.view)
        self.dingtalk_row.editRequested.connect(self._show_edit_card)
        self.dingtalk_row.enabledChanged.connect(self._on_platform_enabled_changed)
        self.viewLayout.addWidget(self.dingtalk_row)
        
        # 编辑卡片容器（放在 view 中）
        self.edit_container = QWidget(self.view)
        self.edit_container.setStyleSheet("background: rgba(30, 30, 30, 100); border-radius: 8px;")
        self.edit_layout = QVBoxLayout(self.edit_container)
        self.edit_layout.setContentsMargins(8, 8, 8, 8)
        self.edit_container.hide()
        self.viewLayout.addWidget(self.edit_container)
    
    def _show_edit_card(self, platform: str):
        """显示编辑卡片"""
        # 隐藏状态行
        self.wecom_row.hide()
        self.dingtalk_row.hide()
        
        # 清理旧的编辑卡片
        while self.edit_layout.count():
            item = self.edit_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        # 创建新的编辑卡片
        self._current_platform = platform
        self._current_edit_card = PlatformEditCard(platform, self.edit_container)
        self._current_edit_card.saved.connect(self._on_edit_saved)
        self._current_edit_card.closed.connect(self._hide_edit_card)
        self.edit_layout.addWidget(self._current_edit_card)
        
        self.edit_container.show()
        self._adjustViewSize()
    
    def _hide_edit_card(self):
        """隐藏编辑卡片"""
        self.edit_container.hide()
        self.wecom_row.show()
        self.dingtalk_row.show()
        self._current_edit_card = None
        self._current_platform = None
        self._adjustViewSize()
    
    def _on_edit_saved(self, platform: str, config: dict):
        """编辑保存后刷新"""
        self._refresh()
    
    def _on_platform_enabled_changed(self, platform: str, enabled: bool):
        """平台启用状态改变"""
        self._refresh()
    
    def _refresh(self):
        """刷新状态"""
        try:
            from app.gateway.config import get_gateway_config
            from app.gateway.base import Platform
            
            config = get_gateway_config()
            
            # 企业微信
            wecom_config = config.get_platform_config(Platform.WECOM)
            self.wecom_row.set_enabled(wecom_config.enabled)
            
            # 钉钉
            dingtalk_config = config.get_platform_config(Platform.DINGTALK)
            self.dingtalk_row.set_enabled(dingtalk_config.enabled)
            
            # 从管理器获取状态
            self._update_status_from_manager()
            
        except Exception as e:
            print(f"[GatewaySettingCard] Refresh error: {e}")
    
    def _update_status_from_manager(self):
        """从管理器获取并更新状态"""
        try:
            from app.gateway.manager import get_platform_manager
            from app.gateway.base import Platform
            
            manager = get_platform_manager()
            if manager:
                status = manager.get_status()
                platforms = status.get("platforms", {})
                
                # 企业微信状态
                wecom = platforms.get(Platform.WECOM.value, {})
                self.wecom_row.update_status(
                    connected=wecom.get("connected", False),
                    error=wecom.get("error")
                )
                
                # 钉钉状态
                dingtalk = platforms.get(Platform.DINGTALK.value, {})
                self.dingtalk_row.update_status(
                    connected=dingtalk.get("connected", False),
                    error=dingtalk.get("error")
                )
                
        except Exception as e:
            print(f"[GatewaySettingCard] Update status error: {e}")
    
    def update_status(self, status: dict):
        """更新所有平台状态"""
        platforms = status.get("platforms", {})
        
        wecom = platforms.get("wecom", {})
        self.wecom_row.update_status(
            connected=wecom.get("connected", False),
            error=wecom.get("error")
        )
        
        dingtalk = platforms.get("dingtalk", {})
        self.dingtalk_row.update_status(
            connected=dingtalk.get("connected", False),
            error=dingtalk.get("error")
        )
