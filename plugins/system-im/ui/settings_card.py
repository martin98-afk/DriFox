# -*- coding: utf-8 -*-
"""IM 网关设置卡：服务总开关 + 平台在线列表（每平台启停与重连）。

数据全部来自 system-im 插件自己的 ``GatewayService``（经宿主
``WindowServiceRegistry`` 全局槽取用），卡片只做展示与转发，不碰
PlatformManager 私有状态。平台清单来自宿主 ``GatewayPlatformRegistry``：
新增一个 gateway-* 插件后本卡片自动多出一行，本文件无需改动。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from loguru import logger
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    StrongBodyLabel,
    SwitchButton,
    TransparentToolButton,
)

from app.utils.design_tokens import Colors
from app.widgets.cards.settings.base_settings_card import BaseSettingsCard

SERVICE_KEY = "im_gateway"
_PLUGIN_NAME = "system-im"
_POLL_MS = 2000

_STATE_TEXT = {
    "online": "在线",
    "connecting": "连接中",
    "off": "已关闭",
    "unavailable": "未就绪",
    "error": "错误",
}


def _resolve_service() -> Optional[Any]:
    """从窗口服务全局槽取插件持有的 GatewayService（未加载返回 None）。"""
    try:
        from app.plugins.registries.window_services import get_window_services

        return get_window_services().get(SERVICE_KEY)
    except Exception as e:  # noqa: BLE001 - 服务表不可用按未就绪展示
        logger.debug(f"[system-im] 服务查询失败: {e}")
        return None


def _persist_service_enabled(enabled: bool) -> None:
    """记住总开关状态，下次启动按此决定是否自动连接。"""
    try:
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        PluginConfigStore().set_values(_PLUGIN_NAME, {"service_enabled": enabled})
    except Exception as e:  # noqa: BLE001 - 写失败只影响下次自动启动
        logger.warning(f"[system-im] 服务开关保存失败: {e}")


class _PlatformRow(QFrame):
    """一个平台一行：名称 + 状态 + 启用开关 + 重连按钮。"""

    def __init__(self, platform_id: str, display_name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.platform_id = platform_id
        self._display_name = display_name or platform_id
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(8)

        self.name_label = BodyLabel(self._display_name, self)
        self.name_label.setMinimumWidth(120)
        layout.addWidget(self.name_label)

        self.state_label = CaptionLabel(_STATE_TEXT["unavailable"], self)
        layout.addWidget(self.state_label)
        layout.addStretch(1)

        self.reconnect_btn = TransparentToolButton(FluentIcon.SYNC, self)
        self.reconnect_btn.setToolTip("重新连接")
        self.reconnect_btn.clicked.connect(self._on_reconnect)
        layout.addWidget(self.reconnect_btn)

        self.enable_switch = SwitchButton(self)
        self.enable_switch.setToolTip("启用该平台并在配置就绪时建立连接")
        self.enable_switch.checkedChanged.connect(self._on_toggled)
        layout.addWidget(self.enable_switch)

        self._apply_row_style()

    def _apply_row_style(self) -> None:
        self.setStyleSheet(
            f"""
            _PlatformRow {{
                background: transparent;
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
            }}
            _PlatformRow:hover {{
                border-color: {Colors.BORDER_ACCENT};
            }}
            """
        )

    def update_state(self, info: Dict[str, Any]) -> None:
        """按 manager.get_status() 的单平台条目刷新显示。"""
        enabled = bool(info.get("enabled"))
        connected = bool(info.get("connected"))
        available = bool(info.get("available"))
        error = info.get("error") or ""

        if not available:
            state, color = "unavailable", Colors.TEXT_MUTED
        elif error:
            state, color = "error", Colors.ERROR
        elif not enabled:
            state, color = "off", Colors.TEXT_MUTED
        elif connected:
            state, color = "online", Colors.SUCCESS
        else:
            state, color = "connecting", Colors.WARNING

        text = _STATE_TEXT[state]
        if state == "error":
            text = f"错误：{str(error)[:40]}"
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {color};")
        self.reconnect_btn.setEnabled(available and enabled)

        if self.enable_switch.isChecked() != enabled:
            self.enable_switch.blockSignals(True)
            self.enable_switch.setChecked(enabled)
            self.enable_switch.blockSignals(False)

    def _on_toggled(self, checked: bool) -> None:
        service = _resolve_service()
        if service is None:
            return
        service.set_platform_enabled(self.platform_id, bool(checked))

    def _on_reconnect(self) -> None:
        service = _resolve_service()
        if service is None:
            return
        service.restart_platform(self.platform_id)


class ImGatewaySettingsCard(BaseSettingsCard):
    """IM 网关设置卡（设置 → 插件页）。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("即时通讯网关", "", parent=parent)
        # 详情页内嵌场景需要完整展示内容，不走「窗口高度 85%」的浮动卡口径
        self.set_height_mode("content")
        # 设置页里的配置卡不可关闭：关掉后没有 CardManager 能再唤回，
        # 用户会以为配置入口消失了。
        close_btn = getattr(self, "close_btn", None)
        if close_btn is not None:
            close_btn.setVisible(False)
        self._rows: Dict[str, _PlatformRow] = {}
        self._syncing = False
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._refresh)
        self._setup_ui()
        self._rebuild_platform_rows()
        self._refresh()
        self._timer.start()

    # ── UI ──

    def _setup_ui(self) -> None:
        Colors.refresh()

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        title = StrongBodyLabel("IM 服务", header)
        header_layout.addWidget(title)

        self._service_state = CaptionLabel("未就绪", header)
        header_layout.addWidget(self._service_state)
        header_layout.addStretch(1)

        self._service_switch = SwitchButton(header)
        self._service_switch.setToolTip("总开关：关闭后停止全部平台连接")
        self._service_switch.checkedChanged.connect(self._on_service_toggled)
        header_layout.addWidget(self._service_switch)
        self.content_layout.addWidget(header)

        hint = CaptionLabel("开启后按各平台配置建立连接；关闭则断开全部平台。", self)
        hint.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
        self.content_layout.addWidget(hint)

        self._platform_box = QWidget(self)
        self._platform_layout = QVBoxLayout(self._platform_box)
        self._platform_layout.setContentsMargins(0, 4, 0, 0)
        self._platform_layout.setSpacing(6)
        self.content_layout.addWidget(self._platform_box)

        self._empty_label = CaptionLabel("尚未注册任何 IM 平台插件。", self)
        self._empty_label.setStyleSheet(f"color: {Colors.TEXT_MUTED};")
        self.content_layout.addWidget(self._empty_label)

        footer = QWidget(self)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(8)

        self._session_label = CaptionLabel("", footer)
        self._session_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
        footer_layout.addWidget(self._session_label)
        footer_layout.addStretch(1)

        refresh_btn = TransparentToolButton(FluentIcon.SYNC, footer)
        refresh_btn.setToolTip("立即刷新状态")
        refresh_btn.clicked.connect(self._refresh)
        footer_layout.addWidget(refresh_btn)
        self.content_layout.addWidget(footer)

    def _platform_defs(self) -> list:
        try:
            from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

            return GatewayPlatformRegistry.get_instance().list_platforms()
        except Exception as e:  # noqa: BLE001 - 注册表不可用按无平台展示
            logger.debug(f"[system-im] 平台注册表查询失败: {e}")
            return []

    def _rebuild_platform_rows(self) -> None:
        """按注册表重建平台行（幂等：清单未变时零操作）。"""
        defs = self._platform_defs()
        ids = [d.platform_id for d in defs]
        if ids == list(self._rows):
            return
        for row in self._rows.values():
            self._platform_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        for d in defs:
            row = _PlatformRow(d.platform_id, d.display_name, self._platform_box)
            self._rows[d.platform_id] = row
            self._platform_layout.addWidget(row)
        visible = bool(defs)
        self._platform_box.setVisible(visible)
        self._empty_label.setVisible(not visible)

    # ── 状态 ──

    def _refresh(self) -> None:
        service = _resolve_service()
        if service is None:
            self._service_state.setText("未就绪")
            self._service_switch.setEnabled(False)
            self._session_label.setText("")
            return
        try:
            status = service.get_status() or {}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[system-im] 状态读取失败: {e}")
            return
        running = bool(status.get("running"))
        platforms: Dict[str, Any] = status.get("platforms") or {}
        online = sum(1 for p in platforms.values() if p.get("connected"))

        self._service_switch.setEnabled(True)
        if self._service_switch.isChecked() != running:
            self._syncing = True
            self._service_switch.setChecked(running)
            self._syncing = False
        self._service_state.setText("运行中" if running else "已停止")
        self._service_state.setStyleSheet(
            f"color: {Colors.SUCCESS if running else Colors.TEXT_MUTED};"
        )
        self._session_label.setText(
            f"{len(platforms)} 个平台 · {online} 在线 · {int(status.get('session_count') or 0)} 个会话"
        )

        for pid, row in self._rows.items():
            row.update_state(platforms.get(pid) or {})

    # ── 交互 ──

    def _on_service_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        service = _resolve_service()
        if service is None:
            return
        if checked:
            service.start()
        else:
            service.stop()
        _persist_service_enabled(bool(checked))
        QTimer.singleShot(300, self._refresh)

    # ── 生命周期 ──

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        self._rebuild_platform_rows()
        self._refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._timer.stop()
        super().hideEvent(event)
