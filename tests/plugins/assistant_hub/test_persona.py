# -*- coding: utf-8 -*-
"""test_persona.py — assistant_hub core/persona 单元测试。"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "persona.py"


def _load():
    spec = importlib.util.spec_from_file_location("test_persona_mod", str(_MODULE))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("test_persona_mod", mod)
    spec.loader.exec_module(mod)
    return mod


m = _load()
_BUILTIN_DIR = _ROOT / "plugins" / "assistant_hub" / "personas"


def _fresh_registry(tmp_path):
    return m.PersonaRegistry.get_instance(custom_path=tmp_path / "personas.json", builtin_dir=_BUILTIN_DIR, reset=True)


def test_list_all_contains_builtin(tmp_path):
    reg = _fresh_registry(tmp_path)
    ids = [p.id for p in reg.list_all()]
    assert "build" in ids and "hanako" in ids and "none" in ids


def test_render_fills_vars(tmp_path):
    reg = _fresh_registry(tmp_path)
    persona = reg.get("build")
    assert persona is not None
    out = reg.render("build", persona.prompt, agent_name="小狐", user_name="马丁")
    assert "{{agentName}}" not in out and "{{userName}}" not in out
    assert "小狐" in out and "马丁" in out


def test_render_none_empty(tmp_path):
    reg = _fresh_registry(tmp_path)
    assert reg.render("none", "", agent_name="a", user_name="b") == ""
    assert reg.render("不存在的", "模板", agent_name="a", user_name="b") == ""


def test_custom_upsert_delete(tmp_path):
    reg = _fresh_registry(tmp_path)
    p = m.Persona(id="my-p", name="我的", description="测试", tag="X", prompt="你好 {{userName}}")
    reg.upsert(p)
    assert "my-p" in [x.id for x in reg.list_all()]
    # 持久化：重建实例仍在
    reg2 = m.PersonaRegistry.get_instance(custom_path=tmp_path / "personas.json", builtin_dir=_BUILTIN_DIR)
    assert "my-p" in [x.id for x in reg2.list_all()]
    assert reg2.get("my-p").builtin is False
    # builtin 不可删
    assert reg2.delete("build") is False
    assert reg2.delete("my-p") is True
    assert "my-p" not in [x.id for x in reg2.list_all()]


def test_resolve_user_name_fallback(monkeypatch):
    # getuser 抛异常 + 环境变量缺失 → 回落「用户」
    monkeypatch.setitem(sys.modules, "getpass", None)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    assert m.resolve_user_name() == "用户"
    # getuser 正常返回 → 原样使用
    fake = type("G", (), {"getuser": staticmethod(lambda: "martin")})
    monkeypatch.setitem(sys.modules, "getpass", fake)
    assert m.resolve_user_name() == "martin"


def test_module_loads_without_getpass(monkeypatch):
    """打包回归：PyInstaller 不分析动态加载的插件源文件，getpass 可能未进 PYZ。

    模拟打包环境（import getpass 失败）下 persona.py 必须仍可加载，
    否则 PersonaRegistry 全挂 → 助手中心人格卡片空白。
    """
    monkeypatch.setitem(sys.modules, "getpass", None)  # None → import 直接 ImportError
    spec = importlib.util.spec_from_file_location("test_persona_no_getpass", str(_MODULE))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_persona_no_getpass"] = mod
    spec.loader.exec_module(mod)  # 修复前：ModuleNotFoundError: No module named 'getpass'
    assert mod.resolve_user_name()  # 回落链兜底，非空即可
