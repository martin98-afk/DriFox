# -*- coding: utf-8 -*-
"""prompt-enhancer 插件验收：input_button + E1 config_schema 注册 + on_click 回注链路。

不触发真实 LLM 调用；mock build_openai_client 与线程池验证完整链路。
"""

import shutil
from pathlib import Path

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.plugins.managers.plugin_config_store import PluginConfigStore

PLUGIN_SRC = Path("C:/Users/black/.drifox/plugins/prompt-enhancer")
PLUGIN_NAME = "prompt-enhancer"


@pytest.fixture()
def fresh_ui_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def copied_plugin(tmp_path):
    dst = tmp_path / PLUGIN_NAME
    shutil.copytree(PLUGIN_SRC, dst)
    return dst


def test_ui_loads_input_button(fresh_ui_registry, copied_plugin):
    reg = fresh_ui_registry
    assert reg.load_plugin(PLUGIN_NAME, copied_plugin) is True
    buttons = reg.get_input_buttons()
    assert [b.button_id for b in buttons] == ["enhance"]
    assert buttons[0].tooltip == "优化提示词（LLM 一键增强）"
    # 卸载幂等
    assert reg.unload_plugin(PLUGIN_NAME) is True
    assert reg.get_input_buttons() == []


def test_config_schema_registers(copied_plugin):
    from app.plugins.managers.plugin_manager import PluginManager

    pm = PluginManager()
    pm._scan_one_plugin_dir(copied_plugin, "user")

    schema = PluginConfigRegistry.get_instance().get(PLUGIN_NAME)
    assert schema is not None
    assert schema.title == "提示词增强"
    field = schema.get_field("enhance_prompt")
    assert field is not None
    assert field.type == "text"
    # 默认生效（三级链：schema 默认）
    val = PluginConfigStore().get(PLUGIN_NAME, "enhance_prompt")
    assert val and "提示词优化专家" in val
    # 清理
    PluginConfigRegistry.get_instance().unregister_plugin(PLUGIN_NAME)
    assert PluginConfigRegistry.get_instance().get(PLUGIN_NAME) is None


def test_on_enhance_clicked_injects(fresh_ui_registry, copied_plugin, monkeypatch):
    reg = fresh_ui_registry
    assert reg.load_plugin(PLUGIN_NAME, copied_plugin) is True

    # InfoBar 在测试中非 QWidget parent，no-op 避免依赖 QApplication
    class _NoOpInfoBar:
        @staticmethod
        def info(*a, **k):
            pass

        @staticmethod
        def success(*a, **k):
            pass

        @staticmethod
        def warning(*a, **k):
            pass

        @staticmethod
        def error(*a, **k):
            pass

    monkeypatch.setattr("qfluentwidgets.InfoBar", _NoOpInfoBar)

    captured = {}

    class FakeMsg:
        content = "优化后的提示词"

    class FakeChoice:
        message = FakeMsg()

    class FakeResp:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            captured["model"] = kwargs.get("model")
            captured["messages"] = kwargs.get("messages")
            return FakeResp()

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self):
            self.chat = FakeChat()

    def fake_build(api_key, base_url):
        captured["api_key"] = api_key
        captured["base_url"] = base_url
        return FakeClient()

    import app.utils.http_client as hc

    monkeypatch.setattr(hc, "build_openai_client", fake_build)

    class FakeInputArea:
        def __init__(self, text):
            self._t = text
            self.set_plain = None

        def toPlainText(self):
            return self._t

        def setPlainText(self, t):
            self.set_plain = t

    class SyncPool:
        def start(self, task):
            task.run()

    class FakeMainWidget:
        _valid_configs = {
            "系统默认配置": {
                "API_KEY": "sk-test",
                "API_URL": "https://api.test",
                "模型名称": "gpt-4o",
            }
        }
        _current_provider_name = "系统默认配置"
        _gen_thread_pool = SyncPool()
        input_area = FakeInputArea("帮我写个爬虫")

    mw = FakeMainWidget()
    btn = [b for b in reg.get_input_buttons() if b.button_id == "enhance"][0]
    btn.on_click(
        {
            "button_id": "enhance",
            "plugin_name": PLUGIN_NAME,
            "window_id": "w1",
            "main_widget": mw,
        }
    )

    # 回注结果
    assert mw.input_area.set_plain == "优化后的提示词"
    # LLM 调用参数正确（复用主程序模型配置）
    assert captured["api_key"] == "sk-test"
    assert captured["base_url"] == "https://api.test"
    assert captured["model"] == "gpt-4o"
    assert captured["messages"][1]["content"] == "帮我写个爬虫"
    assert "提示词优化专家" in captured["messages"][0]["content"]
