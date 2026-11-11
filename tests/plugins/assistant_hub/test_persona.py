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

def test_user_dir_personas_loaded(tmp_path):
    """用户自建人格：<custom_path 同级>/personas/<id>/persona.md 被加载。"""
    user_dir = tmp_path / "personas" / "my-p"
    user_dir.mkdir(parents=True)
    (user_dir / "persona.md").write_text(
        "---\nid: my-p\nname: 我的\ndescription: 自建\ntag: MINE\n---\n\n正文 {{userName}}",
        encoding="utf-8",
    )
    reg = _fresh_registry(tmp_path)
    persona = reg.get("my-p")
    assert persona is not None
    assert persona.builtin is False
    assert persona.tag == "MINE"
    assert reg.exists("my-p") is True
    assert reg.user_dir == (tmp_path / "personas").resolve()


def test_user_dir_avatar(tmp_path):
    """用户自建人格的头像从其自身目录解析。"""
    d = tmp_path / "personas" / "my-p"
    d.mkdir(parents=True)
    (d / "persona.md").write_text("---\nid: my-p\nname: 我的\n---\n\nbody", encoding="utf-8")
    png = b"\x89PNG\r\n\x1a\n"
    (d / "avatar.png").write_bytes(png)
    reg = _fresh_registry(tmp_path)
    ap = reg.avatar_path("my-p")
    assert ap is not None and ap.read_bytes() == png


def test_user_dir_id_conflict_ignored(tmp_path):
    """id 与内置冲突：用户目录那条被忽略，内置人格仍生效（升级不被旧副本顶掉）。"""
    d = tmp_path / "personas" / "build"
    d.mkdir(parents=True)
    (d / "persona.md").write_text("---\nid: build\nname: 冒充内置\n---\n\nX", encoding="utf-8")
    reg = _fresh_registry(tmp_path)
    persona = reg.get("build")
    assert persona.builtin is True
    assert persona.name != "冒充内置"


def test_user_persona_not_deletable_by_json_api(tmp_path):
    """用户目录人格不归 personas.json 管：upsert/delete 均拒绝，删要靠删目录。"""
    d = tmp_path / "personas" / "my-p"
    d.mkdir(parents=True)
    (d / "persona.md").write_text("---\nid: my-p\nname: 我的\n---\n\nbody", encoding="utf-8")
    reg = _fresh_registry(tmp_path)
    assert reg.delete("my-p") is False
    assert reg.upsert(m.Persona(id="my-p", name="覆盖")) is False
