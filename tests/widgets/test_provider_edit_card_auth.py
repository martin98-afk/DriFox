# -*- coding: utf-8 -*-
"""P0-5 认证方式修复回归测试

修复背景
--------
认证方式在 UI 层被写死 `"bearer"`（provider_edit_card.py 两处），把插件声明的
`auth_type`（百度千帆是 `bce`）覆盖掉 → 百度千帆走错签名分支，**当前版本无法
通过 UI 正确配置**。插件声明 `baidu.py:19 auth_type="bce"` 一直被丢弃。

修法（读取统一走 app/utils/provider_ui_meta.get_auth_type）：
- 写点一 `_on_fetch_models` 的 hook_config
- 写点二 `_on_save` 的 payload
- 读点治愈 main_widget._load_model_configs 的合并循环（老配置里存的 bearer 被声明值覆盖）

本组锁定：两个写点都取到插件声明值；未注册服务商回落存档值；读点治愈生效。
"""

import sys
from pathlib import Path

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


@pytest.fixture()
def registry(monkeypatch):
    """隔离注册表：百度千帆声明 bce（复刻真实插件声明）"""
    reg = ProviderRegistry()
    reg.register(
        ProviderDef(
            name="百度千帆",
            api_url="https://qianfan.baidubce.com/v2",
            auth_type="bce",
            default_model="ernie-4.0",
        ),
        source="plugin:test",
    )
    reg.register(
        ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com", auth_type="bearer"),
        source="plugin:test",
    )
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def card(registry, qapp):
    from app.widgets.cards.settings import provider_edit_card as mod

    def _make(provider_name: str, provider_info: dict, is_new: bool = False):
        return mod.ProviderEditCard(provider_name=provider_name, provider_info=dict(provider_info), is_new=is_new)

    return _make


# ══════════════════════════════════════════════════════════════════
# 写点二：保存 payload
# ══════════════════════════════════════════════════════════════════


def test_save_payload_uses_declared_bce(card):
    """百度千帆保存 → payload 认证方式是 bce（不是写死的 bearer）"""
    c = card("百度千帆", {"API_URL": "https://qianfan.baidubce.com/v2", "API_KEY": "sk-x"})
    captured = {}
    c.saved.connect(lambda name, info: captured.update(info=info))
    c.apiKeyEdit.setText("sk-x")
    c.modelListEditor._add_tokens(["ernie-4.0"])
    c.modelListEditor.setDefaultModel("ernie-4.0")
    c._on_save()

    assert captured["info"]["认证方式"] == "bce", "插件声明的 bce 必须被保留"


def test_save_payload_falls_back_to_saved_value(card):
    """未注册插件 → 用存档值（用户自定义的 none 不被覆盖成 bearer）"""
    c = card("我的自建服务", {"API_URL": "http://localhost:8000/v1", "API_KEY": "", "认证方式": "none"})
    captured = {}
    c.saved.connect(lambda name, info: captured.update(info=info))
    c._on_save()

    assert captured["info"]["认证方式"] == "none"


def test_save_payload_defaults_to_bearer(card):
    """未注册且存档为空 → 兜底 bearer（保持历史默认行为）"""
    c = card("无声明服务商", {"API_URL": "https://x/v1", "API_KEY": ""})
    captured = {}
    c.saved.connect(lambda name, info: captured.update(info=info))
    c._on_save()

    assert captured["info"]["认证方式"] == "bearer"


# ══════════════════════════════════════════════════════════════════
# 写点一：models_hook 的 hook_config
# ══════════════════════════════════════════════════════════════════


def test_fetch_hook_config_uses_declared_auth(registry, card, monkeypatch):
    """models_hook 收到的 config 里认证方式是插件声明值"""
    captured = {}

    def hook(config):
        captured["config"] = config
        return ["m1"]

    # 追加一个带 models_hook 的服务商
    registry.register(
        ProviderDef(
            name="带钩子服务商",
            api_url="https://h/v1",
            auth_type="bce",
            capabilities={"models_hook": hook},
        ),
        source="plugin:test",
    )
    c = card("带钩子服务商", {"API_URL": "https://h/v1", "API_KEY": "sk-h"}, is_new=False)
    c.apiKeyEdit.setText("sk-h")
    c._on_fetch_models()

    # 等后台线程把 hook 跑完
    from PyQt5.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    c.fetchSuccess.connect(loop.quit)
    c.fetchFailed.connect(loop.quit)
    QTimer.singleShot(3000, loop.quit)
    loop.exec_()

    assert "config" in captured, "hook 未被调用"
    assert captured["config"]["认证方式"] == "bce", "hook_config 必须带插件声明的认证方式"


# ══════════════════════════════════════════════════════════════════
# 读点治愈（main_widget 合并循环，源码级断言 + 纯逻辑复刻）
# ══════════════════════════════════════════════════════════════════


def test_load_model_configs_source_heals_auth_type():
    """读点治愈：源码含 `or default_key == "认证方式"` 例外分支"""
    import inspect

    from app.main_widget import OpenAIChatToolWindow

    src = inspect.getsource(OpenAIChatToolWindow._load_model_configs)
    assert 'default_key == "认证方式"' in src, "读点治愈缺失：认证方式不会被声明值覆盖"
    assert "if default_key not in config or default_key" in src, "条件写法应保持单行合并式"


def test_healing_logic_semantics(registry):
    """复刻治愈逻辑：老配置存 bearer + 插件声明 bce → 合并后为 bce；磁盘不动"""
    from app.constants import provider_default_config

    declared = provider_default_config("百度千帆") or {}
    assert declared.get("认证方式") == "bce", "前置：插件应声明 bce"

    config = {"provider_name": "百度千帆", "认证方式": "bearer"}  # 老配置（修复前存下的）
    for default_key, default_value in declared.items():
        if default_key not in config or default_key == "认证方式":
            config[default_key] = default_value

    assert config["认证方式"] == "bce", "合并结果应为插件声明值（内存治愈）"


def test_healing_keeps_unregistered_provider_auth(registry):
    """治愈只对「有插件声明」的服务商生效；自定义服务商的 none 不受影响"""
    from app.constants import provider_default_config

    declared = provider_default_config("我的自建服务") or {}  # 无插件 → 空
    config = {"provider_name": "我的自建服务", "认证方式": "none"}
    for default_key, default_value in declared.items():
        if default_key not in config or default_key == "认证方式":
            config[default_key] = default_value

    assert config["认证方式"] == "none", "自定义服务商的认证方式不得被改写"
