# -*- coding: utf-8 -*-
"""场景 E：MCP 探活压测（本地 stub 端点 + probe_servers_background(force=True)）。

压测目标：
- MCPClientManager.get_instance()（mcp_tools.py:732/749）→
  probe_servers_background(force=True)（:1937，必须 force——PROBE_TTL 600s
  > 本场景的探活间隔，不 force 会在 TTL 内静默跳过）→ daemon asyncio loop
  → _probe_all → _probe_one（HTTP initialize）
- 失败重试监控：stub 挂掉后反复探活得到 unreachable，作为采样项观察
  probe 线程 / asyncio loop / httpx 连接栈的内存行为

实现：
- 进程内起停本地 HTTP stub（ThreadingHTTPServer，任意路径返回 200 initialize
  响应），无主线程事件循环依赖
- PluginManager.get_mcp_servers 被 patch 为返回 1-2 个 http 型 stub 配置
  （_configured_servers 经此读取，无需写 app.config）

暴露标注：
- 探活结论写入 mgr._probes（name → state/detail/probed_at），不落 CSV；
  summary 带 probe_fired / probe_ok / probe_fail 计数
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List

from .sampler import Sample, env_full_mode, start_tracemalloc, stop_tracemalloc

_MCP_INIT_RESPONSE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "perf-stub", "version": "0.1.0"},
    },
}


class _StubHandler(BaseHTTPRequestHandler):
    """任意 GET/POST/DELETE 都回 200 initialize 响应（探活只判 200/202）。"""

    alive: bool = True

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002,N802
        pass

    def _respond(self) -> None:
        if not _StubHandler.alive:
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps(_MCP_INIT_RESPONSE, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._respond()

    def do_POST(self) -> None:  # noqa: N802
        self._respond()

    def do_DELETE(self) -> None:  # noqa: N802
        self._respond()


def _start_stub(port: int = 0):
    """起本地 MCP stub；返回 (httpd, port)。杀进程即 stub 挂掉。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _StubHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    return httpd, port


def _patch_plugin_servers(servers: List[dict]):
    """把 PluginManager patch 成返回 stub 配置的已初始化实例；返回还原函数。

    _configured_servers 走 pm.is_initialized() + pm.get_mcp_servers()，
    两者都要 patch（真实 PM 在 runner 进程未初始化）。
    """
    from types import SimpleNamespace

    from app.plugins.managers import plugin_manager as pm_module

    original_get_instance = pm_module.PluginManager.get_instance

    fake_pm = SimpleNamespace(
        is_initialized=lambda: True,
        get_mcp_servers=lambda: list(servers),
    )
    pm_module.PluginManager.get_instance = staticmethod(lambda: fake_pm)
    return lambda: setattr(pm_module.PluginManager, "get_instance", original_get_instance)


def _probes_snapshot(mgr) -> Dict[str, Dict[str, Any]]:
    with mgr._lock:
        return {k: dict(v) for k, v in mgr._probes.items()}


def _wait_probe_settled(mgr, names: List[str], timeout_s: float = 10.0) -> Dict[str, str]:
    """等 names 的探活结论出现（或超时）；返回 name → state。"""
    deadline = time.time() + timeout_s
    states: Dict[str, str] = {}
    while time.time() < deadline:
        snap = _probes_snapshot(mgr)
        states = {n: str(snap.get(n, {}).get("state", "")) for n in names}
        if all(states.get(n) for n in names):
            return states
        time.sleep(0.3)
    return states


def run_mcp_probe_scenario(
    *,
    progress_cb: Callable[[int, Sample], None],
    duration_sec: float,
) -> dict:
    """执行 MCP 探活压测；返回基线摘要 dict。"""
    from app.tools.mcp_tools import MCPClientManager

    start_tracemalloc()
    t0 = time.time()

    httpd, stub_port = _start_stub()
    stub_url = f"http://127.0.0.1:{stub_port}/mcp"
    servers = [
        {"name": "perf-stub-a", "type": "http", "url": stub_url, "enabled": True},
        {"name": "perf-stub-b", "type": "http", "url": f"{stub_url}/b", "enabled": True},
    ]
    restore_servers = _patch_plugin_servers(servers)

    mgr = MCPClientManager.get_instance()
    samples: List[Sample] = []
    last_sample_at = t0
    probe_fired = 0
    probe_ok = 0
    probe_fail = 0
    stub_alive = True
    fail_round_started: Optional[float] = None
    fail_round_probes: List[str] = []

    def _maybe_sample(now: float, force: bool = False) -> None:
        nonlocal last_sample_at
        if force or now - last_sample_at >= max(5.0, float(os.environ.get("PERF_SAMPLE_INTERVAL", "60"))):
            from .sampler import take_sample

            s = take_sample("E_mcp_probe", probe_fired, t0)
            samples.append(s)
            progress_cb(probe_fired, s)
            last_sample_at = now

    try:
        while True:
            now = time.time()
            if now - t0 >= duration_sec:
                break

            # 探活必须 force：PROBE_TTL 600s > 本场景 2min 级间隔
            mgr.probe_servers_background(force=True)
            probe_fired += 1

            states = _wait_probe_settled(mgr, [s["name"] for s in servers])
            ok_n = sum(1 for v in states.values() if v == "ok")
            fail_n = len(states) - ok_n
            if stub_alive:
                probe_ok += ok_n
                probe_fail += fail_n
            else:
                probe_fail += len(states)
                fail_round_probes.append(json.dumps(states, ensure_ascii=False))

            # 中途杀 stub（60% 时长处，只做一次）：失败重试监控采样项
            if stub_alive and (now - t0) >= duration_sec * 0.6:
                stub_alive = False
                _StubHandler.alive = False
                httpd.shutdown()
                fail_round_started = time.time()
                print("  [e] stub killed → 失败重试监控开始", flush=True)

            _maybe_sample(time.time())
            time.sleep(2.0)
    finally:
        restore_servers()
        try:
            httpd.server_close()
        except Exception:
            pass
        _maybe_sample(time.time(), force=True)

    elapsed = time.time() - t0
    rss_delta_mb = samples[-1].rss_mb - samples[0].rss_mb
    summary = {
        "scenario": "E_mcp_probe",
        "iterations": probe_fired,
        "elapsed_sec": elapsed,
        "probe_fired": probe_fired,
        "probe_ok": probe_ok,
        "probe_fail": probe_fail,
        "fail_rounds": len(fail_round_probes),
        "fail_round_samples": fail_round_probes[:6],
        "stub_url": stub_url,
        "rss_mb_first": samples[0].rss_mb,
        "rss_mb_last": samples[-1].rss_mb,
        "rss_delta_mb": rss_delta_mb,
        "qobject_first": samples[0].qobject_count,
        "qobject_last": samples[-1].qobject_count,
        "qobject_delta": samples[-1].qobject_count - samples[0].qobject_count,
        "samples": samples,
    }
    stop_tracemalloc()
    return summary


if __name__ == "__main__":
    duration = 30.0
    if env_full_mode():
        duration = 1800.0
    print(f"[scenario_e] 启动，时长 {duration}s")

    def _cb(i: int, s: Sample) -> None:
        print(
            f"  probe={i}  rss={s.rss_mb:.1f}MB  qobj={s.qobject_count}  "
            f"elapsed={s.elapsed_sec:.0f}s  tm_cur={s.tracemalloc_current_mb:.2f}MB"
        )

    summary = run_mcp_probe_scenario(progress_cb=_cb, duration_sec=duration)
    print(
        f"[scenario_e] 完成：fired={summary['probe_fired']} ok={summary['probe_ok']} "
        f"fail={summary['probe_fail']} RSS Δ={summary['rss_delta_mb']:.1f}MB"
    )
    sys.exit(0)
