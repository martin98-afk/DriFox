# -*- coding: utf-8 -*-
"""ui_click 两步确认协议测试（S3c-r）：token 缺失/过期/不匹配拒、黑名单拒、正常通过。"""

from __future__ import annotations

import json

import pytest


class _FakeRegistry:
    def __init__(self):
        self.registered: list[str] = []

    def register(self, name, schema, **kwargs):
        self.registered.append(name)


@pytest.fixture()
def mod_and_reg(monkeypatch, tmp_path):
    """隔离数据目录 + 全闸打开 → 返回 (模块, 已注册 5 工具的 registry)。"""
    import importlib

    monkeypatch.setenv("DRIFOX_DATA_DIR", str(tmp_path))
    cfg_dir = tmp_path / "plugin_data" / "ui-driver"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    monkeypatch.setenv("DRIFOX_UI_DRIVER", "1")
    mod = importlib.import_module("plugins.ui-driver.tools.ui_tools")
    importlib.reload(mod)
    reg = _FakeRegistry()
    mod.register(reg)
    assert len(reg.registered) == 5, f"前置失败：注册了 {reg.registered}"
    return mod, reg


def _parse(result_json: str) -> dict:
    return json.loads(result_json)


def test_click_without_token_rejected(mod_and_reg):
    mod, _reg = mod_and_reg
    out = _parse(mod._ui_click_impl(None, objectName="send_btn"))
    assert out["ok"] is False and out["data"]["blocked_by"] == "missing_token"


def test_click_expired_token_rejected(mod_and_reg):
    import time

    mod, _reg = mod_and_reg
    mod._tokens["tok_expired"] = {"objectName": "send_btn", "expires": time.time() - 1}
    out = _parse(mod._ui_click_impl(None, objectName="send_btn", confirm_token="tok_expired"))
    assert out["ok"] is False and out["data"]["blocked_by"] == "expired_token"


def test_click_token_mismatch_rejected(mod_and_reg):
    import time

    mod, _reg = mod_and_reg
    mod._tokens["tok_other"] = {"objectName": "other_btn", "expires": time.time() + 60}
    out = _parse(mod._ui_click_impl(None, objectName="send_btn", confirm_token="tok_other"))
    assert out["ok"] is False and out["data"]["blocked_by"] == "token_mismatch"


@pytest.mark.parametrize(
    "target",
    ["删除按钮", "remove_item", "Clear All", "解散团队", "撤回消息"],
)
def test_click_dangerous_keyword_rejected(mod_and_reg, target):
    mod, _reg = mod_and_reg
    out = _parse(mod._ui_click_impl(None, text=target, confirm_token="tok_any"))
    assert out["ok"] is False and out["data"]["blocked_by"] == "dangerous_keyword"


def test_click_with_valid_token_passes_guard(mod_and_reg, monkeypatch):
    """合法 token 通过全部闸（click 环节 mock，闸放行 + 一次性消费由本用例断言）。"""
    mod, _reg = mod_and_reg
    import time

    import tools.ui_driver as driver_mod

    token = "tok_valid"
    mod._tokens[token] = {"objectName": "send_btn", "expires": time.time() + 60}
    monkeypatch.setattr(driver_mod, "click", lambda widget: True)

    class _FakeWidget:
        def objectName(self):
            return "send_btn"

    monkeypatch.setattr(mod, "_driver_find", lambda selector, root=None: _FakeWidget())
    out = _parse(mod._ui_click_impl(None, objectName="send_btn", confirm_token=token))
    assert out["ok"] is True and out["data"]["clicked"] is True
    # 一次性：消费即作废
    assert token not in mod._tokens
