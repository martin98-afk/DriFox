# -*- coding: utf-8 -*-
"""multi_edit 重构回归测试（fix/multi-edit-atomicity）。

覆盖事故与设计要求的五类场景：
1. 正常多条编辑（不重叠）→ 全部生效
2. 锚点漂移防护（复现事故形态：edit1 的 newString 含 edit2 的锚点，
   流式替换会错配到刚插入的副本；原文定位应替换原文位置）
3. 区间重叠 → 整体拒绝，文件原样
4. 部分失败 → 整体不写（全有或全无，删除旧「部分成功已写入」语义）
5. 自校验（py 文件替换成语法非法片段 → 整体不写）+ 跨度体检 WARNING
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TOOL_FILE = Path(__file__).resolve().parents[2] / "plugins" / "system-core" / "tools" / "file_tools.py"
_spec = importlib.util.spec_from_file_location("_file_tools_multi_edit_test", _TOOL_FILE)
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("_file_tools_multi_edit_test", _mod)
_spec.loader.exec_module(_mod)
_multi_edit_impl = _mod._multi_edit_impl


def _call(workdir: Path, path: str, edits):
    return _multi_edit_impl({"workdir": str(workdir)}, path=path, edits=edits)


def test_normal_multiple_edits(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8", newline="")
    r = _call(
        tmp_path,
        "a.py",
        [
            {"oldString": "alpha = 1", "newString": "alpha = 11"},
            {"oldString": "gamma = 3", "newString": "gamma = 33"},
        ],
    )
    assert r.success, getattr(r, "error", None)
    assert f.read_text(encoding="utf-8") == "alpha = 11\nbeta = 2\ngamma = 33\n"
    assert "beta = 2" in r.diff  # 未动的行保留（diff 中可见）


def test_anchor_drift_guarded_by_original_positioning(tmp_path: Path):
    """事故形态复现：edit1 的 newString 含 edit2 的锚点文本。

    旧流式实现会在 edit1 刚插入的副本上做 edit2 的替换（错位）；
    原文定位 + 倒序拼接必须替换原文里的那份。
    """
    f = tmp_path / "b.py"
    original = "X = 1\nY = 3\n"
    f.write_text(original, encoding="utf-8", newline="")
    r = _call(
        tmp_path,
        "b.py",
        [
            {"oldString": "X = 1", "newString": "Y = 3\nX = 1"},  # 插入副本在前
            {"oldString": "Y = 3", "newString": "Y = 4"},  # 原文唯一
        ],
    )
    assert r.success, getattr(r, "error", None)
    # 原文那份 Y = 3 被改成 Y = 4；edit1 插入的副本保留
    assert f.read_text(encoding="utf-8") == "Y = 3\nX = 1\nY = 4\n"


def test_overlapping_regions_rejected_whole(tmp_path: Path):
    f = tmp_path / "c.py"
    original = "hello world\nsecond line\n"
    f.write_text(original, encoding="utf-8", newline="")
    r = _call(
        tmp_path,
        "c.py",
        [
            {"oldString": "hello world", "newString": "HELLO WORLD"},
            {"oldString": "lo wo", "newString": "LO WO"},  # 区间被 edit1 覆盖
        ],
    )
    assert not r.success
    assert "重叠" in (r.error or "")
    assert f.read_text(encoding="utf-8") == original, "重叠拒绝必须整体不写盘"


def test_partial_failure_writes_nothing(tmp_path: Path):
    """全有或全无：一条成功一条失败 → 整体拒绝，文件原样（旧实现会写入成功项）。"""
    f = tmp_path / "d.py"
    original = "keep = 1\nbroken_anchor_xyz = 2\n"
    f.write_text(original, encoding="utf-8", newline="")
    r = _call(
        tmp_path,
        "d.py",
        [
            {"oldString": "keep = 1", "newString": "keep = 111"},
            {"oldString": "nonexistent_anchor_qq", "newString": "x"},  # 原文无此串
        ],
    )
    assert not r.success
    assert "全有或全无" in (r.error or "")
    assert f.read_text(encoding="utf-8") == original, "部分失败必须整体不写盘"


def test_non_unique_anchor_rejected(tmp_path: Path):
    f = tmp_path / "e.py"
    original = "dup = 1\ndup = 2\n"
    f.write_text(original, encoding="utf-8", newline="")
    r = _call(tmp_path, "e.py", [{"oldString": "dup = ", "newString": "x = "}])
    assert not r.success
    assert "不唯一" in (r.error or "")
    assert f.read_text(encoding="utf-8") == original


def test_self_check_blocks_syntax_breaking_result(tmp_path: Path):
    """自校验：py 文件被替换成语法非法片段 → ast.parse 拦截，整体不写。"""
    f = tmp_path / "f.py"
    original = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
    f.write_text(original, encoding="utf-8", newline="")
    r = _call(
        tmp_path,
        "f.py",
        [{"oldString": "def f():\n    return 1\n", "newString": "def f(:"}],
    )
    assert not r.success
    assert "ast.parse" in (r.error or "")
    assert f.read_text(encoding="utf-8") == original


def test_span_anomaly_warning(tmp_path: Path):
    """P4 跨度体检：函数跨度暴增（+>50% 且 >100 行）在 content 附 WARNING，不阻断。"""
    f = tmp_path / "g.py"
    original = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
    f.write_text(original, encoding="utf-8", newline="")
    filler = "\n".join(f"    x{i} = {i}" for i in range(120))
    r = _call(
        tmp_path,
        "g.py",
        [{"oldString": "def f():\n    return 1\n", "newString": f"def f():\n    return 1\n{filler}\n"}],
    )
    assert r.success, getattr(r, "error", None)
    assert "跨度异常增长" in r.content
    assert "f: 2 → 122" in r.content  # 跨度表 key = 函数名，不含 def 前缀
    assert "return 2" in f.read_text(encoding="utf-8")  # g 未受影响


def test_span_shrink_warning_for_dedent_injection(tmp_path: Path):
    """缩减方向：dedent 注入把原函数体顶出，被编辑函数跨度骤缩 → 同样告警。

    13 场景对比暴露的盲区：`_py_def_spans` 旧判据只查增长，场景 10 的
    「从行首起写顶层 def」注入让 big 函数 10 行 → 2 行，delta 为负，零告警。
    """
    f = tmp_path / "s.py"
    body = "\n".join(f"    line{i} = {i}" for i in range(8))
    original = f"def big():\n{body}\n\n\ndef small():\n    return 0\n"
    f.write_text(original, encoding="utf-8", newline="")
    # newString 从行首起写：注入新顶层函数，原 big 的函数体被「顶」出
    injected = "def injected():\n    return -1\n\n\ndef big():\n    return 1\n"
    r = _call(
        tmp_path,
        "s.py",
        [{"oldString": "def big():\n" + body + "\n", "newString": injected}],
    )
    assert r.success, getattr(r, "error", None)
    assert "跨度骤缩" in r.content
    assert "big" in r.content
    # 注入的新函数本身跨度正常，不告警
    assert "injected: " not in r.content.split("⚠️")[1] if "⚠️" in r.content else True


def test_non_utf8_reported_not_replaced(tmp_path: Path):
    """errors=strict：非 UTF-8 字节不再被静默替换为 U+FFFD 写回，而是报错。"""
    f = tmp_path / "h.bin.py"
    f.write_bytes("ok = 1\n".encode("utf-8") + b"\xff\xfe bad = 2\n")
    r = _call(tmp_path, "h.bin.py", [{"oldString": "ok = 1", "newString": "ok = 2"}])
    assert not r.success
    assert f.read_bytes() == "ok = 1\n".encode("utf-8") + b"\xff\xfe bad = 2\n"  # 原字节未损坏


def test_early_error_returns_result_not_exception(tmp_path: Path, monkeypatch):
    """cleanup_note 必须在 try 之前初始化（reviewer 实测形态）。

    try 前段（_resolve / read_text / _check_modified / 定位 / 自校验）任何异常
    都走 except Exception 并引用 cleanup_note —— 若它定义在 try 之后，
    会抛 UnboundLocalError 把原错误吞掉、异常直接逃逸给调用方，破坏
    「失败返回 ToolResult(False)」的工具契约。
    """
    monkeypatch.setattr(
        _mod, "_resolve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    r = _call(tmp_path, "whatever.py", [{"oldString": "a", "newString": "b"}])
    assert not r.success
    assert "boom" in (r.error or ""), "早期异常必须以 ToolResult(False) 返回，不得逃逸"


def test_readonly_target_cleans_tmp(tmp_path: Path):
    """复核阻断项：只读目标 → replace 失败 → 同目录不得残留 .tmp-multiedit。"""
    import os as _os

    f = tmp_path / "ro.py"
    f.write_text("x = 1\n", encoding="utf-8")
    f.chmod(0o444)  # Windows：只读文件 os.replace 覆盖 → PermissionError
    try:
        r = _call(tmp_path, "ro.py", [{"oldString": "x = 1", "newString": "x = 2"}])
        assert not r.success, getattr(r, "error", None)
        leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-multiedit" in p.name]
        assert not leftovers, f"失败路径残留 tmp 文件: {leftovers}"
        assert "临时文件已清理" in (r.error or r.content or "")
    finally:
        f.chmod(0o644)  # 恢复可写，便于 tmp_path 目录清理


def test_tmp_names_unique_across_calls(tmp_path: Path, monkeypatch):
    """并发隔离：两次调用各自 tmp 名不同（pid + uuid4 随机段），不互相覆盖。"""
    import os as _os

    f = tmp_path / "u.py"
    f.write_text("x = 1\n", encoding="utf-8")
    f.chmod(0o444)
    seen = []
    real_replace = _os.replace

    def spy_replace(src, dst):
        seen.append(str(src))  # src = tmp 唯一名（dst = 目标文件）
        real_replace(src, dst)  # 只读目标 → PermissionError

    monkeypatch.setattr(_os, "replace", spy_replace)
    try:
        for _ in range(2):
            r = _call(tmp_path, "u.py", [{"oldString": "x = 1", "newString": "x = 2"}])
            assert not r.success
    finally:
        f.chmod(0o644)
    assert len(seen) == 2, "两次调用都应走到 os.replace（tmp 均已写出）"
    assert seen[0] != seen[1], f"tmp 名必须唯一，实测相同: {seen}"
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-multiedit" in p.name]
    assert not leftovers, f"残留 tmp: {leftovers}"
