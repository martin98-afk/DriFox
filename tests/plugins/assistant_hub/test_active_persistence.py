# -*- coding: utf-8 -*-
"""test_active_persistence.py — active_id 持久化与启动回落测试。"""

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MANAGER = _ROOT / "plugins" / "assistant_hub" / "assistant_manager.py"

if "assistant_hub_manager" not in sys.modules:
    spec = importlib.util.spec_from_file_location("assistant_hub_manager", str(_MANAGER))
    _mod = importlib.util.module_from_spec(spec)
    sys.modules["assistant_hub_manager"] = _mod
    spec.loader.exec_module(_mod)

import pytest

from assistant_hub_manager import AssistantManager


@pytest.fixture()
def mgr(tmp_path):
    """独立 root 的干净单例（每个用例互不污染）"""
    AssistantManager.reset_instance()
    m = AssistantManager.get_instance(root_dir=str(tmp_path / "assistant_hub"))
    yield m
    AssistantManager.reset_instance()


def test_seed_activates_primary_and_persists(mgr):
    """首次 seed：build 主助手被激活，且 active.json 落盘"""
    assert AssistantManager.active_id() == "build"
    data = json.loads((mgr.root / "active.json").read_text(encoding="utf-8"))
    assert data["active_id"] == "build"


def test_restore_persists_across_reset(mgr, tmp_path):
    """切换助手后重启（reset+reload），active_id 从磁盘恢复"""
    assert mgr.set_active("hanako") is True
    aid_before = AssistantManager.active_id()

    AssistantManager.reset_instance()
    m2 = AssistantManager.get_instance(root_dir=str(mgr.root))
    assert AssistantManager.active_id() == aid_before == "hanako"


def test_restore_falls_back_to_primary_when_no_file(mgr, tmp_path):
    """无 active.json（老数据）→ 回落主助手 build"""
    (mgr.root / "active.json").unlink()
    AssistantManager.reset_instance()
    m2 = AssistantManager.get_instance(root_dir=str(mgr.root))
    assert AssistantManager.active_id() == "build"


def test_restore_respects_cleared_state(mgr):
    """clear_active 写空串 → 重启后保持未激活（不回落）"""
    mgr.clear_active()
    assert AssistantManager.active_id() == ""
    AssistantManager.reset_instance()
    m2 = AssistantManager.get_instance(root_dir=str(mgr.root))
    assert AssistantManager.active_id() == ""


def test_restore_skips_stale_id(mgr):
    """active.json 指向已删除助手 → 保持未激活，不指向幽灵 id"""
    mgr.create("临时", id="temp_x")
    mgr.set_active("temp_x")
    mgr.delete("temp_x")
    assert AssistantManager.active_id() != "temp_x"
    data = json.loads((mgr.root / "active.json").read_text(encoding="utf-8"))
    assert data["active_id"] == AssistantManager.active_id()


def test_delete_active_falls_back_to_first(mgr):
    """删除当前激活助手 → 回落到剩余第一个助手并同步落盘"""
    mgr.set_active("hanako")
    assert mgr.delete("hanako") is True
    aid = AssistantManager.active_id()
    assert aid and mgr.has(aid)
