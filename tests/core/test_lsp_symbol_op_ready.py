# -*- coding: utf-8 -*-
"""[E9] LSP 符号操作：sleep(0.3) 轮询化 + did_open/did_close 保活引用计数。

守卫：
1. 就绪即时命中：server 推送后提前执行（耗时 << 300ms）
2. 兜底路径：server 永不推送，最多 ~0.3s 后照常执行且结果正确（等价原固定 sleep）
3. 保活窗口：同文件两次连续操作只 did_open 一次；窗口到期才 did_close
4. 等价性：异常路径仍关闭、client 不可用返回 None
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.lsp.lsp_client import LspClient  # noqa: E402
from app.core.lsp.lsp_config import LspServerConfig  # noqa: E402
from app.core.lsp.lsp_manager import LspManager  # noqa: E402

@pytest.fixture()
def fake_client(tmp_path):
    """可用状态的 LspClient：协议层 mock，文件操作走真实 tmp 文件；TTL 调小加速。"""
    cfg = LspServerConfig.from_dict("fake-lsp", {"command": "fake"}, "test-plugin")
    client = LspClient(cfg, str(tmp_path))
    client._started = True
    client._client = MagicMock()
    client._keepalive_ttl = 0.05
    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    pass\n", encoding="utf-8")
    yield client, str(f)
    for t in client._keepalive_closer_tasks.values():
        t.cancel()

@pytest.fixture()
def manager():
    return LspManager()

async def _async_true(c):
    return True

async def _noop_async(*a, **k):
    return None

def _wire_symbol_op(manager, client, result):
    """manager 路由到 fake client；client 符号操作返回固定结果并计数 open/close。"""
    manager.get_client_for_file = lambda fp: client
    manager._ensure_started = _async_true
    counts = {"did_open": 0, "did_close": 0, "op": 0}

    async def counting_open(fp, language=None):
        counts["did_open"] += 1

    async def counting_close(fp):
        counts["did_close"] += 1

    async def op(fp, *a):
        counts["op"] += 1
        return result

    client.did_open = counting_open
    client.did_close = counting_close
    client.go_to_definition = op
    client.find_references = op
    client.hover = op
    client.document_symbols = op
    return counts

def _drain_pending(client, loop):
    """等待并清理窗口内未到期的延时关闭任务（防 loop 关闭后 pending 警告）。"""
    pending = [t for t in client._keepalive_closer_tasks.values() if not t.done()]
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

def test_ready_hit_fast_path(fake_client, manager):
    """就绪即时命中：server 在 ~30ms 后推送 → 等待远小于 300ms。"""
    client, fp = fake_client
    opened_at = time.monotonic()

    async def push_later():
        await asyncio.sleep(0.03)
        uri = Path(fp).resolve().as_uri()
        client._diag_push_time[uri] = time.monotonic()

    async def main():
        task = asyncio.create_task(push_later())
        await manager._wait_index_ready(client, fp)
        elapsed = time.monotonic() - opened_at
        await task
        return elapsed

    loop = asyncio.new_event_loop()
    try:
        elapsed = loop.run_until_complete(main())
    finally:
        loop.close()
    assert elapsed < 0.2, f"就绪推送后应提前返回，实测等待 {elapsed * 1000:.0f}ms"

def test_timeout_fallback_still_executes(fake_client, manager):
    """兜底：server 永不推送 → 约 0.3s 后照常执行操作且结果正确。"""
    client, fp = fake_client
    counts = _wire_symbol_op(manager, client, result=[{"uri": "x"}])
    loop = asyncio.new_event_loop()
    try:
        t0 = time.monotonic()
        result = loop.run_until_complete(manager.find_references(fp, 1, 1))
        elapsed = time.monotonic() - t0
        _drain_pending(client, loop)
    finally:
        loop.close()
    assert result == [{"uri": "x"}], "超时兜底后操作必须照常执行"
    assert 0.28 <= elapsed < 0.6, f"兜底等待应≈0.3s，实测 {elapsed * 1000:.0f}ms"
    assert counts["op"] == 1 and counts["did_open"] == 1

def test_keepalive_single_open_for_consecutive_ops(fake_client, manager):
    """保活窗口：同文件两次连续操作只 did_open 一次；窗口到期才 did_close 一次。"""
    client, fp = fake_client
    counts = _wire_symbol_op(manager, client, result=None)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(manager.document_symbols(fp))
        loop.run_until_complete(manager.document_symbols(fp))
        assert counts["did_open"] == 1, f"窗口内连续操作应只 open 一次，实测 {counts['did_open']}"
        assert counts["did_close"] == 0, "窗口内不应触发 close"
        loop.run_until_complete(asyncio.sleep(0.2))  # TTL=0.05s，等窗口到期
        _drain_pending(client, loop)
    finally:
        loop.close()
    assert counts["did_close"] == 1, f"窗口到期应恰好 close 一次，实测 {counts['did_close']}"
    assert not client._keepalive_refs, "到期后引用应清空"

def test_keepalive_mtime_change_triggers_did_change(fake_client, manager):
    """保活期间文件被外部修改 → did_change 补偿（内容与重新 open 等价）。"""
    client, fp = fake_client
    counts = _wire_symbol_op(manager, client, result=None)
    did_change_calls = []

    async def counting_change(p):
        did_change_calls.append(p)

    client.did_change = counting_change
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(manager.document_symbols(fp))  # 第一轮（open，建立 mtime 基线）
        Path(fp).write_text("def bar():\n    pass\n", encoding="utf-8")  # 外部修改
        st = Path(fp).stat()
        os.utime(fp, (st.st_atime + 10, st.st_mtime + 10))  # 显式推进 mtime（两次写入间隔可能小于精度）
        loop.run_until_complete(manager.document_symbols(fp))  # 第二轮（复用 + did_change）
        _drain_pending(client, loop)
    finally:
        loop.close()
    assert len(did_change_calls) == 1, f"mtime 变化应补发 did_change，实测 {len(did_change_calls)}"

def test_release_on_op_exception(fake_client, manager):
    """异常路径：操作抛错仍走 release（窗口调度），到期 did_close。"""
    client, fp = fake_client

    async def boom(fp, *a):
        raise RuntimeError("op failed")

    manager.get_client_for_file = lambda p: client
    manager._ensure_started = _async_true
    client.did_open = _noop_async
    client.did_close = _noop_async
    client.go_to_definition = boom
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError):
            loop.run_until_complete(manager.go_to_definition(fp, 1, 1))
        assert client._keepalive_refs.get(str(Path(fp).resolve())) == 0
        loop.run_until_complete(asyncio.sleep(0.1))  # 等延时关闭
        _drain_pending(client, loop)
    finally:
        loop.close()
    assert not client._keepalive_refs

def test_client_unavailable_returns_none(manager):
    """client 不可用 → 返回 None（原语义）。"""
    manager.get_client_for_file = lambda p: None
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(manager.go_to_definition("x.py", 1, 1)) is None
    finally:
        loop.close()

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
