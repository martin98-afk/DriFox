# -*- coding: utf-8 -*-
"""show_artifacts 工具回归（plugins/system-core/tools/artifacts_tool.py）"""
import sys
from importlib import import_module
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_mod = import_module("plugins.system-core.tools.artifacts_tool")


def _ctx(tmp_path):
    return {"workdir": str(tmp_path)}


def test_schema_contract():
    fn = _mod._SCHEMA["function"]
    assert fn["name"] == "show_artifacts"
    assert fn["parameters"]["required"] == ["paths"]
    assert fn["parameters"]["properties"]["paths"]["type"] == "array"


def test_multiple_paths_mounted(tmp_path):
    a = tmp_path / "a.png"
    a.write_bytes(b"x" * 3)
    b = tmp_path / "b.md"
    b.write_text("hi", encoding="utf-8")
    r = _mod._show_impl(_ctx(tmp_path), paths=["a.png", str(b)])
    assert r.success
    assert [i["path"] for i in r.artifacts] == [str(a), str(b)]
    assert r.artifacts[0]["size"] == 3


def test_relative_resolves_against_workdir(tmp_path):
    f = tmp_path / "sub" / "r.txt"
    f.parent.mkdir()
    f.write_text("x", encoding="utf-8")
    r = _mod._show_impl(_ctx(tmp_path), paths=["sub/r.txt"])
    assert r.artifacts[0]["path"] == str(f)


def test_missing_path_skipped(tmp_path):
    ok = tmp_path / "ok.txt"
    ok.write_text("x", encoding="utf-8")
    r = _mod._show_impl(_ctx(tmp_path), paths=["ok.txt", "nope.txt"])
    assert r.success
    assert [i["path"] for i in r.artifacts] == [str(ok)]
    assert "nope.txt" in r.content


def test_all_missing_fails(tmp_path):
    r = _mod._show_impl(_ctx(tmp_path), paths=["a.txt", "b.txt"])
    assert not r.success
    assert "没有可展示的文件" in r.error


def test_directory_skipped(tmp_path):
    (tmp_path / "dir").mkdir()
    r = _mod._show_impl(_ctx(tmp_path), paths=["dir"])
    assert not r.success


def test_cap_at_eight(tmp_path):
    names = []
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
        names.append(f"f{i}.txt")
    r = _mod._show_impl(_ctx(tmp_path), paths=names)
    assert len(r.artifacts) == 8
    assert "上限" in r.content


def test_empty_paths_fails(tmp_path):
    r = _mod._show_impl(_ctx(tmp_path), paths=[])
    assert not r.success
