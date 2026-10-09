# -*- coding: utf-8 -*-
"""编辑卡双开关（自动刷新 / 健康检查）回归测试（波7 段二）

红线背景
--------
两个开关是 ``SwitchButton``，其值必须走 `build_provider_save_plan` 的
``form_values``（bool 直传）。**绝不能进 extra_fields** —— 那条链对编辑器
统一调 ``.text()``，而 SwitchButton 没有该方法，会 AttributeError 炸在保存路径上。

本组锁定：
1. 回显旧值（True/False 都正确）
2. payload 恒写两 bool（含 False = 显式关）
3. 保存后键落 saved_providers
4. 反向断言：开关不进 extra_fields（防回归）
"""

import sys
from pathlib import Path

import pytest
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="module")
def _qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    return app


@pytest.fixture()
def registry(monkeypatch):
    reg = ProviderRegistry()
    reg.register(
        ProviderDef(name="测试服务商", api_url="https://api.example.com/v1", default_model="m1"),
        source="plugin:test",
    )
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def card(_qapp, registry):
    from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

    def _make(info: dict):
        return ProviderEditCard(provider_name="测试服务商", provider_info=dict(info), is_new=False)

    return _make


class TestSwitchEcho:
    def test_reads_true_values(self, card):
        """回显：配置里 True → 开关选中"""
        c = card({"provider_name": "测试服务商", "自动刷新模型": True, "健康检查": True})
        assert c.autoRefreshSwitch.isChecked() is True
        assert c.healthCheckSwitch.isChecked() is True

    def test_reads_false_and_missing(self, card):
        """回显：缺失键 / False → 未选中（不抛异常）"""
        c = card({"provider_name": "测试服务商"})
        assert c.autoRefreshSwitch.isChecked() is False
        assert c.healthCheckSwitch.isChecked() is False

        c2 = card({"provider_name": "测试服务商", "自动刷新模型": False, "健康检查": False})
        assert c2.autoRefreshSwitch.isChecked() is False


class TestSwitchPayload:
    def test_payload_always_writes_both_bools(self, card):
        """payload 恒写两 bool：显式 False 也要写入（否则关不掉）"""
        c = card({"provider_name": "测试服务商", "API_URL": "https://api.example.com/v1"})
        c.autoRefreshSwitch.setChecked(True)
        c.healthCheckSwitch.setChecked(False)

        captured = {}
        c.saved.connect(lambda name, info: captured.update(info=info))
        c._on_save()

        assert captured["info"]["自动刷新模型"] is True
        assert captured["info"]["健康检查"] is False, "False 必须写入（显式关）"

    def test_payload_omits_keys_absent_from_form(self, card):
        """未勾选时仍是 False（不是缺键）——经 plan 保证"""
        from app.core.modelmeta.provider_save_plan import build_provider_save_plan

        plan = build_provider_save_plan(
            {"api_url": "https://x", "api_key": "k", "model": "m", "auth_type": "bearer", "name": "n"},
            {},
            {},
        )
        assert plan["payload"]["自动刷新模型"] is False
        assert plan["payload"]["健康检查"] is False


class TestSwitchPersist:
    def test_switches_reach_saved_providers(self, card, registry):
        """保存后键落 saved_providers（经 apply_provider_save）"""
        from app.core.modelmeta.provider_profile import apply_provider_save, compute_provider_config_id

        info = {"provider_name": "测试服务商", "API_URL": "https://api.example.com/v1", "API_KEY": "sk"}
        info["config_id"] = compute_provider_config_id(info)
        c = card(info)
        c.healthCheckSwitch.setChecked(True)

        captured = {}
        c.saved.connect(lambda name, p: captured.update(info=p))
        c._on_save()

        saved = {info["config_id"]: dict(info)}
        cid = apply_provider_save(saved, captured["info"], "测试服务商", is_new=False)
        assert saved[cid]["健康检查"] is True
        assert saved[cid]["自动刷新模型"] is False


class TestSwitchNotInExtraFields:
    def test_switches_never_enter_extra_fields(self, card):
        """红线反向断言：extra_fields 只含 extra_quota_fields 的键，绝不含双开关

        collect_extra_fields 对编辑器调 .text()；SwitchButton 无该方法
        （若开关被误当额外字段，这条会 AttributeError 炸）。
        """
        from app.core.modelmeta.provider_save_plan import collect_extra_fields

        c = card({"provider_name": "测试服务商", "API_URL": "https://api.example.com/v1"})
        c.autoRefreshSwitch.setChecked(True)

        extra = collect_extra_fields(
            "测试服务商",
            c._extra_field_rows.items(),
            lambda attr: getattr(c, attr, None),
        )
        assert "自动刷新模型" not in extra
        assert "健康检查" not in extra
        # 且调用本身不抛（编辑器都是 LineEdit，有 .text()）
        assert isinstance(extra, dict)

    def test_switch_widgets_have_no_callable_text(self, card):
        """前提核实：SwitchButton 的 ``.text`` 不是可调用方法（红线成立的原因）

        实测：``hasattr(sw, "text")`` 为 True（QWidget 上是字符串属性），但
        ``sw.text()`` 抛 ``TypeError: 'str' object is not callable`` ——
        `collect_extra_fields` 统一调 ``.text()`` 会炸，故开关绝不能进 extra_fields。
        """
        c = card({"provider_name": "测试服务商"})
        sw = c.autoRefreshSwitch
        assert not callable(getattr(sw, "text", None)), "SwitchButton.text 不该是可调用方法"
        with pytest.raises(TypeError):
            sw.text()  # type: ignore[operator]
