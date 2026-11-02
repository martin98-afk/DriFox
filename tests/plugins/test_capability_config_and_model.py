# -*- coding: utf-8 -*-
"""config.write 与 model.config_for 的 provider 行为

config.write：只写已声明 ConfigItem、密钥类字段拒绝、save 可关。
model.config_for：宿主解析器透传、缺失返回 None、异常吞成 None。
"""

import pytest

from app.plugins.capabilities.providers.ui_core import _config_write, _model_config_for


class _Item:
    def __init__(self, value):
        self.value = value


class _FakeSettings:
    def __init__(self):
        self.llm_subagent_default_model = _Item("")
        self.api_key = _Item("secret")
        self.saved = 0

    def save(self):
        self.saved += 1


@pytest.fixture()
def settings(monkeypatch):
    fake = _FakeSettings()
    monkeypatch.setattr("app.utils.config.Settings.get_instance", classmethod(lambda cls: fake))
    return fake


def test_config_write_sets_declared_item(settings):
    write = _config_write(host=None)

    assert write("llm_subagent_default_model", "OpenAI:gpt-4o") is True
    assert settings.llm_subagent_default_model.value == "OpenAI:gpt-4o"
    assert settings.saved == 1


def test_config_write_can_skip_save(settings):
    write = _config_write(host=None)

    assert write("llm_subagent_default_model", "x", save=False) is True
    assert settings.saved == 0


def test_config_write_rejects_secret_keys(settings):
    write = _config_write(host=None)

    assert write("api_key", "leak") is False
    assert settings.api_key.value == "secret"


def test_config_write_rejects_undeclared_keys(settings):
    write = _config_write(host=None)

    assert write("not_a_config_item", 1) is False
    assert write("", 1) is False


class _Host:
    def __init__(self, result=None, raise_exc=False):
        self.calls = []
        self._result = result
        self._raise = raise_exc

    def _resolve_model_config_by_query(self, query, show_error=True):
        self.calls.append((query, show_error))
        if self._raise:
            raise RuntimeError("boom")
        return self._result


def test_model_config_for_delegates_to_host():
    host = _Host(result={"模型名称": "gpt-4o"}, raise_exc=False)

    assert _model_config_for(host)("gpt-4o") == {"模型名称": "gpt-4o"}
    assert host.calls == [("gpt-4o", False)]


def test_model_config_for_returns_none_without_resolver():
    assert _model_config_for(object())("gpt-4o") is None


def test_model_config_for_swallows_exception():
    assert _model_config_for(_Host(raise_exc=True))("x") is None
    assert _model_config_for(_Host(result={"a": 1}))("") is None
