# -*- coding: utf-8 -*-
"""evolution_journal 日志读取测试。

钉死：
- ``_system_log_tail`` 默认读 ``~/.drifox/logs/all.log``（取代旧 llm_chatter.log）
- 传入 ``subsystem`` 参数时读对应分文件
- GBK 编码容错读取、尾部 N 行
"""

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    """加载 self-evolver 插件内的 evolution_journal 工具模块。"""
    p = Path(__file__).parent.parent.parent / ".drifox" / "plugins" / "self-evolver" / "tools" / "evolution_journal.py"
    spec = importlib.util.spec_from_file_location("evolution_journal", p)
    assert spec and spec.loader, f"无法加载 evolution_journal: {p}"
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def evo_module():
    return _load_module()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """把 ``Path.home()`` 重定向到 tmp_path，便于写入日志文件而不污染用户家目录。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".drifox" / "logs").mkdir(parents=True)
    return tmp_path


def test_default_subsystem_reads_all_log(evo_module, fake_home):
    """不传 subsystem 时读 all.log（取代旧 llm_chatter.log）。"""
    (fake_home / ".drifox" / "logs" / "all.log").write_bytes("line1\nline2\nline3\n".encode("gbk"))
    out = evo_module._system_log_tail(lines=10)
    assert out == ["line1", "line2", "line3"]


def test_does_not_read_obsolete_llm_chatter_log(evo_module, fake_home):
    """旧 llm_chatter.log 即使存在也不再被读取（默认走 all.log）。"""
    (fake_home / ".drifox" / "logs" / "llm_chatter.log").write_bytes("stale\n".encode("gbk"))
    out = evo_module._system_log_tail(lines=10)
    assert out == []


def test_subsystem_routes_to_subfile(evo_module, fake_home):
    """传 subsystem=mcp 读 mcp.log。"""
    (fake_home / ".drifox" / "logs" / "mcp.log").write_bytes("mcp-line\n".encode("gbk"))
    (fake_home / ".drifox" / "logs" / "all.log").write_bytes("all-line\n".encode("gbk"))
    out = evo_module._system_log_tail(lines=10, subsystem="mcp")
    assert out == ["mcp-line"]


def test_missing_file_returns_empty(evo_module, fake_home):
    """日志文件不存在返回空列表（不抛）。"""
    assert evo_module._system_log_tail(lines=10) == []
    assert evo_module._system_log_tail(lines=10, subsystem="plugins") == []


def test_tail_limit_respected(evo_module, fake_home):
    """尾部 N 行限制生效。"""
    (fake_home / ".drifox" / "logs" / "all.log").write_bytes(
        ("\n".join(f"line{i}" for i in range(100)) + "\n").encode("gbk")
    )
    out = evo_module._system_log_tail(lines=5)
    assert out == [f"line{i}" for i in range(95, 100)]


def test_gbk_decode_fallback(evo_module, fake_home):
    """GBK 解码失败时容错返回（不抛异常）。"""
    # 写入非法 GBK 字节序列
    (fake_home / ".drifox" / "logs" / "all.log").write_bytes(b"good\n\xff\xfe bad\n")
    out = evo_module._system_log_tail(lines=10)
    assert out  # 至少能返回非空列表（不抛 OSError/UnicodeDecodeError）
