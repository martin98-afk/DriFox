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


def test_caps_without_provider_uses_fallback(provider_registry, qapp):
    """不传 provider_name（既有调用）→ 插件层不介入，只落 family 兜底

    行为变更（能力声明 UI）：_caps_for 改走 resolve 链，无数据时返回
    family/default 的**生效值**而非空 dict（列表显示的就是发送时真用的值）。
    """
    editor = _make_editor(qapp)
    caps = editor._caps_for("gw-opus")
    assert caps.get("thinking_param") is None
    assert caps["context_limit"] == 200000  # custom family 兜底


def test_set_provider_name_invalidates_cache(provider_registry, qapp):
    """切换服务商：缓存作废，后续查询带新名"""
    editor = _make_editor(qapp, provider_name=_PROVIDER)
    editor._caps_for("gw-opus")
    editor.set_provider_name("Other Provider")
    assert editor._caps_cache == {}
    # 新 provider 未注册声明 → 自动链兜底（插件层不介入）
    assert editor._caps_for("gw-opus")["context_limit"] == 200000
    # 切回 → 重新命中
    editor.set_provider_name(_PROVIDER)
    assert editor._caps_for("gw-opus")["context_limit"] == 400000


def test_declared_caps_override_auto_chain(provider_registry, qapp, monkeypatch):
    """用户声明（ModelOverrides 六键）优先于插件 per-model 声明"""
    from app.utils.config import Settings

    monkeypatch.setattr(
        Settings.llm_model_overrides,
        "value",
        {f"{_PROVIDER}||gw-opus": {"声明_上下文长度": 32000, "声明_支持图像": False}},
        raising=False,
    )
    editor = _make_editor(qapp, provider_name=_PROVIDER)
    caps = editor._caps_for("gw-opus")
    assert caps["context_limit"] == 32000
    assert caps["supports_vision"] is False
    # 未声明的字段仍走自动链（插件 per-model 声明）
    assert caps["thinking_param"] == "reasoning_effort"


def test_row_has_caps_edit_button(provider_registry, qapp):
    """行内有能力编辑按钮，点击回调带模型名"""
    editor = _make_editor(qapp, provider_name=_PROVIDER)
    row = editor._row_of("gw-opus")
    assert row is not None and hasattr(row, "caps_btn")
    clicked: list = []
    row._on_edit_caps = clicked.append
    row.caps_btn.click()
    assert clicked == ["gw-opus"]


def test_row_caps_refresh_after_declaration(provider_registry, qapp, monkeypatch):
    """编辑声明后行内灰字/徽章随 resolve 结果刷新"""
    from app.utils.config import Settings

    monkeypatch.setattr(Settings.llm_model_overrides, "value", {}, raising=False)
    editor = _make_editor(qapp, provider_name=_PROVIDER)
    assert editor._caps_for("gw-opus")["context_limit"] == 400000

    # 模拟弹窗保存：覆盖值落进 ModelOverrides，缓存作废后重算
    monkeypatch.setattr(
        Settings.llm_model_overrides,
        "value",
        {f"{_PROVIDER}||gw-opus": {"声明_上下文长度": 16000}},
        raising=False,
    )
    editor._caps_cache.pop("gw-opus")
    editor._sources_cache.pop("gw-opus")
    row = editor._row_of("gw-opus")
    row.update_caps(editor._caps_for("gw-opus"))
    assert row.sub_label.text() == "上下文 16K"


def test_edit_dialog_save_writes_overrides(provider_registry, qapp, monkeypatch):
    """弹窗保存 → 真实走 Settings.set 写入（不 mock set，只掐掉落盘）

    这里刻意不替换 Settings.set：它曾以 `Settings.set(item, value)` 未绑定调用，
    把 ConfigItem 当 self 抛 AttributeError，再加兜底 except 吞异常 → 界面「保存
    没反应」。mock 掉 set 会让这类错永远测不出来。
    """
    from app.utils.config import Settings

    monkeypatch.setattr(Settings.llm_model_overrides, "value", {}, raising=False)
    monkeypatch.setattr(Settings, "save", lambda self: None)  # 真实 set 路径，只跳过写盘

    class _FakeDialog:
        saved = True
        declared = {"声明_支持思考": True}

        def __init__(self, *args, **kwargs):
            pass

        def exec_(self):
            return 1

    import app.widgets.model_capability_edit_dialog as dialog_mod

    monkeypatch.setattr(dialog_mod, "ModelCapabilityEditDialog", _FakeDialog)

    editor = _make_editor(qapp, provider_name=_PROVIDER)
    editor._on_row_edit_caps("gw-opus")

    assert Settings.llm_model_overrides.value[f"{_PROVIDER}||gw-opus"] == {"声明_支持思考": True}
    # 缓存失效后行内取值反映声明
    assert editor._caps_for("gw-opus")["supports_thinking"] is True


def test_sub_label_appears_after_adding_context(provider_registry, qapp):
    """无上下文 → 有上下文：灰字行必须能出现（布局里恒占位，隐藏让位）"""
    editor = _make_editor(qapp, provider_name=_PROVIDER)
    row = editor._row_of("gw-opus")
    row.update_caps({})
    assert row.sub_label.isHidden()
    row.update_caps({"context_limit": 64000})
    assert not row.sub_label.isHidden()
    assert row.sub_label.text() == "上下文 64K"
