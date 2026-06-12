# -*- coding: utf-8 -*-
"""
Gateway 通讯平台设置卡片

接入企业微信、钉钉、Telegram、Discord、飞书、Slack，
让 AI 能够通过这些平台与用户对话。

特性：
- 开关打开时自动连接，关闭时自动断开
- 已连接时按钮变成"断开"（红色）
- 连接中时显示"断开"（黄色）
- 未连接时显示"连接"（默认颜色）
"""
import threading

from loguru import logger
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
    QFormLayout, )
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    ExpandSettingCard,
    PrimaryPushButton,
    PushButton,
    SwitchButton,
    StrongBodyLabel,
    ToolButton,
    FluentIcon, IconWidget,
)
from qfluentwidgets import InfoBar, InfoBarPosition

from app.utils.design_tokens import Colors, ButtonStyles, SwitchStyles, Sizes, CardStyles, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.cards.floating.command_card import _ElidedLabel

# ═══════════════════════════════════════════════════════════
# 共用表单样式（白色标签 + 深色输入框）
# ═══════════════════════════════════════════════════════════

def get_label_style() -> str:
    """获取标签样式（响应主题）"""
    Colors.refresh()
    return f"""
color: {Colors.TEXT_PRIMARY};
font-weight: bold;
{get_font_family_css()}
{font_size_css(13)}
"""

def get_gateway_edit_style() -> str:
    """获取网关输入框样式（响应主题，复用统一编辑样式）"""
    return CardStyles.edit_card_style() + f"""
QLabel {{
    color: {Colors.TEXT_PRIMARY};
}}
"""

# 兼容旧引用
LABEL_STYLE = get_label_style()
GATEWAY_EDIT_STYLE = get_gateway_edit_style()




# ═══════════════════════════════════════════════════════════
# 平台定义
# ═══════════════════════════════════════════════════════════

PLATFORM_DEFS = {
    "wecom": {
        "name": "企业微信",
        "icon": "企业微信",
        "fields": [
            ("bot_id", "Bot ID", "", "企业微信机器人 BotID"),
            ("secret", "Secret", "password", "机器人密钥 Secret"),
            ("websocket_url", "WebSocket", "", "wss://openws.work.weixin.qq.com"),
        ],
        "hint": "💡 需要在企业微信管理后台创建 AI 机器人。",
    },
    "dingtalk": {
        "name": "钉钉",
        "icon": "钉钉",
        "fields": [
            ("client_id", "AppKey", "", "钉钉应用 AppKey"),
            ("client_secret", "AppSecret", "password", "钉钉应用 AppSecret"),
        ],
        "hint": "💡 需要在钉钉开放平台创建应用并启用 Stream Mode。",
    },
    "feishu": {
        "name": "飞书",
        "icon": "飞书",
        "fields": [
            ("app_id", "App ID", "", "飞书开放平台 App ID"),
            ("app_secret", "App Secret", "password", "飞书开放平台 App Secret"),
        ],
        "hint": "💡 需要在飞书开放平台创建企业自建应用，配置事件订阅（长连接模式）。",
    },
    "telegram": {
        "name": "Telegram",
        "icon": "Telegram",
        "fields": [
            ("token", "Bot Token", "password", "BotFather 获取的 Token"),
            ("require_mention", "@校验", "", "群聊需要 @才回复 (true/false)"),
        ],
        "hint": "💡 通过 @BotFather 创建机器人获取 Token。",
    },
    "discord": {
        "name": "Discord",
        "icon": "discord",
        "fields": [
            ("token", "Bot Token", "password", "Discord Developer Portal 获取"),
            ("require_mention", "@校验", "", "群聊需要 @才回复 (true/false)"),
        ],
        "hint": "💡 需要在 Discord Developer Portal 创建 Bot 并开启 Message Content Intent。",
    },
    "slack": {
        "name": "Slack",
        "icon": "slack",
        "fields": [
            ("bot_token", "Bot Token", "password", "Slack App Bot Token (xoxb-)"),
            ("app_token", "App Token", "password", "Slack App Token (xapp-)"),
        ],
        "hint": "💡 需要在 Slack API 创建 App 并启用 Socket Mode。",
    },
}


# ═══════════════════════════════════════════════════════════
# PlatformStatusRow — 平台状态行（优化版）
# ═══════════════════════════════════════════════════════════

class PlatformStatusRow(CardWidget):
    """平台状态行（优化版）"""

    editRequested = pyqtSignal(str)
    enabledChanged = pyqtSignal(str, bool)

    def __init__(self, platform: str, name: str, icon: QIcon, parent=None):
        super().__init__(parent)
        self._platform = platform
        self._name = name
        self._icon = icon
        self._is_connecting = False
        self._is_connected = False
        self._setup_ui()
        self._load_config()
        
        # 定时刷新状态
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_status_from_manager)
        self._refresh_timer.start(2000)  # 每2秒刷新一次

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        # 平台图标
        icon_label = IconWidget(self._icon)
        icon_label.setFixedSize(24, 24)
        layout.addWidget(icon_label)

        # 名称
        self.name_label = StrongBodyLabel(self._name)
        self.name_label.setFixedWidth(80)
        layout.addWidget(self.name_label)

        # 状态（使用 ElidedLabel 处理长错误信息）
        self.status_label = _ElidedLabel("未连接")
        self.status_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: 13px;")
        self.status_label.setToolTip("")
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

    def _resolve_enum(self):
        from app.gateway.base import Platform
        mapping = {
            "wecom": Platform.WECOM,
            "dingtalk": Platform.DINGTALK,
            "telegram": Platform.TELEGRAM,
            "discord": Platform.DISCORD,
            "feishu": Platform.FEISHU,
            "slack": Platform.SLACK,
        }
        return mapping.get(self._platform, Platform.WECOM)

    def _load_config(self):
        try:
            from app.gateway.config import get_gateway_config
            cfg = get_gateway_config().get_platform_config(self._resolve_enum())
            self.enable_switch.setChecked(cfg.enabled)
            self._refresh_status_from_manager()
        except Exception:
            pass

    def _on_enabled_changed(self, checked: bool):
        """开关变化时自动连接或断开"""
        try:
            from app.gateway.config import get_gateway_config
            get_gateway_config().set_platform_enabled(self._resolve_enum(), checked)
        except Exception as e:
            logger.warning(f"[PlatformStatusRow] Save enabled error: {e}")
        
        self.enabledChanged.emit(self._platform, checked)
        
        # 根据开关状态自动连接或断开
        if checked:
            self._do_connect()
        else:
            self._do_disconnect()
    
    def set_error(self, error_msg: str):
        """外部设置错误信息"""
        self._is_connecting = False
        self._is_connected = False
        self._update_status_safe(False, error_msg)

    def _on_edit(self):
        self.editRequested.emit(self._platform)



    def _do_connect(self):
        """执行连接"""
        if self._is_connecting:
            return
        
        self._is_connecting = True
        self._update_status_safe(False, None, connecting=True)  # 显示连接中
        platform_enum = self._resolve_enum()

        def _do():
            try:
                from app.gateway.manager import get_platform_manager
                manager = get_platform_manager()
                if not manager:
                    self._update_status_safe(False, "管理器未就绪")
                    self._is_connecting = False
                    return
                
                success = manager.start_platform(platform_enum)
                # 立即重置 _is_connecting（不等刷新回调），让后续操作能立即执行
                self._is_connecting = False
                # 等待一小段时间后刷新状态（UI 层面的状态刷新）
                QTimer.singleShot(2000, self._refresh_status_from_manager)
            except Exception as e:
                self._update_status_safe(False, str(e))
                self._is_connecting = False

        t = threading.Thread(target=_do, daemon=True)
        t.start()

    def _do_disconnect(self):
        """执行断开"""
        if self._is_connecting:
            return
        
        self._is_connecting = True
        platform_enum = self._resolve_enum()

        def _do():
            try:
                from app.gateway.manager import get_platform_manager
                manager = get_platform_manager()
                if manager:
                    manager.stop_platform(platform_enum)
                    # 立即重置 _is_connecting（不等刷新回调），让后续开关能立即触发连接
                    self._is_connecting = False
                    # 延迟刷新状态（UI 层面的状态刷新）
                    QTimer.singleShot(500, self._refresh_status_from_manager)
                else:
                    self._update_status_safe(False, None)
                    self._is_connecting = False
            except Exception as e:
                self._update_status_safe(False, str(e))
                self._is_connecting = False

        t = threading.Thread(target=_do, daemon=True)
        t.start()

    def _refresh_status_from_manager(self):
        """从管理器刷新状态"""
        try:
            from app.gateway.manager import get_platform_manager
            manager = get_platform_manager()
            if manager:
                status = manager.get_status()
                platform_status = status.get("platforms", {}).get(self._platform, {})
                connected = platform_status.get("connected", False)
                error = platform_status.get("error")
                
                # 重置连接状态
                self._is_connecting = False
                self._is_connected = connected
                self._update_status(connected, error)
            else:
                self._is_connecting = False
                self._update_status(False, "管理器未就绪")
        
        except Exception as e:
            self._is_connecting = False
            self._update_status(False, f"获取状态失败: {e}")

    def _set_status(self, connected: bool, error: str = None):
        """设置状态（在主线程）"""
        self._is_connected = connected
        self._is_connecting = False
        self._update_status(connected, error)

    def _update_status_safe(self, connected: bool, error: str = None, connecting: bool = False):
        """线程安全的 UI 更新"""
        QTimer.singleShot(0, lambda: self._update_status(connected, error, connecting))

    def _update_status(self, connected: bool, error: str = None, connecting: bool = False):
        """更新状态显示"""
        if connected:
            self.status_label.setText("已连接 ✓")
            self.status_label.setStyleSheet("color: #52c41a; font-size: 13px;")
            self.status_label.setToolTip("")
        elif connecting:
            self.status_label.setText("连接中...")
            self.status_label.setStyleSheet("color: #faad14; font-size: 13px;")
            self.status_label.setToolTip("")
        elif error:
            self.status_label.setText(str(error))
            self.status_label.setStyleSheet("color: #ff4d4f; font-size: 13px;")
            self.status_label.setToolTip(error)
        else:
            self.status_label.setText("未连接")
            self.status_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: 13px;")
            self.status_label.setToolTip("")



    def update_status(self, connected: bool, error: str = None):
        """外部更新状态（兼容旧接口）"""
        self._set_status(connected, error)

    def set_enabled(self, enabled: bool):
        self.enable_switch.setChecked(enabled)




# ═══════════════════════════════════════════════════════════
# PlatformEditCard — 平台配置编辑表单
# ═══════════════════════════════════════════════════════════

class PlatformEditCard(QWidget):
    """平台配置编辑卡片（通用）"""

    saved = pyqtSignal(str, dict)  # platform, config
    closed = pyqtSignal()

    def __init__(self, platform: str, parent=None):
        super().__init__(parent)
        self._platform = platform
        self._def = PLATFORM_DEFS.get(platform, {})
        self._inputs = {}
        self._load_config()
        self._init_ui()

    def _resolve_enum(self, platform_name: str):
        """将平台名转为 Platform 枚举"""
        from app.gateway.base import Platform
        mapping = {
            "wecom": Platform.WECOM,
            "dingtalk": Platform.DINGTALK,
            "telegram": Platform.TELEGRAM,
            "discord": Platform.DISCORD,
            "feishu": Platform.FEISHU,
            "slack": Platform.SLACK,
        }
        return mapping.get(platform_name, Platform.WECOM)

    def _load_config(self):
        """加载配置"""
        try:
            from app.gateway.config import get_gateway_config
            self._config = get_gateway_config().get_platform_config(self._resolve_enum(self._platform))
        except Exception as e:
            self._config = None

    def _init_ui(self):
        self.setStyleSheet(GATEWAY_EDIT_STYLE)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 4, 8, 4)
        main_layout.setSpacing(12)

        # 标题
        name = self._def.get("name", self._platform)
        title = StrongBodyLabel(f"{name} 配置")
        title.setStyleSheet(LABEL_STYLE)
        main_layout.addWidget(title)

        # 表单
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)

        for key, label, echo_mode, placeholder in self._def.get("fields", []):
            input_widget = QLineEdit()
            input_widget.setPlaceholderText(placeholder)
            if echo_mode == "password":
                input_widget.setEchoMode(QLineEdit.Password)
            # 填充现有值
            current_val = self._get_config_value(key)
            if current_val is not None:
                input_widget.setText(str(current_val))

            # 标签白色
            lbl = BodyLabel(label)
            lbl.setStyleSheet(LABEL_STYLE)

            form.addRow(lbl, input_widget)
            self._inputs[key] = input_widget

        # 提示
        hint_text = self._def.get("hint", "")
        if hint_text:
            hint = BodyLabel(hint_text)
            hint.setStyleSheet(
                f"color: rgba(255,255,255,0.5); padding: 8px 0; {get_font_family_css()} font-size: 11px;")
            form.addRow("", hint)

        main_layout.addLayout(form)

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

    def _get_config_value(self, key: str):
        """从配置对象读取值"""
        if not self._config:
            return None
        # 尝试直接属性
        if hasattr(self._config, key):
            return getattr(self._config, key)
        # 尝试 extra 字典
        if hasattr(self._config, "extra") and self._config.extra:
            return self._config.extra.get(key)
        return None

    def _on_save(self):
        """保存配置"""
        try:
            from app.gateway.config import get_gateway_config
            from app.gateway.base import Platform, PlatformConfig

            config_helper = get_gateway_config()
            platform_enum = self._resolve_enum(self._platform)
            existing = config_helper.get_platform_config(platform_enum)

            # 提取字段值
            def _val(key):
                if key in self._inputs:
                    return self._inputs[key].text().strip()
                return getattr(existing, key, None)

            # 构建 PlatformConfig
            if self._platform == "wecom":
                config_obj = PlatformConfig(
                    enabled=existing.enabled if existing else False,
                    platform=Platform.WECOM,
                    bot_id=_val("bot_id"),
                    secret=_val("secret"),
                    websocket_url=_val("websocket_url") or "wss://openws.work.weixin.qq.com",
                )
            elif self._platform == "dingtalk":
                config_obj = PlatformConfig(
                    enabled=existing.enabled if existing else False,
                    platform=Platform.DINGTALK,
                    client_id=_val("client_id"),
                    client_secret=_val("client_secret"),
                )
            elif self._platform == "telegram":
                config_obj = PlatformConfig(
                    enabled=existing.enabled if existing else False,
                    platform=Platform.TELEGRAM,
                    token=_val("token"),
                    extra={"require_mention": _val("require_mention") or "true"},
                )
            elif self._platform == "discord":
                config_obj = PlatformConfig(
                    enabled=existing.enabled if existing else False,
                    platform=Platform.DISCORD,
                    token=_val("token"),
                    extra={"require_mention": _val("require_mention") or "true"},
                )
            elif self._platform == "feishu":
                config_obj = PlatformConfig(
                    enabled=existing.enabled if existing else False,
                    platform=Platform.FEISHU,
                    extra={"app_id": _val("app_id"), "app_secret": _val("app_secret")},
                )
            elif self._platform == "slack":
                config_obj = PlatformConfig(
                    enabled=existing.enabled if existing else False,
                    platform=Platform.SLACK,
                    extra={"bot_token": _val("bot_token"), "app_token": _val("app_token")},
                )
            else:
                config_obj = PlatformConfig(enabled=False, platform=platform_enum)

            config_helper.set_platform_config(platform_enum, config_obj)

            name = PLATFORM_DEFS.get(self._platform, {}).get("name", self._platform)
            InfoBar.success(
                title="保存成功",
                content=f"{name} 配置已保存",
                parent=self.window(),
                duration=2000,
                position=InfoBarPosition.BOTTOM,
            )

            self.saved.emit(self._platform, {})
            self.closed.emit()

        except Exception as e:
            InfoBar.error(title="保存失败", content=str(e), parent=self.window())


# ═══════════════════════════════════════════════════════════
# GatewaySettingCard — 主卡片
# ═══════════════════════════════════════════════════════════

class GatewaySettingCard(ExpandSettingCard):
    """
    Gateway 通讯平台设置卡片

    管理企业微信、钉钉、Telegram、Discord、飞书、Slack 的连接配置。
    """

    gatewayToggled = pyqtSignal()  # 平台开关变更信号（用于多窗口同步）

    def __init__(self, icon, title: str, content: str = None, parent=None, home=None):
        super().__init__(icon, title, content, parent)
        self._home = home
        self._current_edit_card: PlatformEditCard = None
        self._current_platform: str = None
        self._rows: dict = {}

        self._setup_ui()
        self._refresh()

    def _setup_ui(self):
        self.viewLayout.setSpacing(2)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)
        self.view.setStyleSheet("background-color: transparent;")

        # 为每个平台创建状态行
        for key, info in PLATFORM_DEFS.items():
            row = PlatformStatusRow(key, info["name"], get_icon(info["icon"]), self.view)
            row.editRequested.connect(self._show_edit_card)
            row.enabledChanged.connect(self._on_platform_enabled_changed)
            self.viewLayout.addWidget(row)
            self._rows[key] = row

        # 编辑卡片容器
        self.edit_container = QWidget(self.view)
        self.edit_container.setStyleSheet("background: rgba(30, 30, 30, 100); border-radius: 8px;")
        self.edit_layout = QVBoxLayout(self.edit_container)
        self.edit_layout.setContentsMargins(8, 8, 8, 8)
        self.edit_container.hide()
        self.viewLayout.addWidget(self.edit_container)

    def _show_edit_card(self, platform: str):
        """显示编辑卡片"""
        # 隐藏所有状态行
        for row in self._rows.values():
            row.hide()

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
        """隐藏编辑卡片，恢复状态行"""
        self.edit_container.hide()
        for row in self._rows.values():
            row.show()
        self._current_edit_card = None
        self._current_platform = None
        self._adjustViewSize()

    def _on_edit_saved(self, platform: str, config: dict):
        """编辑保存后刷新"""
        self._refresh()
        self.gatewayToggled.emit()

    def _on_platform_enabled_changed(self, platform: str, enabled: bool):
        """平台启用状态改变"""
        self._refresh()
        self.gatewayToggled.emit()

    def _refresh(self):
        """刷新状态"""
        try:
            from app.gateway.config import get_gateway_config
            from app.gateway.base import Platform

            config_helper = get_gateway_config()
            mapping = {
                "wecom": Platform.WECOM,
                "dingtalk": Platform.DINGTALK,
                "telegram": Platform.TELEGRAM,
                "discord": Platform.DISCORD,
                "feishu": Platform.FEISHU,
                "slack": Platform.SLACK,
            }
            for key, enum in mapping.items():
                row = self._rows.get(key)
                if row:
                    try:
                        pc = config_helper.get_platform_config(enum)
                        row.set_enabled(pc.enabled)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[GatewaySettingCard] Refresh error: {e}")