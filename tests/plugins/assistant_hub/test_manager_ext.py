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
    assert a.utility_model == ""
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


def test_prompt_block_assembles_sections(tmp_path):
    """prompt_block：header + 人格 + 人工提示 + 记忆段 + 技能段 全部组装。"""
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("小狐")
    mgr.write_pinned(a.id, [("pin-1", "回复保持简短")])
    mgr.write_skill(a.id, "demo-skill", "# 演示技能\n\n正文")
    # 记忆段需要实际记忆内容（无内容时整段不注入，与 hook 行为一致）
    mem_dir = mgr.assistant_dir(a.id) / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "longterm.md").write_text("## 长期记忆\n\n- 用户偏好简洁", encoding="utf-8")
    block = mgr.prompt_block(a.id)
    assert block.startswith("# 助手：小狐")
    assert "小狐" in block  # persona 段
    assert "人工提示" in block and "回复保持简短" in block
    assert "记忆使用规则" in block and "用户偏好简洁" in block  # 规则+编译记忆都注入
    assert "# 助手技能" in block and "demo-skill" in block and "演示技能" in block


def test_prompt_block_memory_off_and_skills_off(tmp_path):
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("纯净狐")
    a.memory_enabled = False
    a.skills_enabled = False
    mgr.update(a)
    mgr.write_pinned(a.id, [("pin-1", "硬性要求")])
    mgr.write_skill(a.id, "demo", "# x")
    block = mgr.prompt_block(a.id)
    assert "记忆使用规则" not in block
    assert "# 助手技能" not in block
    assert "硬性要求" in block  # 人工提示不受开关控制


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


def test_utility_llm_composite_key(tmp_path, monkeypatch):
    """utility_model 复合键 "<config_id>||<model>"：解析 override 覆盖模型名；失效回退全局。

    调用链已改走主对话引擎（services["create_engine_session"]），断言点从
    HTTP 请求体改为引擎会话的 model_config_override。
    """
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("模型助手")
    # 复合键：配置存在 → override 覆盖 模型名称
    a.utility_model = "cfg-1||model-x"
    mgr.update(a)

    captured = {}
    llm_mod = mgr._core_llm()

    class _FakeSession:
        def turn(self, messages=None, **kwargs):
            return _FakeResult("ok")

        def cleanup(self):
            pass

    class _FakeResult:
        def __init__(self, text):
            self.text = text
            self.error = None
            self.cancelled = False
            self.timed_out = False

    def _create_engine_session(engine_name, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeSession()

    llm_mod.set_services({"create_engine_session": _create_engine_session})

    # mock 主路径：UIPluginRegistry 返回带 _valid_configs 的窗口（对齐真实调用链）
    fake_mw = type("MW", (), {})()
    fake_mw._valid_configs = {
        "cfg-1": {"API_URL": "https://cfg1/v1", "API_KEY": "k1", "模型名称": "default-m", "provider_name": "P1"}
    }

    class _FakeReg:
        _main_widget = fake_mw
        _window_main_widgets = {}

        @classmethod
        def get_instance(cls):
            return cls()

    import app.plugins.registries.ui_plugin_registry as _reg_mod

    monkeypatch.setattr(_reg_mod, "UIPluginRegistry", _FakeReg)

    def _override():
        return captured["kwargs"].get("model_config_override") or {}

    call = mgr._utility_llm(a.id)
    out = call([{"role": "user", "content": "hi"}])
    assert out == "ok"
    # 复合键生效：URL 用 cfg-1，模型名被覆盖为 model-x
    assert _override()["API_URL"] == "https://cfg1/v1"
    assert _override()["模型名称"] == "model-x"

    # 无效复合键（配置不存在）→ 回退全局（无 override，只用工具型温度）
    b = mgr.create("回退助手")
    b.utility_model = "not-exist||m"
    mgr.update(b)
    call2 = mgr._utility_llm(b.id)
    call2([{"role": "user", "content": "hi"}])
    assert "模型名称" not in _override()

    # 空键 → 跟随全局
    c = mgr.create("全局助手")
    call3 = mgr._utility_llm(c.id)
    call3([{"role": "user", "content": "hi"}])
    assert "模型名称" not in _override()

    llm_mod.reset_sessions()
    with llm_mod._services_lock:
        llm_mod._services.clear()
