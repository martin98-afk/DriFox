# -*- coding: utf-8 -*-
"""回归：运行期 rescan 登记的 config_schema 必须在插件变更广播前回放成设置卡。

原缺陷：replay_config_cards 只在启动 _after_plugins_loaded 调一次；热重载 /
安装 / 启停经 rescan 重新登记 pending 后无人回放。已打开或重新打开的系统配置
仍读旧卡清单，新增配置项必须新开窗口（新 backend 走启动回放）才出现。
"""

import json

import pytest

pytest.importorskip("PySide6")

PLUGIN_NAME = "plug-hot-replay"


def _write_manifest(plug_dir, fields):
    manifest_dir = plug_dir / ".drifox-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": PLUGIN_NAME,
                "version": "1.0.0",
                "config_schema": {"title": "热重载卡", "fields": fields},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _card(ui):
    return next((c for c in ui.get_settings_cards() if c.card_id == f"{PLUGIN_NAME}-config"), None)


def _make_host(monkeypatch):
    """裸 QObject 宿主：绕过单例/AgentManager 初始化，只验证广播前回放接线"""
    from PySide6.QtCore import QObject

    from app.core.services.plugin_host_service import PluginHostService

    svc = PluginHostService.__new__(PluginHostService)
    QObject.__init__(svc)
    svc._hot_reload_seq = 0
    monkeypatch.setattr(svc, "_trigger_plugin_changed_hook", lambda *a, **k: None)
    return svc


@pytest.fixture()
def pm(monkeypatch):
    from app.plugins.managers.plugin_manager import PluginManager

    manager = PluginManager()
    manager._initialized = True
    monkeypatch.setattr(PluginManager, "get_instance", staticmethod(lambda: manager))
    yield manager
    manager._unregister_config_schema(PLUGIN_NAME)


def test_broadcast_replays_config_card_after_rescan(pm, tmp_path, monkeypatch, qapp):
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    ui = UIPluginRegistry.get_instance()
    plug_dir = tmp_path / PLUGIN_NAME

    _write_manifest(plug_dir, [{"key": "a", "label": "A", "type": "text"}])
    pm._scan_one_plugin_dir(plug_dir, "user")
    assert _card(ui) is None  # 扫描期只登记 pending，与启动期契约一致

    svc = _make_host(monkeypatch)
    svc.emit_plugin_changed({"ui": True}, PLUGIN_NAME, action="updated")
    assert _card(ui) is not None

    # 新增字段 → 再次 rescan 重新登记 → 回放必须换新卡类（签名驱动面板重建）
    old_cls = _card(ui).widget_class
    _write_manifest(
        plug_dir,
        [
            {"key": "a", "label": "A", "type": "text"},
            {"key": "b", "label": "B", "type": "text"},
        ],
    )
    pm._scan_one_plugin_dir(plug_dir, "user")
    svc.emit_plugin_changed({"ui": True}, PLUGIN_NAME, action="updated")
    new_cls = _card(ui).widget_class
    assert new_cls is not old_cls
    assert "b" in new_cls()._rows  # 新配置项直接可渲染（不依赖新开窗口）


def test_broadcast_without_pending_is_noop(pm, monkeypatch):
    _make_host(monkeypatch).emit_plugin_changed({})  # 不抛即可
