# -*- coding: utf-8 -*-
"""MCP 测试服务端到端验收（M2，本任务唯一允许的 UI 测试，一次）。

链路：环境变量启动 dev 实例（隔离数据目录 + DRIFOX_UI_TEST_SERVER）→
tools/list → ui_inspect tree → find 领 token → click 无 token 拒 →
带 token 成功 → ui_screenshot → POST /shutdown 干净退出。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ui]

_PORT = 8791
_BASE = f"http://127.0.0.1:{_PORT}"


def _rpc(method: str, params: dict | None = None, msg_id: int = 1, timeout: float = 15.0) -> dict:
    payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        _BASE + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_tool(name: str, arguments: dict, msg_id: int = 1) -> dict:
    out = _rpc("tools/call", {"name": name, "arguments": arguments}, msg_id=msg_id)
    assert "error" not in out, f"{name} RPC error: {out}"
    content = out["result"]["content"][0]["text"]
    return json.loads(content)


def _wait_port(timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", _PORT), timeout=1.0):
                return True
        except OSError:
            time.sleep(1.0)
    return False


@pytest.fixture(scope="module")
def dev_instance(tmp_path_factory):
    """启动隔离数据目录的 dev 实例（UI 测试服务开启），yield 后收尾。"""
    data_dir = tmp_path_factory.mktemp("drifox_mcp_e2e")
    env = dict(os.environ)
    env["DRIFOX_DATA_DIR"] = str(data_dir)
    env["DRIFOX_UI_TEST_SERVER"] = f"127.0.0.1:{_PORT}"
    env.pop("QT_QPA_PLATFORM", None)  # A 档：真实平台
    proc = subprocess.Popen(
        [sys.executable, str(Path(r"D:/work/DriFox/main.py"))],
        cwd=r"D:/work/DriFox",
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_port(), "dev 实例 90s 内未监听测试服务端口"
    except AssertionError:
        raise
    yield proc
    try:
        urllib.request.urlopen(_BASE + "/shutdown", data=b"", timeout=10)
    except Exception:
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_mcp_e2e_full_chain(dev_instance, tmp_path):
    # 1) initialize
    out = _rpc("initialize", {}, msg_id=1)
    assert out["result"]["serverInfo"]["name"] == "drifox-ui-test-server"

    # 2) tools/list：7 工具
    out = _rpc("tools/list", {}, msg_id=2)
    names = [t["name"] for t in out["result"]["tools"]]
    assert names == [
        "ui_inspect",
        "ui_state",
        "ui_click",
        "ui_type",
        "ui_scroll",
        "ui_wait",
        "ui_screenshot",
    ]

    # 3) ui_inspect mode=tree：返回树 JSON
    tree_data = _call_tool("ui_inspect", {"mode": "tree", "depth": 2}, msg_id=3)
    assert tree_data.get("cls"), "tree 快照为空"

    # 4) ui_inspect mode=find 命中可点按钮 → 领 token
    find_res = _call_tool(
        "ui_inspect",
        {"mode": "find", "selector": {"cls": "QPushButton"}},
        msg_id=4,
    )
    assert find_res.get("found") is True, f"未命中 QPushButton: {find_res}"
    token = find_res.get("confirm_token")
    target_name = find_res.get("objectName")
    assert token, f"未下发 confirm_token: {find_res}"

    # 5) ui_click 无 token → 拒
    denied = _call_tool("ui_click", {"objectName": target_name}, msg_id=5)
    assert denied.get("clicked") is False and denied.get("blocked_by") == "missing_token"

    # 6) ui_click 带 token → 成功
    clicked = _call_tool("ui_click", {"objectName": target_name, "confirm_token": token}, msg_id=6)
    assert clicked.get("clicked") is True, f"带 token 点击失败: {clicked}"

    # 7) ui_screenshot：PNG 非空
    shot = _call_tool("ui_screenshot", {"max_width": 960}, msg_id=7)
    png = Path(shot["path"])
    assert png.exists() and png.stat().st_size > 100
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # 8) shutdown（fixture 收尾再做一次也无妨，幂等）
    urllib.request.urlopen(_BASE + "/shutdown", data=b"", timeout=10)
