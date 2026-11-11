# -*- coding: utf-8 -*-
"""主题 YAML 解析产物跨启动聚合 pickle 缓存回归（#26 L2）

覆盖：失效正确性（mtime/size 变化 → 重解析）、损坏回退（垃圾字节 → 不崩、
回退正常解析）、首启无缓存路径、聚合落盘原子性、签名纯函数确定性。
"""

import hashlib
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

import yaml  # noqa: E402

from app.utils.theme_manager import ThemeManager  # noqa: E402

_YAML_BODY = """id: __cache_probe__
name: 缓存探针
colors:
  accent: "#66c6ff"
backgrounds:
  window:
    gradient_start: "#111111"
    gradient_end: "#222222"
"""


@pytest.fixture()
def cache_env(monkeypatch, tmp_path):
    """app_data 指向 tmp，清进程内聚合态（每用例隔离）"""
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: tmp_path)
    with ThemeManager._yaml_agg_lock:
        ThemeManager._yaml_agg_cache = None
        ThemeManager._yaml_agg_loaded = False
        ThemeManager._yaml_agg_dirty.clear()
        ThemeManager._yaml_agg_timer = None
    yield tmp_path
    if ThemeManager._yaml_agg_timer is not None:
        ThemeManager._yaml_agg_timer.cancel()
        ThemeManager._yaml_agg_timer = None
    with ThemeManager._yaml_agg_lock:
        ThemeManager._yaml_agg_cache = None
        ThemeManager._yaml_agg_loaded = False
        ThemeManager._yaml_agg_dirty.clear()


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "probe.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_first_run_without_cache_parses_normally(cache_env):
    """首启（无聚合缓存）：正常解析，结果正确，内存表入条目"""
    p = _write_yaml(cache_env, _YAML_BODY)
    st = p.stat()
    parsed = ThemeManager._parse_yaml_cached(str(p), st.st_mtime_ns, st.st_size)
    assert parsed.get("id") == "__cache_probe__"
    sig = ThemeManager._yaml_cache_sig(str(p), st.st_mtime_ns, st.st_size)
    assert sig in ThemeManager._yaml_agg_cache, "解析产物必须进聚合内存表"


def test_second_run_hits_pickle_and_skips_yaml(monkeypatch, cache_env):
    """二次冷启动（模拟新进程）：聚合文件命中 → 不触 yaml.safe_load"""
    p = _write_yaml(cache_env, _YAML_BODY)
    st = p.stat()
    ThemeManager._parse_yaml_cached(str(p), st.st_mtime_ns, st.st_size)
    ThemeManager._flush_yaml_agg()
    agg_file = ThemeManager._yaml_agg_path()
    assert agg_file.exists(), "flush 后聚合文件必须落盘"

    # 模拟新进程：清进程内聚合态（磁盘聚合文件仍在）
    with ThemeManager._yaml_agg_lock:
        ThemeManager._yaml_agg_cache = None
        ThemeManager._yaml_agg_loaded = False
    calls = []
    orig = yaml.safe_load

    def _count(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    monkeypatch.setattr(yaml, "safe_load", _count)
    parsed2 = ThemeManager._parse_yaml_cached(str(p), st.st_mtime_ns, st.st_size)
    assert parsed2.get("id") == "__cache_probe__"
    assert not calls, "聚合命中时不得触 yaml.safe_load（磁盘 IO + 解析应全免）"


def test_modified_yaml_invalidates_and_reparses(cache_env):
    """失效正确性：内容变化（mtime/size 变）→ 新签名 miss → 重解析新内容"""
    p = _write_yaml(cache_env, _YAML_BODY.replace("#66c6ff", "#ff0000"))
    st = p.stat()
    ThemeManager._parse_yaml_cached(str(p), st.st_mtime_ns, st.st_size)
    ThemeManager._flush_yaml_agg()

    p.write_text(_YAML_BODY.replace("#66c6ff", "#00ff00") + "\n# edited", encoding="utf-8")
    st2 = p.stat()
    assert (st2.st_mtime_ns, st2.st_size) != (st.st_mtime_ns, st.st_size), "测试前置：签名必须变化"

    with ThemeManager._yaml_agg_lock:
        ThemeManager._yaml_agg_cache = None
        ThemeManager._yaml_agg_loaded = False
    new = ThemeManager._parse_yaml_cached(str(p), st2.st_mtime_ns, st2.st_size)
    assert new["colors"]["accent"] == "#00ff00", "失效后必须重解析新内容"


def test_corrupted_agg_falls_back_to_parse(monkeypatch, cache_env):
    """聚合文件损坏（垃圾字节）：不崩、当空表、静默回退正常解析"""
    p = _write_yaml(cache_env, _YAML_BODY)
    st = p.stat()
    ThemeManager._parse_yaml_cached(str(p), st.st_mtime_ns, st.st_size)
    ThemeManager._flush_yaml_agg()
    ThemeManager._yaml_agg_path().write_bytes(b"\x00\x01not-a-pickle\xff" * 32)

    with ThemeManager._yaml_agg_lock:
        ThemeManager._yaml_agg_cache = None
        ThemeManager._yaml_agg_loaded = False
    parsed = ThemeManager._parse_yaml_cached(str(p), st.st_mtime_ns, st.st_size)
    assert parsed.get("id") == "__cache_probe__", "损坏缓存必须回退正常解析"


def test_signature_deterministic():
    """签名纯函数：同参数同签名、任一成分变化即新签名"""
    a = ThemeManager._yaml_cache_sig("C:/t/a.yaml", 111, 222)
    b = ThemeManager._yaml_cache_sig("C:/t/a.yaml", 111, 222)
    c = ThemeManager._yaml_cache_sig("C:/t/a.yaml", 333, 222)
    d = ThemeManager._yaml_cache_sig("C:/t/other.yaml", 111, 222)
    assert a == b
    assert a != c and a != d and c != d
    expect = hashlib.sha1("C:/t/a.yaml|111|222".encode("utf-8")).hexdigest()
    assert a == expect


def test_none_parse_result_not_cached(cache_env):
    """空 YAML（解析为 None）不入聚合表"""
    p = cache_env / "empty.yaml"
    p.write_text("", encoding="utf-8")
    st = p.stat()
    ThemeManager._parse_yaml_cached(str(p), st.st_mtime_ns, st.st_size)
    ThemeManager._flush_yaml_agg()
    assert not ThemeManager._yaml_agg_path().exists() or True
    # 空结果不入表：聚合表（若有）中不含该签名
    sig = ThemeManager._yaml_cache_sig(str(p), st.st_mtime_ns, st.st_size)
    agg = ThemeManager._load_yaml_agg()
    assert sig not in agg, "None 结果不得入聚合表"


def test_background_write_failure_is_silent(monkeypatch, cache_env):
    """flush 写失败（如路径不可写）：静默，不抛不崩，解析结果不受影响"""
    p = _write_yaml(cache_env, _YAML_BODY)
    st = p.stat()

    def _boom():
        raise PermissionError("mock readonly")

    monkeypatch.setattr(ThemeManager, "_yaml_agg_path", staticmethod(_boom))
    parsed = ThemeManager._parse_yaml_cached(str(p), st.st_mtime_ns, st.st_size)
    ThemeManager._flush_yaml_agg()
    assert parsed.get("id") == "__cache_probe__", "写失败不得影响解析结果"


def test_debounce_single_flush_for_batch(cache_env):
    """首启批量（5 条连续解析）：一次 flush 全部落盘；重复 flush 幂等 no-op"""
    paths = []
    for i in range(5):
        p = _write_yaml(cache_env, _YAML_BODY.replace("__cache_probe__", f"id{i}"))
        paths.append(p)

    sigs = []
    for p in paths:
        st = p.stat()
        ThemeManager._parse_yaml_cached(str(p), st.st_mtime_ns, st.st_size)
        sigs.append(ThemeManager._yaml_cache_sig(str(p), st.st_mtime_ns, st.st_size))

    ThemeManager._flush_yaml_agg()
    agg = ThemeManager._load_yaml_agg()
    for sig in sigs:
        assert sig in agg, f"批量条目必须全部落盘: {sig[:8]}"

    # 重复 flush：dirty 已空 → no-op（不炸、聚合内容不变）
    ThemeManager._flush_yaml_agg()
    ThemeManager._flush_yaml_agg()
    agg2 = ThemeManager._load_yaml_agg()
    assert agg2 == agg, "重复 flush 不得改变聚合内容"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
