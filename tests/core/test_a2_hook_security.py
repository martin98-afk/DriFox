# -*- coding: utf-8 -*-
"""A2：hooks 三模式执行收敛（python 白名单 / command 多行拒收 / http 私网拦截）。

八用例：os:system 拒 / 相对路径放行 / registered_functions 放行 / 多行 command 拒 /
单行过 / http 拒（非 https）/ https 私网拒 / https 公网过。
恶意 fixture 全部 tmp_path；subprocess 用 monkeypatch 拦截（多行 command 不允许真实执行）。
"""
import pytest
from loguru import logger

from app.core.hook_manager import (
    Hook,
    HookManager,
    HookWorker,
    _is_safe_python_module,
    _validate_http_hook_url,
)


@pytest.fixture()
def log_capture():
    """loguru WARNING+ 捕获为文本列表。"""
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


def _make_worker(hook: Hook, event_name: str = "test-event") -> HookWorker:
    """跳过 Qt signals 构造（_execute_python 只用 hook/event_name/context）。"""
    worker = HookWorker.__new__(HookWorker)
    worker.hook = hook
    worker.event_name = event_name
    worker.context = {}
    return worker


def _make_sync_manager(tmp_path, registered=None):
    """同步执行路径最小实例（跳过 QThreadPool/共享态初始化）。"""
    hm = HookManager.__new__(HookManager)
    hm._registered_functions = registered or {}
    hm._on_status_callback = None
    hm._on_decision_callback = None
    hm._on_finished_callback = None
    hm._resolve_command_cwd = lambda hook, context: str(tmp_path)
    return hm


def test_python_standard_path_rejects_os_system(tmp_path, log_capture):
    """os:system（标准路径）不在白名单 → 拒执行 + 审计日志。"""
    worker = _make_worker(Hook(type="python", function="os:system"))
    out, ok = worker._execute_python()
    assert ok is False
    assert "Rejected" in out and "os" in out
    assert any("[HookPythonAudit]" in r for r in log_capture)
    # 内置白名单外的任意模块同理；子模块前缀放行
    assert not _is_safe_python_module("subprocess")
    assert _is_safe_python_module("app.utils.utils")


def test_python_relative_path_bypasses_whitelist(tmp_path):
    """相对路径（hooks.json 同目录）保留旁路，不受标准路径白名单限制。"""
    (tmp_path / "relmod.py").write_text(
        "def hook_func(**kw):\n    return 'relative-ok'\n", encoding="utf-8"
    )
    (tmp_path / "hooks.json").write_text("{}", encoding="utf-8")
    worker = _make_worker(
        Hook(type="python", function=".relmod:hook_func", config_file=str(tmp_path / "hooks.json"))
    )
    out, ok = worker._execute_python()
    assert ok is True and out == "relative-ok"


def test_python_registered_functions_bypass(tmp_path, log_capture):
    """registered_functions 注册表查找先于白名单：表内函数放行（同步路径）。"""
    hm = _make_sync_manager(tmp_path, registered={"goodmod:run": lambda **kw: "ok"})
    # 表内函数的模块名不在白名单也不受影响（表查找优先）
    assert not _is_safe_python_module("goodmod")
    result = hm._execute_hook(
        Hook(type="python", function="goodmod:run", add_output_to_context=False),
        {"event_name": "evt"},
        trigger_async=False,
    )
    assert result.success is True and result.output == "ok"
    # 同一执行面：表未命中走标准路径 → os:system 被白名单拒
    hm2 = _make_sync_manager(tmp_path, registered={})
    result2 = hm2._execute_hook(
        Hook(type="python", function="os:system", add_output_to_context=False),
        {"event_name": "evt"},
        trigger_async=False,
    )
    assert result2.success is False and "Rejected" in result2.output


def test_command_multiline_rejected(tmp_path, log_capture, monkeypatch):
    """多行 command 拒收：不拼接、不执行、明确报错。"""

    def _boom(*a, **k):
        raise AssertionError("multi-line command must never reach subprocess")

    monkeypatch.setattr("subprocess.run", _boom)
    out, ok, rc = HookWorker._run_command_sync("echo one\necho two")
    assert ok is False
    assert "Rejected" in out and "multi-line" in out
    assert any("多行 command 已拒收" in r for r in log_capture)


def test_command_single_line_passes(tmp_path, log_capture):
    """单行 command 正常执行（echo 内建命令），并产生执行审计日志。"""
    hook = Hook(type="command", command="echo hello", skill_root=str(tmp_path / "some-plugin"))
    out, ok, rc = HookWorker._run_command_sync(hook.command)
    assert ok is True and out.strip() == "hello"
    # 执行审计（插件名+事件+命令摘要）在调用侧产生
    from app.core.hook_manager import _audit_command

    _audit_command(hook, "test-event", hook.command)
    audits = [r for r in log_capture if "[HookCommandAudit]" in r]
    assert any("some-plugin" in r and "test-event" in r and "echo hello" in r for r in audits)


@pytest.mark.parametrize("bad_url", ["http://example.com/hook", "ftp://example.com/hook", ""])
def test_http_rejects_non_https(bad_url, log_capture):
    """http（非 https）scheme 一律拒。"""
    ok, reason = _validate_http_hook_url(bad_url)
    assert ok is False
    assert "https" in reason


@pytest.mark.parametrize(
    "private_url",
    [
        "https://127.0.0.1/hook",
        "https://10.1.2.3/hook",
        "https://192.168.0.1/hook",
        "https://172.16.5.4/hook",
        "https://169.254.9.9/hook",
        "https://[::1]/hook",
        "https://localhost/hook",
        "https://svc.localhost/hook",
    ],
)
def test_http_rejects_private_network(private_url, log_capture):
    """https 私网段（127/8、10/8、172.16/12、192.168/16、169.254/16、::1、localhost）默认拒。"""
    ok, reason = _validate_http_hook_url(private_url)
    assert ok is False, reason
    assert "私网" in reason
    # Settings 开关可放行私网（临时改内存值，finally 还原）
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = cfg.hook_allow_private_network.value
    cfg.hook_allow_private_network.value = True
    try:
        ok2, _ = _validate_http_hook_url(private_url)
        assert ok2 is True
    finally:
        cfg.hook_allow_private_network.value = saved


@pytest.mark.parametrize(
    "public_url",
    ["https://example.com/hook", "https://8.8.8.8/hook", "https://api.github.com/hook"],
)
def test_http_allows_public_https(public_url, log_capture):
    """https 公网地址放行（域名不做 DNS 解析，公网 IP 字面量放行）。"""
    ok, reason = _validate_http_hook_url(public_url)
    assert ok is True, reason
