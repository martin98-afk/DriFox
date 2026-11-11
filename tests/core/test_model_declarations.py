# -*- coding: utf-8 -*-
"""模型能力声明（声明_* 六键）读写纯函数回归。

覆盖 model_declarations：
1. read_declarations 只返回已声明键（空值/缺键 = 未声明）
2. 旧格式（纯模型名键）读兜底
3. apply_declarations 写入 / 单键删除 / 全空删条目
4. 其他模型参数（温度等）不被触碰
5. 旧格式六键一并清理，其余键保留
6. describe_source 文案口径
7. 协议入口读写（同条目、空值删键、旧格式清理）
"""

from app.core.modelmeta.model_declarations import (
    apply_declarations,
    apply_protocol_entry,
    describe_source,
    override_key,
    read_declarations,
    read_protocol_entry,
)

_PROVIDER = "CodeBuddy"
_MODEL = "hy4-preview"
_KEY = f"{_PROVIDER}||{_MODEL}"


def test_override_key_format():
    assert override_key(_PROVIDER, _MODEL) == "CodeBuddy||hy4-preview"


def test_read_declarations_subset_only():
    """只返回六键中已声明的部分，空串/None/空列表视为未声明"""
    overrides = {
        _KEY: {
            "声明_上下文长度": 1024000,
            "声明_支持图像": True,
            "声明_支持思考": "",
            "声明_思考强度": [],
            "温度": 0.3,
        }
    }
    declared = read_declarations(overrides, _PROVIDER, _MODEL)
    assert declared == {"声明_上下文长度": 1024000, "声明_支持图像": True}


def test_read_declarations_legacy_model_key():
    """旧格式（纯模型名键）仍可读出声明"""
    overrides = {_MODEL: {"声明_支持思考": True}}
    assert read_declarations(overrides, _PROVIDER, _MODEL) == {"声明_支持思考": True}


def test_apply_declarations_writes_entry():
    updated = apply_declarations({}, _PROVIDER, _MODEL, {"声明_上下文长度": 512000, "声明_支持思考": False})
    assert updated[_KEY] == {"声明_上下文长度": 512000, "声明_支持思考": False}


def test_apply_declarations_removes_single_key():
    """六键全量覆盖：declared 里缺席的声明键被删除，传入的保留"""
    overrides = {_KEY: {"声明_上下文长度": 512000, "声明_支持思考": True}}
    updated = apply_declarations(overrides, _PROVIDER, _MODEL, {"声明_支持思考": True})
    assert updated[_KEY] == {"声明_支持思考": True}


def test_apply_declarations_drops_empty_entry():
    """六键全空且无其他参数 → 删除整个条目"""
    overrides = {_KEY: {"声明_支持思考": True}}
    assert apply_declarations(overrides, _PROVIDER, _MODEL, {}) == {}


def test_apply_declarations_keeps_other_params():
    """同一条目里的模型参数（温度等）不被声明编辑触碰"""
    overrides = {_KEY: {"温度": 0.3, "声明_支持思考": True}}
    updated = apply_declarations(overrides, _PROVIDER, _MODEL, {"声明_支持思考": None})
    assert updated[_KEY] == {"温度": 0.3}


def test_apply_declarations_cleans_legacy_declarations():
    """旧格式条目里的六键一并清理，其余参数保留（否则重置会被旧键复活）"""
    overrides = {_MODEL: {"声明_支持思考": True, "top_p": 0.8}}
    updated = apply_declarations(overrides, _PROVIDER, _MODEL, {})
    assert updated == {_MODEL: {"top_p": 0.8}}


def test_apply_declarations_cleans_legacy_only_declarations():
    """旧格式条目只有声明键 → 整个旧条目删除"""
    overrides = {_MODEL: {"声明_支持思考": True}}
    assert apply_declarations(overrides, _PROVIDER, _MODEL, {}) == {}


def test_describe_source():
    assert describe_source("declared") == "自定义"
    assert describe_source("caps") == "模型能力"
    assert describe_source("family") == "服务商族"
    assert describe_source("default") == "默认值"
    assert describe_source(None) == ""
    assert describe_source("unknown") == ""


# ---------- 协议入口（模型级路由参数，与六键同条目） ----------
def test_protocol_entry_roundtrip():
    """写入 responses → 读回；与已保存的声明共存同一 entry"""
    overrides = {_KEY: {"声明_支持思考": True}}
    updated = apply_protocol_entry(overrides, _PROVIDER, _MODEL, "responses")
    assert updated[_KEY] == {"声明_支持思考": True, "协议入口": "responses"}
    assert read_protocol_entry(updated, _PROVIDER, _MODEL) == "responses"


def test_protocol_entry_auto_removes_key():
    """空值 / auto = 删键（回到自动判定），其余参数保留"""
    overrides = {_KEY: {"协议入口": "responses", "温度": 0.3}}
    for empty in (None, "", "auto"):
        updated = apply_protocol_entry(overrides, _PROVIDER, _MODEL, empty)
        assert updated[_KEY] == {"温度": 0.3}, f"{empty!r} 应删键"
        assert read_protocol_entry(updated, _PROVIDER, _MODEL) == ""


def test_protocol_entry_only_key_drops_entry():
    """条目只有协议入口 → 改回自动后整条删除（不留空壳）"""
    overrides = {_KEY: {"协议入口": "transport:openai_chat"}}
    assert apply_protocol_entry(overrides, _PROVIDER, _MODEL, "") == {}


def test_protocol_entry_cleans_legacy_key():
    """旧格式（纯模型名）里的协议入口一并清理，其余参数保留"""
    overrides = {_MODEL: {"协议入口": "responses", "top_p": 0.8}}
    updated = apply_protocol_entry(overrides, _PROVIDER, _MODEL, "")
    assert updated == {_MODEL: {"top_p": 0.8}}

