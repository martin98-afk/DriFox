# -*- coding: utf-8 -*-
"""test_skills.py — 助手专属技能：enabled_skills 过滤 + write_skill 规范化 + 序列化往返。"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "assistant_manager.py"
spec = importlib.util.spec_from_file_location("test_skills_mgr_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_skills_mgr_mod", m)
spec.loader.exec_module(m)


def _fresh_manager(tmp_path):
    m.AssistantManager.reset_instance()
    return m.AssistantManager.get_instance(root_dir=str(tmp_path / "hub"))


def _mk_skills(mgr, aid, *names):
    for n in names:
        mgr.write_skill(aid, n, f"# {n} 的简介\n\n正文内容。")


def test_enabled_skills_default_all_on(tmp_path):
    """默认（whitelist/blacklist 全空）：全部启用。"""
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    _mk_skills(mgr, a.id, "alpha", "beta")
    names = [s["name"] for s in mgr.enabled_skills(a.id)]
    assert names == ["alpha", "beta"]


def test_enabled_skills_blacklist(tmp_path):
    """whitelist 空 + blacklist 非空：黑名单排除。"""
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    _mk_skills(mgr, a.id, "alpha", "beta", "gamma")
    a.skills_blacklist = ["beta"]
    mgr.update(a)
    names = [s["name"] for s in mgr.enabled_skills(a.id)]
    assert names == ["alpha", "gamma"]


def test_enabled_skills_whitelist_wins(tmp_path):
    """whitelist 非空：仅白名单启用（blacklist 被忽略）。"""
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    _mk_skills(mgr, a.id, "alpha", "beta", "gamma")
    a.skills_whitelist = ["alpha", "gamma"]
    a.skills_blacklist = ["gamma"]  # whitelist 模式下无效
    mgr.update(a)
    names = [s["name"] for s in mgr.enabled_skills(a.id)]
    assert names == ["alpha", "gamma"]


def test_enabled_skills_master_switch(tmp_path):
    """skills_enabled=False：全部不注入。"""
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    _mk_skills(mgr, a.id, "alpha")
    a.skills_enabled = False
    mgr.update(a)
    assert mgr.enabled_skills(a.id) == []


def test_skills_enabled_roundtrip(tmp_path):
    """序列化往返：skills_enabled=False 落盘后重载保留。"""
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    a.skills_enabled = False
    a.skills_blacklist = ["beta"]
    mgr.update(a)
    mgr2 = _fresh_manager(tmp_path)
    a2 = mgr2.get(a.id)
    assert a2 is not None
    assert a2.skills_enabled is False
    assert a2.skills_blacklist == ["beta"]


def test_write_skill_returns_safe_name(tmp_path):
    """write_skill 返回规范化名；非法名返回空串。"""
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    safe = mgr.write_skill(a.id, "Drifox 插件开发!", "# 简介")
    assert safe == "drifox"
    assert mgr.read_skill(a.id, "drifox").startswith("# 简介")
    assert mgr.write_skill(a.id, "///", "x") == ""
    assert mgr.delete_skill(a.id, "drifox") is True
    assert mgr.read_skill(a.id, "drifox") == ""
