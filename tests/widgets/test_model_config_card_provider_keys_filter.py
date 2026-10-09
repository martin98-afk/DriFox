# -*- coding: utf-8 -*-
"""回归：模型配置卡不得渲染服务商级管理键。

背景（泄漏事故）：ProviderEditCard 保存计划与 ModelRefreshService 会把
「模型列表 / 模型关闭列表 / 自动刷新模型 / 健康检查 / 上次模型刷新 /
模型刷新状态」写入 ``saved_providers[config_id]``。这些键属于**服务商管理**
而非模型参数，且没有 PARAM_SCHEMA 条目。

两道红线：
1. 不得出现在配置卡（无意义控件 + 用户困惑）
2. 即使漏渲染，也绝不能经 ``get_config`` 全量回传被写坏
   （bool 被 slider 拖动覆盖成 float / 时间戳被 LineEdit 改坏）

第 2 条是真正的数据安全线：``get_config`` 从 ``self.config.copy()`` 起步，
任何进入 config 的键都会被回传，故过滤必须发生在 ``set_config`` 入口。
"""

import pytest

from app.constants import PROVIDER_MANAGED_KEYS


@pytest.fixture()
def card(qapp):
    """qapp 用 tests/conftest.py 的 session 级共享单例。

    ⚠ 不要在本文件自建 module-scope QApplication：module 卸载时会销毁
    QApplication，连带带走 qfluentwidgets 的全局 QConfig 单例，导致同进程
    后续测试文件 `qconfig.themeChanged` 抛 "C/C++ object has been deleted"。
    """
    from app.widgets.cards.settings.model_config_card import ModelConfigCard

    return ModelConfigCard()


def _polluted_config(**overrides):
    """复刻事故现场：服务商配置叠加了管理键后的形态。"""
    base = {
        "温度": 0.7,
        "最大Token": 128000,
        "模型列表": ["gpt-4o", "claude-fable-5"],
        "模型关闭列表": [],
        "自动刷新模型": True,
        "健康检查": True,
        "上次模型刷新": "2026-10-09 23:16:58",
        "模型刷新状态": "unreachable",
    }
    base.update(overrides)
    return base


def test_managed_keys_not_rendered(card):
    """红线 1：六个管理键一个都不许渲染成控件"""
    card.set_config("GitHub Copilot", _polluted_config(), "gpt-4o")

    for key in PROVIDER_MANAGED_KEYS:
        assert key not in card._widgets, f"{key} 属于服务商管理键，不得渲染到模型配置卡"


def test_managed_keys_not_passthrough_on_save(card):
    """红线 2：管理键不得经 get_config 回传（防 bool 被覆盖成 float）"""
    card.set_config("GitHub Copilot", _polluted_config(), "gpt-4o")
    got = card.get_config()

    leaked = PROVIDER_MANAGED_KEYS & set(got)
    assert not leaked, f"管理键泄漏进保存回传，会写坏磁盘数据: {sorted(leaked)}"
    assert got["温度"] == 0.7, "常规模型参数不受影响"
    assert got["最大Token"] == 128000


def test_bool_not_rendered_as_slider(card):
    """bool 是 int 子类：schema 未收录的 bool 曾命中 slider 分支渲染成滑条。

    True == 1 落在 0~2 区间 → 渲染滑条 → 拖动存 0.5 之类 float 覆盖原 bool。
    """
    assert card._infer_fallback_type("某个开关", True) == "checkbox"
    assert card._infer_fallback_type("某个开关", False) == "checkbox"
    # 常规数值键不受影响
    assert card._infer_fallback_type("某个比例", 0.5) == "slider"
    assert card._infer_fallback_type("某个上限", 4096) == "spinbox"
    assert card._infer_fallback_type("某个文本", "abc") == "line"


def test_scalar_keys_still_distinguished(card):
    """修复不得误伤：非 bool 的 0/1/2 仍走 slider（语义是连续值）"""
    assert card._infer_fallback_type("某个权重", 1) == "slider"
    assert card._infer_fallback_type("某个权重", 2) == "slider"
