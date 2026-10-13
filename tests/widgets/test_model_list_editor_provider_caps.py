# -*- coding: utf-8 -*-
"""模型列表编辑器 per-model 能力查询（provider_name 贯通）回归。

背景（2026-10-13 用户实测）：服务商编辑卡的模型列表能力列对 CodeBuddy
claude-opus-4.8 等私有 ID 全 "-"——_caps_for 旧实现 get_model_capabilities(name)
不传 provider，L-1 插件 per-model 层被跳过（models.dev 无带点 ID，全链落空）。

覆盖：
1. 构造带 provider_name → _caps_for 命中插件 per-model 声明
2. 不传 provider_name → 行为不变（降级全局链，桩隔离后为空）
3. set_provider_name 切换 → 缓存作废，后续查询带新名
"""

import sys

import pytest
from PyQt5.QtCore import QCoreApplication, Qt
from PyQt5.QtWidgets import QApplication

import app.core.modelmeta.model_capabilities as mc
from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

_PROVIDER = "PerModel Test Provider"


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture()
def provider_registry(monkeypatch):
    """独立 registry + 注册带 per-model 声明的 ProviderDef；隔离 models.dev 动态层"""
    reg = ProviderRegistry()
    reg.register(
        ProviderDef(
            name=_PROVIDER,
            family="permodel_test",
            models=["gw-opus"],
            model_capabilities={
                "gw-opus": {
                    "context_limit": 400000,
                    "max_output_tokens": 131072,
                    "supports_thinking": True,
                    "thinking_param": "reasoning_effort",
                    "supports_vision": True,
                }
            },
        )
    )
    monkeypatch.setattr(ProviderRegistry, "get_instance", staticmethod(lambda: reg))
    monkeypatch.setattr(mc, "_get_partitioned_model_capabilities", lambda n, p: None)
    monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda n: None)
    return reg


def _make_editor(qapp, provider_name=""):
    from app.widgets.model_list_edit_dialog import ModelListEditorWidget

    return ModelListEditorWidget(["gw-opus"], provider_name=provider_name)


def test_caps_follow_provider_name(provider_registry, qapp):
    """带 provider_name 构造 → 能力查询命中插件 per-model 声明"""
    editor = _make_editor(qapp, provider_name=_PROVIDER)
    caps = editor._caps_for("gw-opus")
    assert caps["context_limit"] == 400000
    assert caps["thinking_param"] == "reasoning_effort"


def test_caps_without_provider_unchanged(provider_registry, qapp):
    """不传 provider_name（既有调用）→ 查全局链，插件层不介入"""
    editor = _make_editor(qapp)
    assert editor._caps_for("gw-opus") == {}


def test_set_provider_name_invalidates_cache(provider_registry, qapp):
    """切换服务商：缓存作废，后续查询带新名"""
    editor = _make_editor(qapp, provider_name=_PROVIDER)
    editor._caps_for("gw-opus")
    editor.set_provider_name("Other Provider")
    assert editor._caps_cache == {}
    # 新 provider 未注册声明 → 全局链（桩隔离后为空）
    assert editor._caps_for("gw-opus") == {}
    # 切回 → 重新命中
    editor.set_provider_name(_PROVIDER)
    assert editor._caps_for("gw-opus")["context_limit"] == 400000
