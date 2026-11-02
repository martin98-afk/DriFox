# -*- coding: utf-8 -*-
"""test_manager_ext.py — AssistantManager v2 门面扩展测试。"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "assistant_manager.py"

spec = importlib.util.spec_from_file_location("test_manager_ext_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_manager_ext_mod", m)
spec.loader.exec_module(m)


def _fresh_manager(tmp_path):
    m.AssistantManager.reset_instance()
    return m.AssistantManager.get_instance(root_dir=str(tmp_path / "hub"))


# ── 会话归属映射（记忆素材过滤数据源）──


def test_session_map_record_and_resolve(tmp_path):
    mgr = _fresh_manager(tmp_path)
    a1 = mgr.create("主助手")
    mgr.set_primary(a1.id)
    a2 = mgr.create("临时助手")
    # 无记录回落主助手
    assert mgr.resolve_session_aid("s-none") == a1.id
    # 记录后按映射解析
    mgr.record_session_aid("s1", a1.id)
    mgr.record_session_aid("s2", a2.id)
    assert mgr.resolve_session_aid("s1") == a1.id
    assert mgr.resolve_session_aid("s2") == a2.id
    # 空参数防御
    mgr.record_session_aid("", a1.id)
    assert mgr.resolve_session_aid("") == a1.id


def test_session_map_persist_and_reload(tmp_path):
    mgr = _fresh_manager(tmp_path)
    a1 = mgr.create("主助手")
    mgr.set_primary(a1.id)
    a2 = mgr.create("临时助手")
    mgr.record_session_aid("s1", a1.id)
    mgr.record_session_aid("s2", a2.id)
    assert (tmp_path / "hub" / "_session_map.json").exists()
    # 重载后映射保留
    mgr2 = _fresh_manager(tmp_path)
    assert mgr2.resolve_session_aid("s2") == a2.id


def test_session_map_deleted_assistant_falls_back(tmp_path):
    mgr = _fresh_manager(tmp_path)
    a1 = mgr.create("主助手")
    mgr.set_primary(a1.id)
    a2 = mgr.create("将删助手")
    mgr.record_session_aid("s1", a2.id)
    mgr.delete(a2.id)
    # 归属助手已删 → 回落主助手
    assert mgr.resolve_session_aid("s1") == a1.id


def test_set_session_override_records_map(tmp_path):
    mgr = _fresh_manager(tmp_path)
    mgr.create("主助手")
    a2 = mgr.create("临时助手")
    mgr.set_session_override("s9", a2.id)
    assert mgr.resolve_session_aid("s9") == a2.id
    mgr.set_session_override("s9", "")  # 清除 override
    # override 清除不影响归属映射（归属以最后使用为准）


def test_session_override_persist_and_reload(tmp_path):
    """override 持久化：重启后恢复，SessionStart 记忆注入仍按临时助手开关判断。"""
    mgr = _fresh_manager(tmp_path)
    mgr.create("主助手")
    a2 = mgr.create("临时助手")
    assert mgr.set_session_override("s1", a2.id)
    assert (tmp_path / "hub" / "_session_overrides.json").exists()
    # 重启（新实例）后 override 保留
    mgr2 = _fresh_manager(tmp_path)
    assert mgr2.get_session_override("s1") == a2.id
    # 清除 override 也落盘：重启后不回来
    mgr2.set_session_override("s1", "")
    mgr3 = _fresh_manager(tmp_path)
    assert mgr3.get_session_override("s1") == ""


def test_session_override_deleted_assistant_cleaned(tmp_path):
    """override 指向的助手已删：读取时惰性清理并落盘。"""
    mgr = _fresh_manager(tmp_path)
    mgr.create("主助手")
    a2 = mgr.create("将删助手")
    mgr.set_session_override("s1", a2.id)
    mgr.delete(a2.id)
    assert mgr.get_session_override("s1") == ""
    mgr2 = _fresh_manager(tmp_path)
    assert mgr2.get_session_override("s1") == ""


def test_session_override_trimmed_when_overflow(tmp_path):
    mgr = _fresh_manager(tmp_path)
    mgr.create("主助手")
    for i in range(1100):
        mgr.set_session_override(f"s{i}", mgr.active_id())
    assert len(m.AssistantManager._session_overrides) <= 1000
    assert "s0" not in m.AssistantManager._session_overrides


def test_session_map_trimmed_when_overflow(tmp_path):
    mgr = _fresh_manager(tmp_path)
    mgr.create("主助手")
    for i in range(1100):
        mgr.record_session_aid(f"s{i}", mgr.active_id())
    assert len(m.AssistantManager._session_map) <= 1000
    # 最旧的被裁剪
    assert "s0" not in m.AssistantManager._session_map


def test_yuan_migration_on_load(tmp_path):
    aid_dir = tmp_path / "hub" / "legacy"
    aid_dir.mkdir(parents=True)
    (aid_dir / "assistant.yaml").write_text("id: legacy\nname: 旧助手\nyuan: kong\n", encoding="utf-8")
    mgr = _fresh_manager(tmp_path)
    a = mgr.get("legacy")
    assert a is not None and a.yuan == "none"


def test_create_defaults_and_experience_field(tmp_path):
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("测试助手")
    assert a.yuan == "build"
    assert a.experience_enabled is False
    # 落盘回读
    text = (mgr.assistant_dir(a.id) / "assistant.yaml").read_text(encoding="utf-8")
    assert "experience_enabled" in text


def test_identity_and_persona_fill(tmp_path):
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    text = mgr.identity_and_persona(a.id)
    # 身份模板 + persona 底座 + AGENTS.md 三段都在；变量被填充
    assert "小狐" in text
    assert "{{agentName}}" not in text and "{{userName}}" not in text


def test_identity_persona_none(tmp_path):
    """persona=none：不注入 persona 底座（推演/MOOD 块协议不出现）。"""
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("纯净助手")
    a.yuan = "none"
    mgr.update(a)
    text = mgr.identity_and_persona(a.id)
    assert "<plan>" not in text and "<mood>" not in text


def test_prompt_blocks_assemble_sections(tmp_path):
    """注入块拆分：身份块 / 人工提示块 / 记忆体系块（说明书+全局+项目）各自就位。"""
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    mgr.write_pinned(a.id, [("pin-1", "回复保持简短")])
    mgr.write_global_memory(a.id, "- 跨项目：用户偏好简洁")
    mgr.write_project_memory(a.id, str(tmp_path / "proj"), "- 项目：DriFox 重构中")

    identity = mgr.identity_block(a.id)
    assert identity.startswith("# 助手：小狐")
    assert "小狐" in identity  # persona 段
    # 身份块不含记忆
    assert "人工提示" not in identity and "memory_system" not in identity

    pinned = mgr.pinned_block(a.id)
    assert "人工提示" in pinned and "回复保持简短" in pinned

    mem = mgr.memory_system_block(a.id, str(tmp_path / "proj"))
    assert "memory_read" in mem and "memory_write" in mem  # 指针级说明书
    assert "<global_memory>" in mem and "用户偏好简洁" in mem
    assert "<project_memory>" in mem and "DriFox 重构中" in mem


def test_memory_system_block_memory_off(tmp_path):
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("纯净狐")
    a.memory_enabled = False
    mgr.update(a)
    mgr.write_pinned(a.id, [("pin-1", "硬性要求")])
    assert mgr.memory_system_block(a.id) == ""
    assert "硬性要求" in mgr.pinned_block(a.id)  # 人工提示不受开关控制


def test_tool_access_for_session_override(tmp_path):
    """预置档位：会话 override 助手优先，否则主助手。"""
    mgr = _fresh_manager(tmp_path)
    mgr.create("主狐")
    a2 = mgr.create("只读狐")
    a2.tool_access = "readonly"
    mgr.update(a2)
    assert mgr.tool_access_for("") == "full"  # 无 override → 主助手档
    assert mgr.tool_access_for("s-none") == "full"
    mgr.set_session_override("s1", a2.id)
    assert mgr.tool_access_for("s1") == "readonly"  # override 助手档生效
    assert mgr.tool_access_for("s2") == "full"  # 其他会话不受影响


def test_prompt_stats_counts_chars(tmp_path):
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    stats = mgr.prompt_stats(a.id)
    assert stats["chars"] > 0
    assert stats["tokens_est"] == int(stats["chars"] / 1.6)
    # 无 persona 段且无其他内容：纯净助手块为空 → 统计为 0
    a2 = mgr.create("空狐")
    a2.yuan = "none"
    a2.memory_enabled = False
    mgr.update(a2)
    empty = mgr.prompt_stats(a2.id)
    assert empty == {"chars": 0, "tokens_est": 0}


def test_persona_registry_via_facade(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path)
    reg = mgr.persona_registry()
    ids = [p.id for p in reg.list_all()]
    assert "build" in ids and "hanako" in ids and "none" in ids




def test_invalidate_session_prompt_is_static(tmp_path, monkeypatch):
    """守卫：instance._invalidate_session_prompt(sid) 单参可调用。

    漏 staticmethod 时 session_id 会被 self 吃掉、真实 sid 变成多余实参，
    TypeError 被上层 except 吞掉 → 会话 system_prompt 缓存永不清 → 选中的
    助手身份本轮不生效（@提及与 UI 选中两条路径同时失效）。
    """
    mgr = _fresh_manager(tmp_path)
    called = []

    def _fake_caps(cap_id):
        if cap_id != "session.invalidate":
            return None
        return lambda sid="": called.append(sid)

    monkeypatch.setattr(m, "_caps", _fake_caps)
    mgr._invalidate_session_prompt("sid-x")
    assert called == ["sid-x"]
