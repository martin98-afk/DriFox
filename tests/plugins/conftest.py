# -*- coding: utf-8 -*-
"""plugins/ 测试共享 fixtures。

T19 批次1 上收：fresh_registry（UIPluginRegistry 变体）原散布于本目录 24 个
测试文件，定义体一致：新建实例并 monkeypatch 单例入口，
用例结束由 monkeypatch 自动还原 get_instance；_instance 直接赋值覆盖。
"""
import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    """每用例独立 UIPluginRegistry（绕过单例状态污染）。"""
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg
