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
