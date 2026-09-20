# -*- coding: utf-8 -*-
"""安全中心卡 smoke 测试：实例化 / 配置读写联动 / 名单编辑回调"""

import sys

import pytest
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="session")
def _app_keepalive():
    """进程级持有 QApplication 引用（防 GC 销毁后重建损坏 Qt 全局状态，见 test_command_card_pin.py）"""
    app = _ensure_qapp()
    yield app


@pytest.fixture(autouse=True)
def _qapp(_app_keepalive):
    yield


@pytest.fixture
def isolated_cfg(tmp_path, monkeypatch):
    """隔离 SandboxConfig：单例指向 tmp_path 配置文件（沙箱默认显式开启）"""
    from app.tools.sandbox import SandboxConfig

    SandboxConfig.reset_instance()
    cfg = SandboxConfig(config_path=str(tmp_path / "sandbox_config.json"))
    cfg.set("sandbox_enabled", True)  # 测试关注开启态行为，不依赖全局默认值
    monkeypatch.setattr(SandboxConfig, "_instance", cfg)
    yield cfg
    SandboxConfig.reset_instance()


def test_card_instantiates_and_binds_config(isolated_cfg):
    """总开关反映配置；切开关写回配置"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    card = SecurityCenterCard()
    # 总开关反映配置（fixture 显式开启）
    assert card.sandbox_switch.isChecked() is True
    # 切开关写回配置
    card.sandbox_switch.setChecked(False)
    assert isolated_cfg.get("sandbox_enabled") is False
    # 删除保护与系统豁免同理
    card.delete_switch.setChecked(False)
    assert isolated_cfg.get("delete_protection") is False
    card.sys_switch.setChecked(True)
    assert isolated_cfg.get("sys_tools_bypass") is True


def test_path_list_editor_roundtrip(isolated_cfg):
    """名单增删回调写回配置（card 级接口按 key 路由）"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    card = SecurityCenterCard()
    card._add_path_entry("whitelist", "D:/other_proj")
    assert "D:/other_proj" in isolated_cfg.get("path.whitelist")
    card._remove_path_entry("whitelist", 0)
    assert isolated_cfg.get("path.whitelist") == []


def test_command_and_network_lists_route_to_config(isolated_cfg):
    """命令/网络名单编辑路由到正确的配置键"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    card = SecurityCenterCard()
    card._add_path_entry("allow_prefixes", "git")
    card._add_path_entry("confirm_prefixes", "git push")
    card._add_path_entry("blacklist_domains", "evil.com")
    assert isolated_cfg.get("command.allow_prefixes") == ["git"]
    assert isolated_cfg.get("command.confirm_prefixes") == ["git push"]
    assert isolated_cfg.get("network.blacklist_domains") == ["evil.com"]


def test_duplicate_add_is_idempotent(isolated_cfg):
    """重复添加同一名单条目不产生重复项"""
    from app.widgets.cards.settings.security_center_card import SecurityCenterCard

    card = SecurityCenterCard()
    card._add_path_entry("blacklist", "D:/secrets")
    card._add_path_entry("blacklist", "D:/secrets")
    assert isolated_cfg.get("path.blacklist") == ["D:/secrets"]
