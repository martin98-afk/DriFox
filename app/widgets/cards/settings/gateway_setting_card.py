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
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    ExpandSettingCard,
    FluentIcon,
    IconWidget,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SwitchButton,
    ToolButton,
)

from app.plugins.contracts.gateway_platform import GatewayPlatformDef
from app.utils.design_tokens import (
    ButtonStyles,
    CardStyles,
    Colors,
    Sizes,
    SwitchStyles,
    font_size_css,
    scale_icon_size,
)
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.elided_label import _ElidedLabel

# ═══════════════════════════════════════════════════════════
# 共用表单样式（白色标签 + 深色输入框）
# ═══════════════════════════════════════════════════════════


def get_label_style() -> str:
    """获取标签样式（每次调用刷新主题）"""
    Colors.refresh()
    return f"""
color: {Colors.TEXT_PRIMARY};
font-weight: bold;
{get_font_family_css()}
{font_size_css(13)}
"""


def get_gateway_edit_style() -> str:
    """获取网关输入框样式（每次调用刷新主题）"""
    Colors.refresh()
    return (
        CardStyles.edit_card_style()
        + f"""
QLabel {{
    color: {Colors.TEXT_PRIMARY};
}}
"""
    )


# ═══════════════════════════════════════════════════════════
# 平台定义 — registry 驱动（E2 Task 6）
# ═══════════════════════════════════════════════════════════

# def.fields 契约尚未提供表单字段描述（task 6 改造点不变契约字段）：
# 内置六平台的 fields 元组硬编码到 _FALLBACK_FIELDS，第三方平台/未来插件
# 若在 def 上提供 config_schema 字段则优先采用，否则空表（仅开关行可编辑
# 启用开关，凭证字段需插件自身提供表单）。模块 import 时 gateway loader
# 已预热，registry 单例 list_platforms() 已可见。
_FALLBACK_FIELDS = {
    "wecom": [
        ("bot_id", "Bot ID", "", "企业微信机器人 BotID"),
        ("secret", "Secret", "password", "机器人密钥 Secret"),
        ("websocket_url", "WebSocket", "", "wss://openws.work.weixin.qq.com"),
    ],
    "dingtalk": [
        ("client_id", "AppKey", "", "钉钉应用 AppKey"),
        ("client_secret", "AppSecret", "password", "钉钉应用 AppSecret"),
    ],
    "feishu": [
        ("app_id", "App ID", "", "飞书开放平台 App ID"),
        ("app_secret", "App Secret", "password", "飞书开放平台 App Secret"),
    ],
    "telegram": [
        ("token", "Bot Token", "password", "BotFather 获取的 Token"),
        ("require_mention", "@校验", "", "群聊需要 @才回复 (true/false)"),
    ],
    "discord": [
        ("token", "Bot Token", "password", "Discord Developer Portal 获取"),
        ("require_mention", "@校验", "", "群聊需要 @才回复 (true/false)"),
    ],
    "slack": [
        ("bot_token", "Bot Token", "password", "Slack App Bot Token (xoxb-)"),
        ("app_token", "App Token", "password", "Slack App Token (xapp-)"),
    ],
}


def _build_platform_defs_from_registry():
    """遍历 GatewayPlatformRegistry 构造平台元数据 dict。

    返回：{platform_id: {name, icon, fields, hint}} —— 字段名沿用旧
    PLATFORM_DEFS 形态以保持模块内部消费点不变。
    """
    from app.plugins.registries.gateway_platform_registry import (
        GatewayPlatformRegistry,
    )

    defs: dict = {}
    for d in GatewayPlatformRegistry.get_instance().list_platforms():
        # icon_hint 回退：内置六平台 def 暂未声明 icon_hint；当前 get_icon 仍
        # 接受平台名作为资源键（"Telegram" / "企业微信" 等），统一约定：
        # def.icon_hint 非空直接用，否则回退到平台 display_name（get_icon
        # 资源查找沿用旧字符串键）。
        icon = d.icon_hint or d.display_name
        # fields：优先 def 上声明的 config_schema；否则内置平台走 _FALLBACK_FIELDS；
        # 第三方平台无 fallback → 空表（仅启用开关可编辑）。
        fields = _def_fields_from_schema(d) or _FALLBACK_FIELDS.get(d.platform_id, [])
        defs[d.platform_id] = {
            "name": d.display_name,
            "icon": icon,
            "fields": fields,
            "hint": _def_hint(d),
        }
    return defs


def _def_fields_from_schema(d: "GatewayPlatformDef") -> list:
    """从 def 上声明的 config_schema（若提供）解析表单字段元组。

    形态：(key, label, echo_mode, placeholder)。当前 GatewayPlatformDef 契约
    未声明该属性，本函数保留扩展点：未来 def 携带 config_schema 时按
    PluginConfigField 转表单列（password → echo_mode='password'）。
    """
    schema = getattr(d, "config_schema", None)
    if not schema:
        return []
    out = []
    for f in getattr(schema, "fields", []):
        ftype = getattr(f, "type", "text")
        echo = "password" if ftype == "password" else ""
        out.append((f.key, f.label, echo, getattr(f, "placeholder", "")))
    return out


def _def_hint(d: "GatewayPlatformDef") -> str:
    """平台提示文本（未来 def.description 接入时优先）。"""
    desc = getattr(d, "description", "") or ""
    return f"💡 {desc}" if desc else ""


def _save_platform_values(platform_id: str, values: dict, old_config) -> object:
    """表单值 → def.build_config_values → GatewayConfigHelper.set_platform_config。

    返回：写入后的 PlatformConfig；def 缺失 build_config_values 或注册表为空
    时返回 None（UI 据此报「该平台不支持配置编辑」）。
    """
    from app.gateway.config import get_gateway_config

    from app.plugins.registries.gateway_platform_registry import (
        GatewayPlatformRegistry,
    )

    d = GatewayPlatformRegistry.get_instance().get(platform_id)
    if d is None or d.build_config_values is None:
        return None
    config_obj = d.build_config_values(values, old_config)
    if config_obj is None:
        return None
    get_gateway_config().set_platform_config(platform_id, config_obj)
    return config_obj


def _truthy_str(v) -> bool:
    """文本字段 truthy 判定（UI 全 QLineEdit，bool 字段以 'true'/'false' 文本表达）。"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# 模块级平台 def 缓存（registry 已预热）。消费点（GatewaySettingCard /
# PlatformEditCard）继续以 PLATFORM_DEFS 名字引用即可。
PLATFORM_DEFS = _build_platform_defs_from_registry()


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
        self._platform_icon = IconWidget(self._icon)
        self._platform_icon.setFixedSize(scale_icon_size(24), scale_icon_size(24))
        layout.addWidget(self._platform_icon)

        # 名称
        self.name_label = StrongBodyLabel(self._name)
        self.name_label.setFixedWidth(80)
        layout.addWidget(self.name_label)

        # 状态（使用 ElidedLabel 处理长错误信息）
        self._last_status_state = None  # 记录上次状态，避免冗余 setStyleSheet
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
        """平台 id 直传：str-mixin 后 Platform(str) 即可构造，第三方 id 不在
        枚举闭集时 manager / config 已兼容 str 入参（_platform_key 统一归一化）。"""
        return self._platform

    def _load_config(self):
        try:
            from app.gateway.config import get_gateway_config

            cfg = get_gateway_config().get_platform_config(self._resolve_enum())
            # 加载存档开关状态时屏蔽信号：避免被误判为「用户拨动开关」触发自动连接。
            # 复制窗口时新 PlatformStatusRow 的默认状态(False)与存档(True)不一致，
            # setChecked 会 emit checkedChanged → _on_enabled_changed → _do_connect，
            # 进而调用 manager.start_platform(Platform.DINGTALK)。
            # 当 dingtalk_stream 依赖未安装、_adapters[DINGTALK] 缺失时，
            # 会在 _start_platform_async:357 反复打 ERROR("No adapter for dingtalk")。
            # blockSignals 是 Qt 加载配置的标准做法，setChecked 后立即恢复即可。
            self.enable_switch.blockSignals(True)
            try:
                self.enable_switch.setChecked(cfg.enabled)
            finally:
                self.enable_switch.blockSignals(False)
            self._refresh_status_from_manager()
        except Exception:
            # 异常兜底：确保信号不会因外部异常被永久屏蔽
            try:
                self.enable_switch.blockSignals(False)
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
        """更新状态显示 — 跟踪状态避免冗余 setStyleSheet"""
        # 确定当前状态标识
        if connected:
            new_state = "connected"
            text = "已连接 ✓"
            color = "#52c41a"
            tip = ""
        elif connecting:
            new_state = "connecting"
            text = "连接中..."
            color = "#faad14"
            tip = ""
        elif error:
            new_state = "error"
            text = str(error)
            color = "#ff4d4f"
            tip = error
        else:
            new_state = "disconnected"
            text = "未连接"
            color = Colors.TEXT_MUTED
            tip = ""

        # 状态未变：只更新文字（setStyleSheet 不碰）
        if new_state == self._last_status_state:
            self.status_label.setText(text)
            if tip:
                self.status_label.setToolTip(tip)
            return

        self._last_status_state = new_state
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 13px;")
        self.status_label.setToolTip(tip)

    def update_status(self, connected: bool, error: str = None):
        """外部更新状态（兼容旧接口）"""
        self._set_status(connected, error)

    def set_enabled(self, enabled: bool):
        self.enable_switch.setChecked(enabled)

    def refresh_style(self):
        """刷新样式：图标缩放 + 标签颜色（主题切换时调用）"""
        if hasattr(self, "_platform_icon") and self._platform_icon is not None:
            s = scale_icon_size(24)
            self._platform_icon.setFixedSize(s, s)
        # 强制下次 _update_status 重新 setStyleSheet（颜色可能已变）
        self._last_status_state = None


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
        self.refresh_style()

    def _load_config(self):
        """加载配置（平台 id 直传，str-mixin 已兼容第三方 id）"""
        try:
            from app.gateway.config import get_gateway_config

            self._config = get_gateway_config().get_platform_config(self._platform)
        except Exception:
            self._config = None

    def refresh_style(self):
        """主题切换时刷新输入框和标签颜色"""
        Colors.refresh()
        self.setStyleSheet(get_gateway_edit_style())
        label_style = get_label_style()
        if hasattr(self, "_title_label"):
            self._title_label.setStyleSheet(label_style)
        # 刷新表单标签（存放于 form layout 的 label 角色）
        for w in self.findChildren(BodyLabel):
            # hint 标签有独立颜色，不覆盖
            if w.objectName() != "hintLabel":
                w.setStyleSheet(label_style)

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 4, 8, 4)
        main_layout.setSpacing(12)

        # 标题
        name = self._def.get("name", self._platform)
        title = StrongBodyLabel(f"{name} 配置")
        self._title_label = title
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

            form.addRow(lbl, input_widget)
            self._inputs[key] = input_widget

        # 提示
        hint_text = self._def.get("hint", "")
        if hint_text:
            hint = BodyLabel(hint_text)
            hint.setObjectName("hintLabel")
            hint.setStyleSheet(
                f"color: rgba(255,255,255,0.5); padding: 8px 0; {get_font_family_css()} font-size: 11px;"
            )
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
        """保存配置（registry 分派：表单值 → def.build_config_values → 持久化）

        行为对齐 task 5 收官后的契约：所有平台一律走 def.build_config_values，
        主仓不再 if-elif Platform.X。表单字段列表来自 _FALLBACK_FIELDS 或
        def.config_schema；旧 _val 缺省回退仍存在（仅当字段不在 inputs 时
        从 existing 读），保证 schema 缺失场景不会丢已有凭证。
        """
        try:
            from app.gateway.config import get_gateway_config

            existing = get_gateway_config().get_platform_config(self._platform)

            def _val(key):
                if key in self._inputs:
                    return self._inputs[key].text().strip()
                if existing is None:
                    return None
                if hasattr(existing, key):
                    return getattr(existing, key)
                if existing.extra:
                    return existing.extra.get(key)
                return None

            # 收集所有 schema 字段（def 未提供时 _FALLBACK_FIELDS 兜底）。
            values: dict = {}
            for key, *_ in self._def.get("fields", []):
                v = _val(key)
                if v is not None:
                    values[key] = v

            config_obj = _save_platform_values(self._platform, values, existing)

            name = PLATFORM_DEFS.get(self._platform, {}).get("name", self._platform)
            from app.widgets.tab_manager_window import TabManagerWindow

            parent = TabManagerWindow.get_instance() or self.window()
            if config_obj is None:
                InfoBar.warning(
                    title="该平台不支持配置编辑",
                    content=f"{name} 缺少 build_config_values 回调，请在插件仓库补充表单字段",
                    parent=parent,
                    duration=3000,
                    position=InfoBarPosition.BOTTOM,
                )
                return

            InfoBar.success(
                title="保存成功",
                content=f"{name} 配置已保存",
                parent=parent,
                duration=2000,
                position=InfoBarPosition.BOTTOM,
            )

            self.saved.emit(self._platform, {})
            self.closed.emit()

        except Exception as e:
            from app.widgets.tab_manager_window import TabManagerWindow

            InfoBar.error(
                title="保存失败",
                content=str(e),
                parent=TabManagerWindow.get_instance() or self.window(),
            )


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
        """刷新状态（直接以 str 平台 id 读取，str-mixin 兼容第三方 id）"""
        try:
            from app.gateway.config import get_gateway_config

            config_helper = get_gateway_config()
            for key in PLATFORM_DEFS:
                row = self._rows.get(key)
                if row:
                    try:
                        pc = config_helper.get_platform_config(key)
                        row.set_enabled(pc.enabled)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[GatewaySettingCard] Refresh error: {e}")

    def refresh_style(self):
        """主题切换时刷新所有子控件样式"""
        Colors.refresh()
        for row in self._rows.values():
            if hasattr(row, "refresh_style"):
                row.refresh_style()
        # 如果当前正在编辑，也刷新编辑卡片
        if self._current_edit_card is not None and hasattr(self._current_edit_card, "refresh_style"):
            self._current_edit_card.refresh_style()
