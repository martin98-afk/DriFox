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


def test_utility_llm_binding(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path)
    a = mgr.create("模型助手")
    a.utility_model = "cfg-123"
    mgr.update(a)

    captured = {}

    class _Item:
        value = {}

    class _Sel:
        value = ""

    llm_mod = mgr._core_llm()

    def fake_resolve(config_id=""):
        captured["config_id"] = config_id
        return {"base_url": "https://x/v1", "api_key": "k", "model": "m", "provider_name": "p"}

    monkeypatch.setattr(llm_mod, "resolve_model_config", fake_resolve)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json as _json

            return _json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", lambda req, timeout=60: _Resp())

    call = mgr._utility_llm(a.id)
    # 验证 config_id 透传到 resolve
    out = call([{"role": "user", "content": "hi"}])
    assert out == "ok"
    assert captured["config_id"] == "cfg-123"

    # 空 utility_model → config_id 空 = 跟随全局
    b = mgr.create("全局助手")
    call2 = mgr._utility_llm(b.id)
    call2([{"role": "user", "content": "hi"}])
    assert captured["config_id"] == ""
