# -*- coding: utf-8 -*-
"""场景 F：窗口级会话开关压测（纯 HTTP 驱动运行中实例的 ui_test_server）。

为什么无 QApplication
---------------------
本场景不自己建窗口，而是驱动**已在运行**的 DriFox 实例（由冒烟档启动脚本
带 ``DRIFOX_UI_TEST_SERVER=host:port`` 起）。这样测的才是真实窗口生命周期：
新建会话 → 真实发送入口 → 流式渲染 → UI 关闭按钮 → 会话消失。

每轮（默认 300s）：
    new 会话 → 3×(send → 等 streaming 结束，60s 超时) → close → 确认 sessions 消失

采样口径（与场景 a/b/c 的差异，显式标注）：
- ``rss_mb`` 取 ``ui_memory.main_private_mb``（被测实例的 PrivateUsage，
  本进程只是 HTTP 客户端，自身 RSS 无意义）
- ``tracemalloc_*`` 全部置 None/0：跨进程不可用
- ``qobject_count`` 填 -1：跨进程不可测（判据只看 rss 曲线）

依赖：``DRIFOX_UI_TEST_SERVER``（``host:port``）。未设置时返回带 error 的
空 summary，不抛异常（runner 的 all 模式不会因此中断）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .sampler import Sample

ROUND_SEC = 300.0
STREAM_TIMEOUT_S = 60.0
SEND_COUNT_PER_ROUND = 3


def _endpoint() -> Optional[Tuple[str, int]]:
    raw = os.environ.get("DRIFOX_UI_TEST_SERVER", "").strip()
    if not raw:
        return None
    host, _, port = raw.partition(":")
    try:
        return (host or "127.0.0.1", int(port or 8765))
    except ValueError:
        return None


def _rpc(host: str, port: int, tool: str, args: Dict[str, Any], timeout: float = 90.0) -> Dict[str, Any]:
    """JSON-RPC tools/call；把 content[0].text 里的 JSON 还原成 dict。"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    req = urllib.request.Request(
        f"http://{host}:{port}/",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    result = body.get("result") or {}
    if result.get("isError"):
        return {"ok": False, "error": str((result.get("content") or [{}])[0].get("text") or "")}
    text = (result.get("content") or [{}])[0].get("text") or "{}"
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"raw": text}


def _state(host: str, port: int) -> Dict[str, Any]:
    return (_rpc(host, port, "ui_state", {}) or {}).get("state") or {}


def _wait_streaming_end(host: str, port: int, timeout_s: float = STREAM_TIMEOUT_S) -> bool:
    """轮询 ui_state.streaming 直到 False；超时返回 False。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _state(host, port).get("streaming"):
            return True
        time.sleep(1.0)
    return False


def _wait_window_ready(host: str, port: int, timeout_s: float = 15.0) -> bool:
    """等被测实例的对话窗口就绪；超时不阻塞——ui_session new 会兜底重建窗口。

    冷启动空窗期通常 30-60s；close 后对话窗口被移除且不会自动重建，
    new 的兜底路径（TabManagerWindow.add_window + FakeHostPage）负责重建。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _state(host, port).get("window_alive"):
            return True
        time.sleep(2.0)
    return False


def _sample_rows(host: str, port: int, elapsed: float, loop_iter: int, start: float) -> Sample:
    mem = _rpc(host, port, "ui_memory", {}) or {}
    private_mb = mem.get("main_private_mb")
    return Sample(
        scenario="F_window_session",
        elapsed_sec=elapsed,
        loop_iter=loop_iter,
        rss_mb=float(private_mb) if isinstance(private_mb, (int, float)) else -1.0,
        rss_ok=isinstance(private_mb, (int, float)),
        qobject_count=-1,  # 跨进程不可测
        qobject_top_types={},
        qobject_ok=False,
        biz_object_counts={"sessions": len(_state(host, port).get("sessions") or [])},
        biz_object_ok=True,
        tracemalloc_top=[],
        tracemalloc_current_mb=0.0,
        tracemalloc_peak_mb=0.0,
        tracemalloc_ok=False,
    )


def run_window_session_scenario(
    *,
    progress_cb: Callable[[int, Sample], None],
    duration_sec: float,
) -> dict:
    """窗口级会话开关压测；返回与 a/b/c 同构的 summary dict。"""
    ep = _endpoint()
    if ep is None:
        return {
            "scenario": "F_window_session",
            "error": "DRIFOX_UI_TEST_SERVER 未设置（需指向运行中的 ui_test_server host:port）",
            "iterations": 0,
            "elapsed_sec": 0.0,
            "rss_mb_first": 0.0,
            "rss_mb_last": 0.0,
            "rss_delta_mb": 0.0,
            "qobject_first": -1,
            "qobject_last": -1,
            "qobject_delta": 0,
            "rounds": 0,
            "samples": [],
        }
    host, port = ep
    interval = max(1.0, float(os.environ.get("PERF_SAMPLE_INTERVAL", "60")))
    start = time.time()
    samples: List[Sample] = []
    rounds = 0
    sends = 0
    closes = 0
    last_sample_at = 0.0
    errors: List[str] = []

    def _maybe_sample(now: float, force: bool = False) -> None:
        nonlocal last_sample_at
        if force or now - last_sample_at >= interval:
            s = _sample_rows(host, port, now - start, sends + closes, start)
            samples.append(s)
            last_sample_at = now
            progress_cb(sends + closes, s)

    try:
        while time.time() - start < duration_sec:
            # 冷启动空窗期：等被测实例完成首窗构造，避免 new 撞上无窗口
            _wait_window_ready(host, port)
            rounds += 1
            round_start = time.time()
            # 1) 新建会话
            new = _rpc(host, port, "ui_session", {"action": "new"})
            if not new.get("ok"):
                errors.append(f"new failed: {new.get('error')}")
                break
            sid = str(new.get("session_id") or "")
            # 2) 三次「发送 → 等流式结束」
            for i in range(SEND_COUNT_PER_ROUND):
                send = _rpc(host, port, "ui_send", {"text": f"[scenario-f] round={rounds} msg={i + 1} 压测占位文本。"})
                if not send.get("ok"):
                    errors.append(f"send failed: {send.get('error')}")
                    break
                sends += 1
                if not _wait_streaming_end(host, port):
                    errors.append(f"streaming timeout round={rounds} msg={i + 1}")
                _maybe_sample(time.time())
            # 3) 关闭会话（走 UI 关闭链）。目标取 ui_state 的当前会话：
            #    发送后真实链路可能重建会话，new 返回的旧 id 必然落空；
            #    用户点关闭按钮关的也是眼前 tab 的会话。
            cur_sid = str((_state(host, port) or {}).get("session_id") or "")
            close = _rpc(host, port, "ui_session", {"action": "close", "session_id": cur_sid or sid})
            if close.get("ok"):
                closes += 1
            else:
                errors.append(f"close failed: {close.get('error')}")
            _maybe_sample(time.time(), force=True)
            # 4) 轮间等待：保持「每 300s 一轮」节奏；剩余时长不足一轮则结束
            remain = ROUND_SEC - (time.time() - round_start)
            if remain > 0:
                if time.time() - start + remain >= duration_sec:
                    break
                time.sleep(remain)
    except (urllib.error.URLError, OSError) as exc:
        errors.append(f"ui_test_server unreachable: {exc}")

    _maybe_sample(time.time(), force=True)
    elapsed = time.time() - start
    rss_first = samples[0].rss_mb if samples else 0.0
    rss_last = samples[-1].rss_mb if samples else 0.0
    return {
        "scenario": "F_window_session",
        "iterations": sends + closes,
        "elapsed_sec": elapsed,
        "rss_mb_first": rss_first,
        "rss_mb_last": rss_last,
        "rss_delta_mb": rss_last - rss_first,
        "qobject_first": -1,
        "qobject_last": -1,
        "qobject_delta": 0,
        "rounds": rounds,
        "sends": sends,
        "closes": closes,
        "errors": errors[:10],
        "samples": samples,
    }
