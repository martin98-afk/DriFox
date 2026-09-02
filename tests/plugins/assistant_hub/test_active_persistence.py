# -*- coding: utf-8 -*-
"""test_active_persistence.py — 主助手即当前身份语义测试。

active_id 恒等于主助手 id：无独立「当前/激活」层，@提及走会话级 override。
"""

import importlib.util
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


def test_seed_primary_is_active(mgr):
    """首次 seed：主助手 build 即当前身份"""
    assert AssistantManager.active_id() == "build"


def test_set_primary_switches_active(mgr):
    """切主助手 = 切当前身份"""
    assert mgr.set_primary("hanako") is True
    assert AssistantManager.active_id() == "hanako"
    assert mgr.get("build").primary is False


def test_primary_survives_reload(mgr):
    """切换主助手后重启（reset+reload），当前身份从 yaml primary 恢复"""
    mgr.set_primary("hanako")
    AssistantManager.reset_instance()
    AssistantManager.get_instance(root_dir=str(mgr.root))
    assert AssistantManager.active_id() == "hanako"


def test_delete_non_primary_keeps_active(mgr):
    """删除非主助手：当前身份不变"""
    mgr.create("临时", id="temp_x")
    assert mgr.delete("temp_x") is True
    assert AssistantManager.active_id() == "build"
