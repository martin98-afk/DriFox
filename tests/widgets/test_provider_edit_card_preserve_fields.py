# -*- coding: utf-8 -*-
"""P0-6 回归：编辑保存时保留未知键 + 用户清空的字段不被合并复活

修复背景
--------
`_on_save` 的 extra_fields 提取有 `if val:` 门槛：用户把某字段清空 → 该键不写入
payload → `apply_provider_save` 覆盖式赋值虽保住磁盘旧值，但语义是「清空失效」
（用户清完值又被旧值悄悄填回）。同时配置文件携带任何非白名单字段（如插件新增
元数据），编辑一次就丢。

修法：
1. `_on_save` 显式管理键恒写入（含空串）；非当前服务商的字段行完全不出现在 payload，
   交给 merge 保留旧值。
2. `apply_provider_save` 四处整体赋值改 merge（`{**existing, **provider_info}`）：
   显式含某键（含空串）→ 覆盖；未含 → 保留既有未知键。

本组锁定四条：
- 未知键保留（磁盘上的额外字段编辑后仍在）
- 清空不复活（清空后重读 == ""，不是旧值也不是 KeyError）
- 表单键被新值覆盖
- 新建态不报错且不引入脏键
"""

import sys
from pathlib import Path

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.modelmeta.provider_profile import apply_provider_save, compute_provider_config_id
from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


@pytest.fixture()
def registry(monkeypatch):
    """隔离注册表：注册一个带 extra_quota_field 的测试服务商（有编辑器）"""
    from app.plugins.registries.provider_registry import QuotaField

    reg = ProviderRegistry()
    reg.register(
        ProviderDef(
            name="测试服务商",
            api_url="https://api.example.com/v1",
            default_model="test-model",
            extra_quota_fields=[QuotaField(key="server_id", label="Server ID:", placeholder="")],
        ),
        source="plugin:test",
    )
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def card(registry, qapp, monkeypatch):
    """构造编辑卡（编辑态），屏蔽会阻塞的 UI 交互"""
    from app.widgets.cards.settings import provider_edit_card as mod

    def _make(provider_info: dict, is_new: bool = False):
        c = mod.ProviderEditCard(provider_name="测试服务商", provider_info=dict(provider_info), is_new=is_new)
        # 保存不需要真弹窗
        monkeypatch.setattr(c, "_show_info_bar", lambda *a, **k: None, raising=False)
        return c

    return _make


def _save(card) -> dict:
    """触发保存，返回 payload（不依赖外部信号连接）"""
    captured = {}
    card.saved.connect(lambda name, info: captured.update(name=name, info=info))
    card._on_save()
    return captured


# ══════════════════════════════════════════════════════════════════
# 1. 未知键保留
# ══════════════════════════════════════════════════════════════════


def test_unknown_fields_preserved_after_edit(card):
    """配置里携带的非白名单键（如插件新增元数据）编辑一次后仍在"""
    old_info = {
        "provider_name": "测试服务商",
        "API_URL": "https://api.example.com/v1",
        "API_KEY": "sk-old",
        "模型名称": "test-model",
        "认证方式": "bearer",
        "未来新元数据": "别丢我",
        "另一未知键": 123,
    }
    cid = compute_provider_config_id(old_info)
    old_info["config_id"] = cid
    c = card(old_info)
    saved_providers = {cid: dict(old_info)}

    captured = _save(c)
    payload = captured["info"]
    apply_provider_save(saved_providers, payload, "测试服务商", is_new=False)
    stored = saved_providers[cid]

    assert stored["未来新元数据"] == "别丢我", "未知键必须被 merge 保留"
    assert stored["另一未知键"] == 123


def test_cleared_field_stays_empty_not_resurrected(card):
    """关键回归：用户清空某字段 → 存盘后该键为空串，不得被旧值合并复活"""
    old_info = {
        "provider_name": "测试服务商",
        "API_URL": "https://api.example.com/v1",
        "API_KEY": "sk-old",
        "模型名称": "test-model",
        "认证方式": "bearer",
        "server_id": "old-server-id",
    }
    cid = compute_provider_config_id(old_info)
    old_info["config_id"] = cid
    c = card(old_info)
    # 用户清空该字段（有编辑器 → 应恒写入空串）
    editor = getattr(c, "quota_edit_测试服务商_server_id")
    editor.setText("")

    saved_providers = {cid: dict(old_info)}
    captured = _save(c)
    payload = captured["info"]
    apply_provider_save(saved_providers, payload, "测试服务商", is_new=False)
    stored = saved_providers[cid]

    assert "server_id" in stored, "清空是「显式置空」，键仍应在"
    assert stored["server_id"] == "", f"清空后应为空串，实际 {stored['server_id']!r}（旧值复活）"


# ══════════════════════════════════════════════════════════════════
# 2. 表单键被新值覆盖
# ══════════════════════════════════════════════════════════════════


def test_form_keys_overwritten_by_new_values(card):
    """五键表单应被新输入覆盖（用不改 (URL, KEY) 的字段，保持同一 config_id）

    注：API_KEY / API_URL 是 config_id 的 hash 输入，改了它们等于换条目
    （apply_provider_save 会删旧条目落新位置），那是另一条分支，本组不混测。
    """
    old_info = {
        "provider_name": "测试服务商",
        "API_URL": "https://api.example.com/v1",
        "API_KEY": "sk-old",
        "模型名称": "old-model",
        "认证方式": "bearer",
        "name": "旧配置名",
    }
    cid = compute_provider_config_id(old_info)
    old_info["config_id"] = cid  # 编辑态必带：让 apply_provider_save 认作同一条目
    c = card(old_info)
    # modelCombo.currentText() 取的是当前 index 的 item 文本，故必须先入列再定位
    c.modelCombo.addItem("new-model")
    c.modelCombo.setCurrentIndex(c.modelCombo.findText("new-model"))
    c.configNameEdit.setText("新配置名")

    saved_providers = {cid: dict(old_info)}
    captured = _save(c)
    new_id = apply_provider_save(saved_providers, captured["info"], "测试服务商", is_new=False)

    assert new_id == cid, "URL/KEY 未变，应原位更新同一条目"
    stored = saved_providers[cid]
    assert stored["模型名称"] == "new-model", "表单值应覆盖旧值"
    assert stored["name"] == "新配置名"
    assert stored["API_KEY"] == "sk-old"
    assert stored["API_URL"] == "https://api.example.com/v1"


def test_apikey_change_moves_to_new_entry(card):
    """对照：改了 API_KEY → config_id 变化 → 落到新条目（旧条目删除）"""
    old_info = {
        "provider_name": "测试服务商",
        "API_URL": "https://api.example.com/v1",
        "API_KEY": "sk-old",
        "模型名称": "test-model",
        "认证方式": "bearer",
    }
    cid = compute_provider_config_id(old_info)
    old_info["config_id"] = cid
    c = card(old_info)
    c.apiKeyEdit.setText("sk-new")

    saved_providers = {cid: dict(old_info)}
    captured = _save(c)
    new_id = apply_provider_save(saved_providers, captured["info"], "测试服务商", is_new=False)

    assert new_id != cid, "KEY 变了 → 新 hash"
    assert cid not in saved_providers, "旧条目应被删除"
    assert saved_providers[new_id]["API_KEY"] == "sk-new"


# ══════════════════════════════════════════════════════════════════
# 3. 新建态
# ══════════════════════════════════════════════════════════════════


def test_new_provider_save_does_not_raise(card):
    """新建态：保存不报错，且不引入「幽灵键」（未编辑的字段不进 payload）"""
    c = card({"API_URL": "", "API_KEY": "", "模型名称": ""}, is_new=True)
    c.apiKeyEdit.setText("sk-brand-new")
    c.modelCombo.setCurrentText("test-model")

    saved_providers = {}
    captured = _save(c)
    payload = captured["info"]
    new_id = apply_provider_save(saved_providers, payload, "测试服务商", is_new=True)

    assert new_id in saved_providers
    stored = saved_providers[new_id]
    assert stored["API_KEY"] == "sk-brand-new"
    assert stored["provider_name"] == "测试服务商"
    # 表单里没动过的配额字段：编辑器存在但未填 → 空串（显式管理键，不算幽灵）
    assert stored.get("server_id", "") == ""


# ══════════════════════════════════════════════════════════════════
# 4. merge 语义直测（不依赖 UI）
# ══════════════════════════════════════════════════════════════════


def test_apply_provider_save_merges_unknown_keys():
    """apply_provider_save 语义：未在 payload 中出现的既有键被保留"""
    existing_info = {
        "provider_name": "测试服务商",
        "API_URL": "https://api.example.com/v1",
        "API_KEY": "sk-1",
        "未知键A": "keep-me",
        "未知键B": [1, 2, 3],
    }
    cid = compute_provider_config_id(existing_info)
    existing_info["config_id"] = cid
    saved = {cid: dict(existing_info)}

    payload = {
        "provider_name": "测试服务商",
        "API_URL": "https://api.example.com/v1",
        "API_KEY": "sk-1",
        "config_id": cid,
        "模型名称": "m1",
    }
    apply_provider_save(saved, payload, "测试服务商", is_new=False)

    stored = saved[cid]
    assert stored["未知键A"] == "keep-me"
    assert stored["未知键B"] == [1, 2, 3]
    assert stored["模型名称"] == "m1"


def test_apply_provider_save_empty_string_overrides():
    """payload 显式带空串 → 覆盖旧值（清空语义不被打折）"""
    existing_info = {
        "provider_name": "测试服务商",
        "API_URL": "https://api.example.com/v1",
        "API_KEY": "sk-1",
        "server_id": "old-id",
    }
    cid = compute_provider_config_id(existing_info)
    existing_info["config_id"] = cid
    saved = {cid: dict(existing_info)}

    payload = {
        "config_id": cid,
        "provider_name": "测试服务商",
        # 真实 _on_save 的 payload 必带 URL/KEY（config_id 是二者的 hash，缺了就重算成随机值）
        "API_URL": "https://api.example.com/v1",
        "API_KEY": "sk-1",
        "server_id": "",
    }
    apply_provider_save(saved, payload, "测试服务商", is_new=False)

    assert saved[cid]["server_id"] == "", "空串是显式值，必须覆盖旧值"
    assert saved[cid]["API_URL"] == "https://api.example.com/v1", "未提及的键保留"
