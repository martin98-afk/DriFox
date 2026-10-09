# -*- coding: utf-8 -*-
"""服务商列表行状态点 / 副标题 / tooltip 改版回归测试（波7 段三）

覆盖：
1. 状态点三色（ok=绿 / unreachable=灰 / auth_failed=红）+ 无点场景
2. 副标题格式「{模型名称} · {N} 个模型」+ 「模型列表」键缺失时退化
3. tooltip 相对时间（自动刷新开关状态 + 上次刷新）
"""

import sys
from pathlib import Path

import pytest
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QLabel

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


def _make_item(info: dict, qapp=None):
    from app.widgets.cards.settings.provider_setting_card import ProviderItem

    return ProviderItem("cid_1", dict(info), False)


class TestStatusDot:
    def test_ok_green_dot(self, _qapp, registry):
        item = _make_item(
            {
                "provider_name": "测试服务商",
                "API_URL": "https://api.example.com/v1",
                "模型刷新状态": "ok",
            }
        )
        assert hasattr(item, "statusDot"), "有状态 + 有刷新能力应画点"
        assert "#2ecc71" in item.statusDot.styleSheet()

    def test_unreachable_gray_dot(self, _qapp, registry):
        item = _make_item(
            {
                "provider_name": "测试服务商",
                "API_URL": "https://api.example.com/v1",
                "模型刷新状态": "unreachable",
            }
        )
        assert "#95a5a6" in item.statusDot.styleSheet()

    def test_auth_failed_red_dot(self, _qapp, registry):
        item = _make_item(
            {
                "provider_name": "测试服务商",
                "API_URL": "https://api.example.com/v1",
                "模型刷新状态": "auth_failed",
            }
        )
        assert "#e74c3c" in item.statusDot.styleSheet()

    def test_no_dot_when_status_missing(self, _qapp, registry):
        """无「模型刷新状态」键 → 不画点"""
        item = _make_item({"provider_name": "测试服务商", "API_URL": "https://api.example.com/v1"})
        assert not hasattr(item, "statusDot")

    def test_no_dot_when_no_refresh_capability(self, _qapp, registry):
        """有状态但无刷新能力（无 hook 且无 API_URL）→ 不画点"""
        item = _make_item({"provider_name": "测试服务商", "模型刷新状态": "ok"})
        assert not hasattr(item, "statusDot")

    def test_dot_when_hook_present_without_url(self, _qapp, monkeypatch):
        """有 models_hook 时即使无 API_URL 也画点（hook 可刷新）"""
        reg = ProviderRegistry()
        reg.register(
            ProviderDef(
                name="钩子服务商",
                capabilities={"models_hook": lambda cfg: ["m"]},
            ),
            source="plugin:test",
        )
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

        item = _make_item({"provider_name": "钩子服务商", "模型刷新状态": "ok"})
        assert hasattr(item, "statusDot")


class TestSubtitle:
    def test_subtitle_with_model_count(self, _qapp, registry):
        """有「模型列表」→ 「{模型名称} · {N} 个模型」"""
        item = _make_item(
            {
                "provider_name": "测试服务商",
                "API_URL": "https://api.example.com/v1",
                "模型名称": "gpt-x",
                "模型列表": ["a", "b", "c"],
            }
        )
        assert item.modelLabel.text() == "gpt-x · 3 个模型"

    def test_subtitle_without_model_list_key(self, _qapp, registry):
        """「模型列表」键缺失（词典兜底态）→ 只显示模型名"""
        item = _make_item(
            {
                "provider_name": "测试服务商",
                "API_URL": "https://api.example.com/v1",
                "模型名称": "gpt-x",
            }
        )
        assert item.modelLabel.text() == "gpt-x"

    def test_subtitle_empty_list_counts_zero(self, _qapp, registry):
        """空列表是显式值（0 个模型）→ 显示计数"""
        item = _make_item(
            {
                "provider_name": "测试服务商",
                "API_URL": "https://api.example.com/v1",
                "模型名称": "gpt-x",
                "模型列表": [],
            }
        )
        assert item.modelLabel.text() == "gpt-x · 0 个模型"

    def test_subtitle_empty_model_name(self, _qapp, registry):
        """无模型名但有列表 → 只显示计数"""
        item = _make_item(
            {"provider_name": "测试服务商", "API_URL": "https://api.example.com/v1", "模型列表": ["a"]}
        )
        assert item.modelLabel.text() == "1 个模型"


class TestTooltip:
    def test_tooltip_contains_switch_state_and_relative_time(self, _qapp, registry):
        item = _make_item(
            {
                "provider_name": "测试服务商",
                "API_URL": "https://api.example.com/v1",
                "模型刷新状态": "ok",
                "自动刷新模型": True,
                "上次模型刷新": "2026-10-09 12:00:00",
            }
        )
        tip = item.statusDot.toolTip()
        assert "自动刷新：开" in tip
        assert "上次刷新" in tip
        assert "从未" not in tip, "有时间戳时不应显示「从未」"

    def test_tooltip_never_when_no_timestamp(self, _qapp, registry):
        item = _make_item(
            {
                "provider_name": "测试服务商",
                "API_URL": "https://api.example.com/v1",
                "模型刷新状态": "ok",
            }
        )
        tip = item.statusDot.toolTip()
        assert "自动刷新：关" in tip
        assert "从未" in tip
