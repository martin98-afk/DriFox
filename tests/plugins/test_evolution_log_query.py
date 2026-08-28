# -*- coding: utf-8 -*-
"""evolution_log_query 测试。

钉死 list / query / context / triage 四 operation 的行为 + 纯函数。
用 ``monkeypatch`` 替换模块级 ``_logs_dir`` 与 ``_read_journal``，避免污染用户家目录。
"""

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    p = (
        Path(__file__).parent.parent.parent
        / ".drifox"
        / "plugins"
        / "self-evolver"
        / "tools"
        / "evolution_log_query.py"
    )
    spec = importlib.util.spec_from_file_location("evolution_log_query", p)
    assert spec and spec.loader, f"无法加载 evolution_log_query: {p}"
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def lq(tmp_path):
    """加载模块 + 注入 tmp_path 日志目录 + 清空 journal 副作用。"""
    mod = _load_module()
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    mod._logs_dir = lambda: log_dir  # type: ignore[assignment]
    mod._read_journal = lambda: []  # type: ignore[assignment]
    return mod


def _write_log(log_dir: Path, name: str, lines: list[str]) -> Path:
    p = log_dir / name
    p.write_bytes(("\n".join(lines) + "\n").encode("gbk", errors="replace"))
    return p


# ── 纯函数 ──────────────────────────────────────────


def test_parse_ts_valid_and_invalid(lq):
    assert lq._parse_ts("2026-08-28 10:14:32") is not None
    assert lq._parse_ts("not a time") is None
    assert lq._parse_ts(None) is None
    assert lq._parse_ts("") is None


def test_format_size(lq):
    assert lq._format_size(500) == "500 B"
    assert lq._format_size(2048) == "2.0 KB"
    assert lq._format_size(2 * 1024 * 1024) == "2.0 MB"


def test_read_tail_gbk_and_missing(lq, tmp_path):
    p = tmp_path / "x.log"
    p.write_bytes("a\nb\nc\n".encode("gbk"))
    assert lq._read_tail(p, lines=10) == ["a", "b", "c"]

    bad = tmp_path / "bad.log"
    bad.write_bytes(b"good\n\xff\xfe bad\n")
    out = lq._read_tail(bad, lines=10)
    assert out  # GBK 容错不抛

    missing = tmp_path / "missing.log"
    assert lq._read_tail(missing) == []


def test_detect_tool_loops(lq):
    tail = [f"line {i} Executing tool: foo" for i in range(10)] + [
        "line 11 Executing tool: bar",
        "line 12 no tool",
    ]
    loops = lq._detect_tool_loops(tail, threshold=8)
    assert ("foo", 10) in loops


# ── list ──────────────────────────────────────────


def test_list_logs_includes_only_dot_log_files(lq):
    log_dir = lq._logs_dir()
    _write_log(log_dir, "all.log", ["x"])
    _write_log(log_dir, "mcp.log", ["y"])
    (log_dir / "notes.txt").write_text("ignore me")
    out = lq._list_logs()
    assert "all.log" in out
    assert "mcp.log" in out
    assert "notes.txt" not in out


def test_list_logs_empty_dir_message(lq):
    out = lq._list_logs()
    assert "无 .log 文件" in out


# ── query ──────────────────────────────────────────


@pytest.fixture
def fake_logs(lq):
    log_dir = lq._logs_dir()
    # 注意：DriFox 实际日志格式是「单空格 | 单空格」，不要用对齐多空格
    lines = [
        "2026-08-28 10:00:00 | INFO | core.backend | [ChatBackend] ready",
        "2026-08-28 10:01:00 | WARNING | core.backend | [ChatBackend] slow response 5s",
        "2026-08-28 10:02:00 | ERROR | core.backend | [ChatBackend] upstream timeout",
        "2026-08-28 10:03:00 | DEBUG | core.backend | [ChatBackend] retry 1",
        "2026-08-28 10:04:00 | ERROR | tools.x | [Tool] something failed",
    ]
    _write_log(log_dir, "all.log", lines)
    _write_log(log_dir, "mcp.log", lines)
    _write_log(log_dir, "plugins.log", ["2026-08-28 09:00:00 | INFO | x | y"])
    return log_dir


def test_query_default_summary(lq, fake_logs):
    out = lq._query(
        subsystem=None,
        level=None,
        since=None,
        until=None,
        pattern=None,
        head=2,
        tail_n=2,
        max_hits=500,
    )
    assert "[query] subsystem=all" in out
    assert "级别分布" in out
    assert "前 2 行" in out
    assert "后 2 行" in out
    assert "提示：用 operation=context" in out


def test_query_filters_by_level(lq, fake_logs):
    out = lq._query(
        subsystem="all",
        level="ERROR",
        since=None,
        until=None,
        pattern=None,
        head=3,
        tail_n=3,
        max_hits=500,
    )
    assert "ERROR" in out
    assert "WARNING" not in out
    assert "INFO" not in out


def test_query_filters_by_subsystem(lq, fake_logs):
    out = lq._query(
        subsystem="mcp",
        level=None,
        since=None,
        until=None,
        pattern=None,
        head=3,
        tail_n=3,
        max_hits=500,
    )
    assert "subsystem=mcp" in out
    assert "[ChatBackend] ready" in out


def test_query_filters_by_pattern(lq, fake_logs):
    out = lq._query(
        subsystem="all",
        level=None,
        since=None,
        until=None,
        pattern=r"timeout",
        head=3,
        tail_n=3,
        max_hits=500,
    )
    assert "命中" in out
    assert "timeout" in out


def test_query_filters_by_time_window(lq, fake_logs):
    out = lq._query(
        subsystem="all",
        level=None,
        since="2026-08-28 10:02:00",
        until="2026-08-28 10:03:30",
        pattern=None,
        head=3,
        tail_n=3,
        max_hits=500,
    )
    assert "upstream timeout" in out
    assert "slow response" not in out
    assert "ready" not in out


def test_query_unknown_subsystem(lq, fake_logs):
    out = lq._query(
        subsystem="not_a_real_one",
        level=None,
        since=None,
        until=None,
        pattern=None,
        head=3,
        tail_n=3,
        max_hits=500,
    )
    assert "未知 subsystem" in out


def test_query_invalid_pattern(lq, fake_logs):
    out = lq._query(
        subsystem="all",
        level=None,
        since=None,
        until=None,
        pattern="[unclosed",
        head=3,
        tail_n=3,
        max_hits=500,
    )
    assert "pattern 编译失败" in out


def test_query_truncates_by_max_hits(lq, fake_logs):
    out = lq._query(
        subsystem="all",
        level=None,
        since=None,
        until=None,
        pattern=None,
        head=1,
        tail_n=1,
        max_hits=2,
    )
    assert "已截断" in out


def test_query_no_hits_message(lq, fake_logs):
    out = lq._query(
        subsystem="all",
        level=None,
        since=None,
        until=None,
        pattern=r"never_match_xyz",
        head=3,
        tail_n=3,
        max_hits=500,
    )
    assert "未命中" in out


# ── context ──────────────────────────────────────────


def test_context_last_line(lq, fake_logs):
    out = lq._context(line_no=1, n=2, subsystem="all")
    assert "line_no=1" in out
    assert "←" in out  # 锚点标记


def test_context_out_of_range(lq, fake_logs):
    out = lq._context(line_no=99999, n=2, subsystem="all")
    assert "超出已读范围" in out


def test_context_invalid_line_no(lq, fake_logs):
    out = lq._context(line_no=0, n=2, subsystem="all")
    assert "必须 ≥1" in out


# ── triage ──────────────────────────────────────────


def test_triage_detects_error_and_loop(lq):
    """triage 必须命中 ERROR 行 + 工具调用循环（独立构造数据）。"""
    log_dir = lq._logs_dir()
    lines = ["2026-08-28 10:00:00 | INFO | core.x | [ToolExecutor] Executing tool: evolution_journal"] * 10 + [
        "2026-08-28 10:01:00 | ERROR | core.x | boom"
    ]
    _write_log(log_dir, "all.log", lines)
    out = lq._triage(lines=500, plugin_filter=None)
    assert "ERROR/CRITICAL" in out
    assert "boom" in out
    assert "工具调用循环" in out
    assert "evolution_journal" in out


def test_triage_missing_log(lq):
    out = lq._triage(lines=500, plugin_filter=None)
    assert "未找到系统日志" in out


# ── 入口 _impl ──────────────────────────────────────────


def test_impl_routes_list(lq):
    r = lq._impl({}, operation="list")
    assert r is not None


def test_impl_routes_unknown_op(lq):
    r = lq._impl({}, operation="bogus")
    assert r is not None
    err = getattr(r, "error", "")
    assert "可用 list/query/context/triage" in err


def test_impl_returns_for_each_op(lq):
    log_dir = lq._logs_dir()
    _write_log(
        log_dir,
        "all.log",
        ["2026-08-28 10:00:00 | INFO | x | [ToolExecutor] Executing tool: demo"] * 9
        + ["2026-08-28 10:01:00 | ERROR | x | boom"],
    )
    for op in ("list", "query", "context", "triage"):
        r = lq._impl({}, operation=op)
        assert r is not None, f"{op} 未返回结果"
