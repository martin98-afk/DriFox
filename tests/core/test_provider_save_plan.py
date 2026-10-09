# -*- coding: utf-8 -*-
"""服务商保存计划纯函数（P1-11）单元测试 —— 无 Qt 依赖

覆盖 build_provider_save_plan 与 collect_extra_fields 的规则：
- confirm_clear 触发 / 不触发
- payload 五键恒存在（含空串）
- extra_fields 透传（空串覆盖）
- config_id 保留 / 新建态不写
- 模型列表空串（显式清空）
"""

from app.core.modelmeta.provider_save_plan import (
    FORM_KEYS,
    build_provider_save_plan,
    collect_extra_fields,
)


def _form(**overrides):
    base = {
        "api_url": "https://api.example.com/v1",
        "api_key": "sk-1",
        "model": "m1",
        "auth_type": "bearer",
        "name": "我的配置",
        "models": ["m1", "m2"],
    }
    base.update(overrides)
    return base


# ══════════════════════════════════════════════════════════════════
# confirm_clear
# ══════════════════════════════════════════════════════════════════


def test_confirm_clear_triggered_when_clearing_nonempty_list():
    """清空列表 + 旧列表非空 → 需要确认"""
    plan = build_provider_save_plan(_form(models=[]), {"模型列表": ["m1"]}, {})
    assert plan["confirm_clear"] is True


def test_confirm_clear_not_triggered_when_old_list_empty():
    """旧列表本来就空 → 不需要确认（没有「恢复旧数据」的风险）"""
    plan = build_provider_save_plan(_form(models=[]), {"模型列表": []}, {})
    assert plan["confirm_clear"] is False


def test_confirm_clear_not_triggered_when_models_present():
    """列表非空 → 不需要确认"""
    assert build_provider_save_plan(_form(), {"模型列表": ["m0"]}, {})["confirm_clear"] is False


def test_confirm_clear_when_old_info_lacks_models_key():
    """旧配置无「模型列表」键 → 视为空，不触发确认"""
    assert build_provider_save_plan(_form(models=[]), {}, {})["confirm_clear"] is False


# ══════════════════════════════════════════════════════════════════
# payload 组装
# ══════════════════════════════════════════════════════════════════


def test_payload_five_keys_always_present_even_empty():
    """五键恒存在：表单值全空也要写出空串（显式清空），不得省略键"""
    plan = build_provider_save_plan(
        _form(api_url="", api_key="", model="", auth_type="", name=""),
        {},
        {},
    )
    payload = plan["payload"]
    for key in FORM_KEYS:
        assert key in payload, f"五键必须恒存在，缺 {key}"
        assert payload[key] == ""


def test_payload_strips_whitespace():
    """表单值应去除首尾空白（与改动前的 .strip() 行为一致）"""
    payload = build_provider_save_plan(
        _form(api_url="  https://x/v1  ", api_key=" sk-2 ", model=" m2 ", name=" 名字 "),
        {},
        {},
    )["payload"]
    assert payload["API_URL"] == "https://x/v1"
    assert payload["API_KEY"] == "sk-2"
    assert payload["模型名称"] == "m2"
    assert payload["name"] == "名字"


def test_payload_extra_fields_pass_through_including_empty():
    """extra_fields 透传：空串同样是有效值（覆盖语义），不被丢弃"""
    payload = build_provider_save_plan(_form(), {}, {"server_id": "", "cookie": "abc"})["payload"]
    assert payload["server_id"] == "", "空串必须透传（清空生效）"
    assert payload["cookie"] == "abc"


def test_payload_config_id_preserved_in_edit_mode():
    """编辑态：旧 config_id 写入 payload（供 apply_provider_save 判断是否改过 KEY）"""
    payload = build_provider_save_plan(_form(), {"config_id": "abc12345"}, {})["payload"]
    assert payload["config_id"] == "abc12345"


def test_payload_config_id_absent_for_new_provider():
    """新建态：旧信息无 config_id → payload 不写该键（防止伪造陈旧 id）"""
    payload = build_provider_save_plan(_form(), {}, {})["payload"]
    assert "config_id" not in payload


def test_payload_models_empty_list_is_explicit():
    """模型列表恒写入：空列表也是显式值（不是「省略键」）"""
    payload = build_provider_save_plan(_form(models=[]), {"模型列表": ["m1"]}, {})["payload"]
    assert payload["模型列表"] == []


def test_payload_models_copied_not_aliased():
    """模型列表应复制：调用方后续改动自己的列表不应影响 payload"""
    models = ["m1"]
    payload = build_provider_save_plan(_form(models=models), {}, {})["payload"]
    models.append("m2")
    assert payload["模型列表"] == ["m1"]


# ══════════════════════════════════════════════════════════════════
# collect_extra_fields
# ══════════════════════════════════════════════════════════════════


class _FakeEditor:
    def __init__(self, text: str):
        self._text = text

    def text(self) -> str:
        return self._text


def test_collect_extra_fields_only_current_provider():
    """只收当前服务商的行；别的服务商同名键不写入"""
    rows = {
        ("目标服务商", "server_id"): (None, "e1"),
        ("其他服务商", "server_id"): (None, "e2"),
        ("其他服务商", "cookie"): (None, "e3"),
    }
    editors = {"e1": _FakeEditor("keep"), "e2": _FakeEditor("x"), "e3": _FakeEditor("y")}
    out = collect_extra_fields("目标服务商", rows.items(), lambda a: editors.get(a))
    assert out == {"server_id": "keep"}


def test_collect_extra_fields_empty_string_kept():
    """有编辑器 + 值为空 → 仍写入空串（P0-6：清空是显式意图）"""
    rows = {("目标服务商", "server_id"): (None, "e1")}
    out = collect_extra_fields("目标服务商", rows.items(), lambda a: _FakeEditor(""))
    assert out == {"server_id": ""}


def test_collect_extra_fields_missing_editor_skipped():
    """编辑器缺失（行未创建）→ 不写入，交给 merge 保留旧值"""
    rows = {("目标服务商", "server_id"): (None, "e1")}
    out = collect_extra_fields("目标服务商", rows.items(), lambda a: None)
    assert out == {}


def test_collect_extra_fields_strips_values():
    """值应去除首尾空白"""
    rows = {("目标服务商", "cookie"): (None, "e1")}
    out = collect_extra_fields("目标服务商", rows.items(), lambda a: _FakeEditor("  abc  "))
    assert out == {"cookie": "abc"}
