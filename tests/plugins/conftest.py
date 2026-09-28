# -*- coding: utf-8 -*-
"""plugins/ 测试共享 fixtures。

T19 批次1 上收：fresh_registry（UIPluginRegistry 变体）原散布于本目录 24 个
测试文件，定义体一致：新建实例并 monkeypatch 单例入口，
用例结束由 monkeypatch 自动还原 get_instance；_instance 直接赋值覆盖。
"""
import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def tool_settings_snapshot():
    """工具策略三 ConfigItem 快照/还原（function 级，非 autouse）。

    供直接写 Settings.tool_toggles / tool_off_behavior / tool_permission_policy
    的用例显式声明，退出时还原进参值（D3 隔离方案的一部分：避免测试态
    写穿到真实配置，与 tests/conftest.py 的 DRIFOX_DATA_DIR 隔离互补）。
    """
    from app.utils.config import Settings

    s = Settings.get_instance()
    snap = (s.tool_toggles.value, s.tool_off_behavior.value, s.tool_permission_policy.value)
    yield s
    s.tool_toggles.value, s.tool_off_behavior.value, s.tool_permission_policy.value = snap
    s.save()


@pytest.fixture()
def fresh_registry(monkeypatch):
    """每用例独立 UIPluginRegistry（绕过单例状态污染）。"""
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg
