# -*- coding: utf-8 -*-
"""system-im 插件测试：manifest 声明 / 设置卡状态映射 / ui 注册与卸载。

网关业务逻辑（manager/session/media/interaction）由 ``tests/gateway`` 覆盖；
本文件只验证插件外壳：清单契约、设置卡对 ``get_status()`` 的呈现、
``register_ui`` 把服务挂进 WindowServiceRegistry 并注册设置卡。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN = _ROOT / "plugins" / "system-im"


def _manifest() -> dict:
    return json.loads((_PLUGIN / ".drifox-plugin" / "plugin.json").read_text(encoding="utf-8"))


_UI_MODULE = None


def _load_ui_module():
    """以包形式加载 ui/__init__.py（其内部有相对 import）；进程内只加载一次。

    重复 exec 会新建包对象，而相对 import 的子模块仍命中 sys.modules 缓存，
    新包对象上拿不到 ``settings_card`` 属性。
    """
    global _UI_MODULE
    if _UI_MODULE is not None:
        return _UI_MODULE
    name = "ui_plugin_system_im_under_test"
    spec = importlib.util.spec_from_file_location(
        name, str(_PLUGIN / "ui" / "__init__.py"), submodule_search_locations=[str(_PLUGIN / "ui")]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[name] = module
    spec.loader.exec_module(module)
    _UI_MODULE = module
    return module


def _load_card_module():
    return _load_ui_module().settings_card


class _FakeService:
    def __init__(self, status=None):
        self._status = status or {"running": False, "platforms": {}, "session_count": 0}
        self.calls: list = []

    def get_status(self):
        return self._status

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def set_platform_enabled(self, platform_id, enabled):
        self.calls.append(("set_platform_enabled", platform_id, enabled))

    def restart_platform(self, platform_id):
        self.calls.append(("restart_platform", platform_id))


def test_manifest_declares_system_plugin():
    """type=system + ui 组件 + 插件模块命名空间 + 迁移所需能力。"""
    data = _manifest()
    assert data["name"] == "system-im"
    assert data["type"] == "system"
    assert data["components"]["ui"] is True
    prefixes = data["module_prefixes"]
    for prefix in ("system_im_core", "system_im_engine", "system_im_service"):
        assert prefix in prefixes
    caps = set(data["capabilities"])
    assert {"engine.session", "engine.session.resume", "session.persist"} <= caps


def test_core_loader_registers_core_package():
    """core 包按固定名注册，子模块可被正常 import（engine/service 依赖它）。"""
    _load_ui_module().core_loader.load_core()
    import system_im_core.manager as manager_mod

    assert hasattr(manager_mod, "create_platform_manager")


def test_platform_row_state_mapping():
    """状态映射：在线/连接中/已关闭/未就绪/错误。"""
    card_mod = _load_card_module()
    cases = [
        ({"enabled": True, "connected": True, "available": True, "error": None}, "在线"),
        ({"enabled": True, "connected": False, "available": True, "error": None}, "连接中"),
        ({"enabled": False, "connected": False, "available": True, "error": None}, "已关闭"),
        ({"enabled": True, "connected": False, "available": False, "error": None}, "未就绪"),
        ({"enabled": True, "connected": False, "available": True, "error": "凭据无效"}, "错误：凭据无效"),
    ]
    for info, expected in cases:
        row = card_mod._PlatformRow("wecom", "企业微信")
        row.update_state(info)
        assert row.state_label.text() == expected, info


def test_platform_row_forwards_toggle_and_reconnect(monkeypatch):
    """开关与重连按钮把动作转给插件服务，卡片不直接碰 manager。"""
    card_mod = _load_card_module()
    fake = _FakeService()
    monkeypatch.setattr(card_mod, "_resolve_service", lambda: fake)
    row = card_mod._PlatformRow("feishu", "飞书")
    row._on_toggled(True)
    row._on_reconnect()
    assert fake.calls == [("set_platform_enabled", "feishu", True), ("restart_platform", "feishu")]


def test_register_ui_registers_service_and_settings_card(monkeypatch):
    """register_ui：服务进 WindowServiceRegistry 全局槽 + 设置卡挂 plugins 分区。"""
    ui = _load_ui_module()
    fake = _FakeService()

    class _Registry:
        def __init__(self):
            self.cards = []

        def register_settings_card(self, *args, **kwargs):
            self.cards.append((args, kwargs))

    monkeypatch.setattr(ui, "_ensure_service", lambda: fake)
    monkeypatch.setattr(ui, "_service_enabled_pref", lambda: True)
    registry = _Registry()
    try:
        ui.register_ui(registry)
        assert fake.calls == [("start",)]
        assert len(registry.cards) == 1
        args, kwargs = registry.cards[0]
        assert args[0] == "system-im" and args[1] == "im_gateway"
        assert kwargs["section"] == "plugins"

        from app.plugins.registries.window_services import get_window_services

        assert get_window_services().get("im_gateway") is fake
    finally:
        ui.unload_ui()
    assert get_window_services().get("im_gateway") is None


def test_settings_card_refresh_reflects_service_status(monkeypatch):
    """卡片刷新：总开关随 running 同步，平台行数量与注册表一致。"""
    card_mod = _load_card_module()
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    reg = GatewayPlatformRegistry.get_instance()
    for pid, name in (("wecom", "企业微信"), ("feishu", "飞书")):
        reg.register(
            GatewayPlatformDef(
                platform_id=pid, display_name=name, adapter_factory=lambda cfg: None, source="test:im"
            )
        )
    fake = _FakeService(
        {
            "running": True,
            "session_count": 2,
            "platforms": {
                "wecom": {"enabled": True, "connected": True, "available": True, "error": None},
                "feishu": {"enabled": True, "connected": False, "available": True, "error": None},
            },
        }
    )
    monkeypatch.setattr(card_mod, "_resolve_service", lambda: fake)
    monkeypatch.setattr(card_mod, "_persist_service_enabled", lambda enabled: None)
    try:
        from PyQt5.QtWidgets import QApplication

        assert QApplication.instance() is not None
        card = card_mod.ImGatewaySettingsCard()
        card._timer.stop()
        assert card._height_mode == "content", "详情页内嵌需要内容自然高度"
        # 设置页里的配置卡不可关闭（没有 CardManager 能再唤回）
        assert card.close_btn.isHidden(), "设置详情页里的 IM 配置卡不应出现关闭按钮"
        card._rebuild_platform_rows()
        card._refresh()
        assert card._service_switch.isChecked() is True
        assert card._service_state.text() == "运行中"
        assert set(card._rows) >= {"wecom", "feishu"}
        assert card._rows["wecom"].state_label.text() == "在线"
        assert card._rows["feishu"].state_label.text() == "连接中"
        card.close()
    finally:
        reg.unregister_source("test:im")
