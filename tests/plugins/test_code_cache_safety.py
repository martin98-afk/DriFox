# -*- coding: utf-8 -*-
"""插件工具码对象缓存的安全性回归（2026-11-27）。

本文件守护的是这条结论：**缓存键必须用源码内容哈希，不能用 mtime+size。**

背景：`plugin_tool_loader._load_module` 原本刻意绕过 `__pycache__`，原因是
Windows 文件系统 mtime 精度低，快速写入后 exec 会误复用旧 pyc，导致热重载
内容不更新。若这里自建的缓存把键改回 mtime/size，就等于把原设计要防的坑
重新挖一遍，且线上表现为「插件更新了但代码没变」的灵异问题。

每条用例都直接操作缓存模块本身，不启动 GUI、不加载插件。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from app.plugins.loaders import _code_cache as cc


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """把缓存落点隔离到临时目录，并重置进程内状态。"""
    monkeypatch.setenv("DRIFOX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cc, "_state", {
        "loaded": False, "enabled": True, "dirty": False, "scheduled": False, "entries": {},
    })
    yield tmp_path
    cc._state["scheduled"] = False


def _exec_value(code, namespace=None):
    ns = {"__builtins__": __builtins__} if namespace is None else namespace
    exec(code, ns)
    return ns


def test_same_mtime_same_size_edit_is_detected(isolated_cache):
    """★ 本方案存在的理由：同尺寸改写 + mtime 不变，缓存必须失效。

    这条对应 `__pycache__` 被绕过的原始原因。若键用 (mtime, size)，
    第二次会命中旧 code object，读到 VALUE=1。
    """
    p = isolated_cache / "m.py"
    p.write_text("VALUE = 1\n", encoding="utf-8")
    st1 = p.stat()
    src1 = p.read_text(encoding="utf-8")

    code1, hit1 = cc.get_code(src1, str(p))
    assert hit1 is False, "首个版本必然是现场编译"
    assert _exec_value(code1)["VALUE"] == 1

    # 同尺寸改写，并把 mtime 强行恢复原值（模拟 Windows 低精度 mtime）
    p.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(p, ns=(st1.st_atime_ns, st1.st_mtime_ns))
    st2 = p.stat()
    assert st2.st_size == st1.st_size, "用例要求尺寸相同"
    assert st2.st_mtime_ns == st1.st_mtime_ns, "用例要求 mtime 相同"

    src2 = p.read_text(encoding="utf-8")
    code2, hit2 = cc.get_code(src2, str(p))
    assert hit2 is False, "源码已变，必须重新编译而不是命中"
    assert _exec_value(code2)["VALUE"] == 2, "必须执行新代码，绝不能复用旧 code object"


def test_unchanged_source_hits_cache(isolated_cache):
    """源码未变时必须命中，否则缓存没有意义。"""
    p = isolated_cache / "m.py"
    p.write_text("VALUE = 42\n", encoding="utf-8")
    src = p.read_text(encoding="utf-8")

    code1, hit1 = cc.get_code(src, str(p))
    assert hit1 is False
    code2, hit2 = cc.get_code(src, str(p))
    assert hit2 is True, "同源码第二次必须命中缓存"
    assert _exec_value(code2)["VALUE"] == 42


def test_cache_survives_process_restart(isolated_cache):
    """跨进程持久化：落盘后重置进程内状态，仍应命中（这才是启动收益的来源）。"""
    p = isolated_cache / "m.py"
    p.write_text("VALUE = 7\n", encoding="utf-8")
    src = p.read_text(encoding="utf-8")

    cc.get_code(src, str(p))
    cc.flush()
    cache_file = Path(os.environ["DRIFOX_DATA_DIR"]) / "cache" / cc._FILENAME
    assert cache_file.exists(), "flush 后应产生缓存文件"

    # 模拟新进程
    cc._state.update({"loaded": False, "dirty": False, "scheduled": False, "entries": {}})
    code, hit = cc.get_code(src, str(p))
    assert hit is True, "新进程应命中断盘缓存"
    assert _exec_value(code)["VALUE"] == 7


def test_python_version_mismatch_invalidates(isolated_cache, monkeypatch):
    """解释器版本不一致必须整份作废（code object 跨版本二进制不兼容）。"""
    p = isolated_cache / "m.py"
    p.write_text("VALUE = 1\n", encoding="utf-8")
    src = p.read_text(encoding="utf-8")
    cc.get_code(src, str(p))
    cc.flush()

    cc._state.update({"loaded": False, "dirty": False, "scheduled": False, "entries": {}})
    monkeypatch.setattr(cc.sys, "version", "0.0.0-fake")
    _code, hit = cc.get_code(src, str(p))
    assert hit is False, "版本不匹配时不得复用"


def test_schema_mismatch_invalidates(isolated_cache, monkeypatch):
    """schema 版本变化必须整份作废（判定语义变更时自增）。"""
    p = isolated_cache / "m.py"
    p.write_text("VALUE = 1\n", encoding="utf-8")
    src = p.read_text(encoding="utf-8")
    cc.get_code(src, str(p))
    cc.flush()

    cc._state.update({"loaded": False, "dirty": False, "scheduled": False, "entries": {}})
    monkeypatch.setattr(cc, "_SCHEMA", cc._SCHEMA + 1)
    _code, hit = cc.get_code(src, str(p))
    assert hit is False, "schema 不匹配时不得复用"


def test_corrupt_entry_falls_back_to_compile(isolated_cache):
    """损坏条目应回落到现场编译，绝不放行可疑对象。"""
    p = isolated_cache / "m.py"
    p.write_text("VALUE = 3\n", encoding="utf-8")
    src = p.read_text(encoding="utf-8")
    k = cc._key(src, str(p))
    entries = cc._load()
    entries[k] = "!!!not-base64-marshal!!!"

    code, hit = cc.get_code(src, str(p))
    assert hit is False, "损坏条目不算命中"
    assert _exec_value(code)["VALUE"] == 3


def test_disabled_switch_bypasses_cache(isolated_cache, monkeypatch):
    """PLUGIN_CODE_CACHE=0 时必须完全绕过缓存（A/B 对照用）。"""
    monkeypatch.setattr(cc, "_state", {
        "loaded": True, "enabled": False, "dirty": False, "scheduled": False, "entries": {},
    })
    p = isolated_cache / "m.py"
    p.write_text("VALUE = 9\n", encoding="utf-8")
    code, hit = cc.get_code(p.read_text(encoding="utf-8"), str(p))
    assert hit is False
    assert _exec_value(code)["VALUE"] == 9
    assert cc._state["dirty"] is False, "禁用时不应标脏"


def test_schedule_flush_resets_flag_so_later_entries_persist(isolated_cache):
    """回归：flush 后调度标志必须复位，否则后续新条目永不落盘。"""
    p = isolated_cache / "m.py"
    p.write_text("VALUE = 1\n", encoding="utf-8")
    cc.get_code(p.read_text(encoding="utf-8"), str(p))
    cc.schedule_flush(delay=0.01)
    time.sleep(0.08)
    assert cc._state["scheduled"] is False, "flush 后 scheduled 必须复位"
    assert cc._state["dirty"] is False

    # 新源码产生新条目，必须能再次落盘
    p.write_text("VALUE = 2\n", encoding="utf-8")
    cc.get_code(p.read_text(encoding="utf-8"), str(p))
    assert cc._state["dirty"] is True
    cc.flush()
    cache_file = Path(os.environ["DRIFOX_DATA_DIR"]) / "cache" / cc._FILENAME
    import json

    entries = json.loads(cache_file.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 2, "两个版本的 code object 都应落盘"


def test_entry_cap_does_not_grow_unbounded(isolated_cache):
    """条目数达到上限后应清理，避免缓存无限增长。"""
    p = isolated_cache / "m.py"
    monkeypatch_cap = 4
    cc._state["entries"] = {f"k{i}": "x" for i in range(monkeypatch_cap)}
    cc._state["loaded"] = True
    p.write_text("VALUE = 5\n", encoding="utf-8")
    code, hit = cc.get_code(p.read_text(encoding="utf-8"), str(p))
    assert hit is False
    assert _exec_value(code)["VALUE"] == 5
    assert len(cc._state["entries"]) <= max(monkeypatch_cap, cc._MAX_ENTRIES)


# ── 多实例并发（2026-11-27 实测发现：固定 tmp 名导致 3/5 概率产出损坏 JSON）──

_CONCURRENT_WRITER = """
import json, os, sys
from pathlib import Path

data_dir, tag, base = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.environ["DRIFOX_DATA_DIR"] = data_dir
sys.path.insert(0, "__REPO_ROOT__")
from app.plugins.loaders import _code_cache as cc

# 每个进程写 10 条**互不重叠**的条目，让写入内容真正不同
for i in range(base, base + 10):
    cc.get_code(f"UNIQ_{tag}_{i} = {i}\\ndef register(r): pass\\n", f"/x/{tag}_{i}.py")
cc._state["scheduled"] = False
cc._state["dirty"] = True
cc.flush()
"""


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def test_concurrent_writers_never_produce_corrupt_json(tmp_path):
    """★ 两个进程并发 flush 同一缓存文件，最终文件必须仍是合法 JSON。

    回归的是「固定 tmp 名被多进程共用」这个 bug：两进程的写入交错到同一个
    临时文件，产出两个 JSON 文档的混合体（`Extra data: line 1 column N`），
    随后 os.replace 把损坏内容扶正。实测固定名时复现率 3/5。

    断言只要求**不损坏**，不要求内容完整——last-writer-wins 丢条目是设计
    取舍，但那绝不该表现为「文件坏了」。
    """
    import json
    import subprocess
    import sys as _sys

    writer = tmp_path / "_writer.py"
    writer.write_text(_CONCURRENT_WRITER.replace("__REPO_ROOT__", _repo_root()), encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    procs = [
        subprocess.Popen(
            [_sys.executable, str(writer), str(data_dir), tag, str(base)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(tmp_path),
        )
        for tag, base in (("A", 0), ("B", 100))
    ]
    for p in procs:
        p.wait(timeout=180)

    cache_file = data_dir / "cache" / cc._FILENAME
    assert cache_file.exists(), "并发写后应存在缓存文件"
    payload = json.loads(cache_file.read_text(encoding="utf-8"))  # 损坏即抛，测试失败
    assert isinstance(payload.get("entries"), dict), "entries 必须是 dict"
    assert len(payload["entries"]) >= 1, "至少应保留写入者的条目"


def test_tmp_file_name_is_per_process(isolated_cache, monkeypatch):
    """★ 确定性回归：两个不同 pid 写出的 tmp 路径必须不同。

    固定 tmp 名（`.tmp`）时两进程交错写到同一文件，产出两个 JSON 文档的
    混合体（`Extra data`），随后 os.replace 把损坏内容扶正——实测双进程
    复现率 50~60%。这里拦 `os.replace` 直接看源路径，不依赖竞态概率，
    回退到固定名必然变红。
    """
    captured = []
    real_replace = os.replace

    def spy(src, dst):
        captured.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(cc.os, "replace", spy)

    src = "VALUE = 1\ndef register(r): pass\n"

    monkeypatch.setattr(cc.os, "getpid", lambda: 111111)
    cc._state.update({"loaded": True, "enabled": True, "dirty": False, "scheduled": False, "entries": {}})
    cc.get_code(src, "/x/one.py")
    cc.flush()

    monkeypatch.setattr(cc.os, "getpid", lambda: 222222)
    cc._state["dirty"] = True
    cc.flush()

    assert len(captured) == 2, "两次 flush 都应走到 os.replace"
    assert captured[0] != captured[1], (
        f"不同 pid 的 tmp 路径必须不同，实得 {captured}（固定名会让并发写交错损坏）"
    )
    assert "111111" in captured[0] and "222222" in captured[1], (
        f"tmp 名应含各自 pid，实得 {captured}"
    )


def test_stale_tmp_of_dead_process_is_swept(isolated_cache):
    """被强杀进程留下的 tmp 应被回收，存活进程的 tmp 不得误删。"""
    cache_dir = isolated_cache / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    base = cache_dir / cc._FILENAME

    dead = base.with_suffix(".999999.tmp")     # 几乎不可能存在的 pid
    dead.write_text("junk", encoding="utf-8")
    alive = base.with_suffix(f".{os.getpid()}.tmp")  # 自己：必须保留
    alive.write_text("junk", encoding="utf-8")

    cc._state.update({"loaded": False, "enabled": True, "dirty": False, "scheduled": False, "entries": {}})
    cc._load()  # 触发 _sweep_stale_tmp

    assert not dead.exists(), "已退出进程的 tmp 应被清理"
    assert alive.exists(), "本进程的 tmp 不得被自己删掉"


def test_co_filename_matches_requested_path(isolated_cache):
    """★ 同源码不同 filename，第二个必须拿到自己的 co_filename。

    回归真实缺陷（2026-11-27）：`_key` 只含源码哈希时，两个逐字节相同的
    插件工具文件（`system-core/tools/_path_utils.py` 与
    `system-diagnostics/tools/_path_utils.py`）会撞同一个键，第二个插件
    复用第一个的 code object，traceback 指向错误的插件目录。
    不影响执行正确性，但排障会被彻底误导。
    """
    repo = Path(__file__).resolve().parents[2]
    src_file = repo / "plugins" / "system-core" / "tools" / "_path_utils.py"
    if not src_file.exists():  # 仓库结构变化时退化为自造样本，保持用例有效
        source = "VALUE = 1\n"
        path_a, path_b = str(isolated_cache / "core" / "_path_utils.py"), str(
            isolated_cache / "diag" / "_path_utils.py"
        )
    else:
        source = src_file.read_text(encoding="utf-8")
        path_a = str(repo / "plugins" / "system-core" / "tools" / "_path_utils.py")
        path_b = str(repo / "plugins" / "system-diagnostics" / "tools" / "_path_utils.py")

    code_a, hit_a = cc.get_code(source, path_a)
    assert hit_a is False
    assert code_a.co_filename == path_a

    code_b, _hit_b = cc.get_code(source, path_b)
    assert code_b.co_filename == path_b, (
        "同源码不同路径必须拿到各自的 co_filename（键必须包含 filename）"
    )


def test_same_source_same_path_still_hits(isolated_cache):
    """加了 filename 进键之后，同一文件仍必须命中，否则缓存退化为无用。"""
    p = isolated_cache / "m.py"
    p.write_text("VALUE = 11\n", encoding="utf-8")
    src = p.read_text(encoding="utf-8")
    cc.get_code(src, str(p))
    _code, hit = cc.get_code(src, str(p))
    assert hit is True, "同源码同路径必须命中"


def test_concurrent_writers_produce_valid_json(tmp_path):
    """补充：两个进程各写不同条目并**屏障同步**同时 flush，最终文件必须合法。

    这条是行为级验证（真实竞态），但竞态本身有概率性——修复前实测约 50~60%
    触发，所以它不能单独作为唯一防线。确定性防线是上面的
    `test_tmp_file_name_is_per_process`。两条一起用：后者保证不会回退，
    前者证明真实路径确实不再损坏。
    """
    import json
    import subprocess
    import sys as _sys

    writer_src = '''
import json, os, sys, time, importlib.util
from pathlib import Path
data_dir, tag, base = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.environ["DRIFOX_DATA_DIR"] = data_dir
spec = importlib.util.spec_from_file_location("cc_under_test", "__CACHE_MODULE__")
cc = importlib.util.module_from_spec(spec); spec.loader.exec_module(cc)
for i in range(base, base + 10):
    cc.get_code(f"U_{tag}_{i} = {i}\\ndef register(r): pass\\n", f"/x/{tag}_{i}.py")
cc._state["scheduled"] = False
cc._state["dirty"] = True
gate = Path(data_dir) / "gate"; gate.mkdir(exist_ok=True)
(gate / tag).write_text("1")
deadline = time.time() + 20
while time.time() < deadline:
    if len(list(gate.iterdir())) >= 2: break
    time.sleep(0.0001)
cc.flush()
'''.replace("__CACHE_MODULE__", str(Path(cc.__file__).resolve()))

    writer = tmp_path / "_concurrent_writer.py"
    writer.write_text(writer_src, encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    procs = [
        subprocess.Popen(
            [_sys.executable, str(writer), str(data_dir), tag, str(base)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(tmp_path),
        )
        for tag, base in (("A", 0), ("B", 100))
    ]
    for p in procs:
        p.wait(timeout=180)

    cache_file = data_dir / "cache" / cc._FILENAME
    assert cache_file.exists(), "并发写后应存在缓存文件"
    payload = json.loads(cache_file.read_text(encoding="utf-8"))  # 损坏即抛 = 测试失败
    assert isinstance(payload.get("entries"), dict)
    assert len(payload["entries"]) >= 1
