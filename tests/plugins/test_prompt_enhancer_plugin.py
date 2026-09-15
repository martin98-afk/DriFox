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

PLUGIN_SRC = Path.home() / ".drifox" / "plugins" / "prompt-enhancer"
PLUGIN_NAME = "prompt-enhancer"

pytestmark = pytest.mark.skipif(
    not PLUGIN_SRC.is_dir(), reason=f"未安装 prompt-enhancer 插件: {PLUGIN_SRC}"
)


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
    assert field.type == "textarea"
    # 默认生效（三级链：schema 默认）
    val = PluginConfigStore().get(PLUGIN_NAME, "enhance_prompt")
    assert val and "提示词优化专家" in val
    # 清理
    PluginConfigRegistry.get_instance().unregister_plugin(PLUGIN_NAME)
    assert PluginConfigRegistry.get_instance().get(PLUGIN_NAME) is None


def test_on_enhance_clicked_injects(fresh_ui_registry, copied_plugin, monkeypatch):
    reg = fresh_ui_registry
    assert reg.load_plugin(PLUGIN_NAME, copied_plugin) is True

    # 插件配置隔离：不读本机 plugin_data（否则 enhance_model 会把用例变成环境相关）
    monkeypatch.setattr(PluginConfigStore, "get", lambda self, *a, **k: None)

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

    # 宿主服务面（真实主程序经 context["services"] 注入 get_provider_config，
    # 读内存态已解锁明文；插件不得自行读 app.config）
    services = {
        "get_provider_config": lambda provider="", model="": {
            "API_KEY": "sk-plain-from-host",
            "API_URL": "https://api.test",
            "模型名称": "gpt-4o",
        }
    }

    mw = FakeMainWidget()
    btn = [b for b in reg.get_input_buttons() if b.button_id == "enhance"][0]
    btn.on_click(
        {
            "button_id": "enhance",
            "plugin_name": PLUGIN_NAME,
            "window_id": "w1",
            "main_widget": mw,
            "services": services,
        }
    )

    # 回注结果：追加到原问题之后（保留原问题，与插件 docstring 契约一致）
    assert mw.input_area.set_plain == "帮我写个爬虫\n\n优化后的提示词"
    # LLM 调用参数来自宿主服务面（明文 key + 端点 + 模型）
    assert captured["api_key"] == "sk-plain-from-host"
    assert captured["base_url"] == "https://api.test"
    assert captured["model"] == "gpt-4o"
    assert captured["messages"][1]["content"] == "帮我写个爬虫"
    # 增强指令来自插件默认（本用例配置已隔离）
    assert captured["messages"][0]["content"]


def test_llm_config_falls_back_to_memory_not_disk_ciphertext(copied_plugin):
    """回归：无 services 时回退读内存态 _valid_configs，绝不读磁盘密文。

    旧实现 `_get_provider_config_by_name` 直接 json.load(app.config)，密钥模式下
    拿到 enc:v2: 密文当 Bearer token → 401 "Missing API key"。
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("_pe_ui_probe", copied_plugin / "ui" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 磁盘上放一份密文（模拟 password 模式下的 app.config），
    # 插件若走磁盘就读到它 —— 本用例断言绝不发生
    fake_cfg = copied_plugin / "fake_app.config"
    fake_cfg.write_text(
        '{"LLM": {"SavedProviders": {"cid1": {"provider_name": "MiniMax", '
        '"API_URL": "https://api.example.com/v1", "API_KEY": "enc:v2:CIPHERTEXT", '
        '"模型名称": "MiniMax-M3"}}}}',
        encoding="utf-8",
    )
    mod._SYSTEM_CONFIG_CACHE.update({"path": None, "mtime": 0.0, "data": None})
    mod._DEFAULT_SYSTEM_CONFIG_PATHS = (str(fake_cfg),)

    class FakeMainWidget:
        _valid_configs = {
            "cid1": {
                "provider_name": "MiniMax",
                "display_name": "MiniMax",
                "API_URL": "https://api.example.com/v1",
                "API_KEY": "plain-from-memory",
                "模型名称": "MiniMax-M3",
            }
        }
        _current_provider_name = "cid1"
        _current_model_name = "MiniMax-M3"

    cfg = mod._get_llm_config(FakeMainWidget(), override_provider="MiniMax", override_model="MiniMax-M2.7")
    assert cfg is not None
    assert cfg["API_KEY"] == "plain-from-memory"
    assert not str(cfg["API_KEY"]).startswith("enc:v2:")
    assert cfg["模型名称"] == "MiniMax-M2.7"

    # 未知 provider 不得静默串到别的服务商
    assert mod._get_llm_config(FakeMainWidget(), override_provider="不存在") is None
