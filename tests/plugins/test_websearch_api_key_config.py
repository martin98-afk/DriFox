# -*- coding: utf-8 -*-
"""websearch 工具插件配置测试：两个 API key 注册到插件自包含配置（主程序零改动）

不变量：
- set_api_key_config 注册 → 持久化到用户数据目录 → get_api_key_config 读回
- _api_key 读取优先级：环境变量 > 插件配置 > 插件内置默认值
- 配置为空时回退内置默认（行为零变化）
"""

import pytest

from plugins.system.tools import web_tools


@pytest.fixture()
def isolated_config(monkeypatch, tmp_path):
    """隔离配置路径：monkeypatch get_app_data_dir → tmp_path"""
    monkeypatch.setattr(
        "app.utils.utils.get_app_data_dir",
        lambda: tmp_path,
    )
    # 环境变量隔离：清除两个 key（teardown 自动恢复）
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    return tmp_path


# ---------- 注册 / 读取 ----------


def test_set_and_get_config(isolated_config, tmp_path):
    """注册两个 key → 配置文件落盘 → 读回一致"""
    assert web_tools.set_api_key_config(tavily_api_key="tvly-cfg-1", tinyfish_api_key="tf-cfg-1") is True

    cfg = web_tools.get_api_key_config()
    assert cfg == {"tavily_api_key": "tvly-cfg-1", "tinyfish_api_key": "tf-cfg-1"}

    # 配置文件实际落盘（用户数据目录 tools/web_search_keys.json）
    config_file = tmp_path / "tools" / web_tools._CONFIG_FILENAME
    assert config_file.exists()
    import json

    assert json.loads(config_file.read_text(encoding="utf-8"))["tavily_api_key"] == "tvly-cfg-1"


def test_set_override_idempotent(isolated_config):
    """重复注册覆盖（幂等）；只注册一个 key 时另一个保持"""
    web_tools.set_api_key_config(tavily_api_key="tvly-1")
    web_tools.set_api_key_config(tavily_api_key="tvly-2", tinyfish_api_key="tf-2")
    cfg = web_tools.get_api_key_config()
    assert cfg["tavily_api_key"] == "tvly-2"
    assert cfg["tinyfish_api_key"] == "tf-2"


def test_config_empty_by_default(isolated_config):
    """未注册 → 配置为空 dict（回退默认值路径）"""
    assert web_tools.get_api_key_config() == {"tavily_api_key": "", "tinyfish_api_key": ""}


# ---------- 读取优先级 ----------


def test_api_key_priority_config_over_default(isolated_config):
    """注册配置后 _api_key 返回配置值（覆盖内置默认）"""
    web_tools.set_api_key_config(tavily_api_key="tvly-cfg", tinyfish_api_key="tf-cfg")
    assert web_tools._api_key(None, "TAVILY_API_KEY") == "tvly-cfg"
    assert web_tools._api_key(None, "TINYFISH_API_KEY") == "tf-cfg"


def test_api_key_priority_env_over_config(isolated_config, monkeypatch):
    """环境变量 > 插件配置（最高优先级）"""
    web_tools.set_api_key_config(tavily_api_key="tvly-cfg")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env")
    assert web_tools._api_key(None, "TAVILY_API_KEY") == "tvly-env"
    # TinyFish 未设环境变量 → 走配置/默认
    web_tools.set_api_key_config(tinyfish_api_key="tf-cfg")
    assert web_tools._api_key(None, "TINYFISH_API_KEY") == "tf-cfg"


def test_api_key_fallback_default(isolated_config):
    """环境变量与配置均无 → 回退插件内置默认值（行为零变化）"""
    assert web_tools._api_key(None, "TAVILY_API_KEY") == web_tools._DEFAULT_TAVILY_KEY
    assert web_tools._api_key(None, "TINYFISH_API_KEY") == web_tools._DEFAULT_TINYFISH_KEY


def test_corrupt_config_safe(isolated_config, tmp_path):
    """配置文件损坏 → 读回空配置（不炸，回退默认）"""
    config_file = tmp_path / "tools" / web_tools._CONFIG_FILENAME
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("{broken json", encoding="utf-8")
    assert web_tools.get_api_key_config() == {"tavily_api_key": "", "tinyfish_api_key": ""}


# ---------- 主程序零改动 ----------


def test_no_settings_involvement():
    """插件配置不依赖主程序 Settings/app.config（源码零改动断言）"""
    import inspect

    src = inspect.getsource(web_tools)
    assert "from app.utils.config import Settings" not in src
    assert "app.config" not in src


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
