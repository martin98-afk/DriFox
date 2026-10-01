# -*- coding: utf-8 -*-
"""pty 会话配额接线（EU-G8）

`PtyShellSession._spawn_session` 原先裸构造 `ProcessJob()`（三限额全 0 = 不限），
是同体系唯一遗漏点 —— `bg_manager.py:268` 与 `terminal_tools.py:416` 都已用
`create_quota_job()` 读 `SandboxConfig["job_limits"]`。

说明：`pty_session` 全仓无生产调用方（仅自身 + 本目录测试），本项价值是
**消除同体系不一致 + 未来启用即就位**。
"""

import inspect
import sys
from pathlib import Path

import pytest


def _read_pty_source() -> str:
    root = Path(__file__).resolve().parent.parent.parent
    return (root / "app" / "tools" / "pty_session.py").read_text(encoding="utf-8")


def test_pty_uses_create_quota_job():
    """源码级：必须走 create_quota_job（与 bg_manager/terminal_tools 同口径）"""
    src = _read_pty_source()
    assert "create_quota_job" in src, "pty 会话未接配额工厂"


def test_pty_no_longer_bare_constructs_process_job():
    """源码级：不得再裸构造 ProcessJob（那是三限额全 0 = 不限的旧写法）"""
    src = _read_pty_source()
    # 允许注释里提到 ProcessJob，但不得有实际构造调用
    code_lines = [
        line.strip() for line in src.splitlines() if "ProcessJob" in line and not line.strip().startswith("#")
    ]
    constructs = [line for line in code_lines if "ProcessJob(" in line]
    assert not constructs, f"仍存在裸构造: {constructs}"


def test_pty_import_is_lazy():
    """延迟 import：避免 pty_session 加载时拖入 bg_manager 的重依赖"""
    src = _read_pty_source()
    # 顶层不得有 from app.tools.bg_manager import ...
    top_lines = [line for line in src.splitlines() if line.startswith("from app.tools.bg_manager")]
    assert not top_lines, f"create_quota_job 必须函数内延迟 import，实际顶层: {top_lines}"
    assert "from app.tools.bg_manager import create_quota_job" in src


def test_pty_job_none_is_tolerated():
    """create_quota_job 返回 None 时（非 Windows / 不支持）必须兼容"""
    src = _read_pty_source()
    assert "if self._job is not None:" in src, "None 分支处理缺失"


@pytest.mark.skipif(sys.platform != "win32", reason="ProcessJob 仅 Windows 支持")
def test_create_quota_job_reads_job_limits(monkeypatch):
    """端到端：create_quota_job 从 SandboxConfig 读三限额"""
    from app.tools import bg_manager
    from app.tools.process_job import ProcessJob
    from app.tools.sandbox import SandboxConfig

    if not ProcessJob.is_supported():
        pytest.skip("本机 ProcessJob 不可用")

    SandboxConfig.reset_instance()
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("job_limits", {"memory_mb": 64, "active_process": 8, "cpu_time_ms": 500})
    monkeypatch.setattr(SandboxConfig, "_instance", cfg)
    try:
        job = bg_manager.create_quota_job()
        assert job is not None, "Windows 上应返回带配额的 Job"
        # 关掉避免句柄泄漏
        try:
            job.close()
        except Exception:
            pass
    finally:
        SandboxConfig.reset_instance()


def test_create_quota_job_falls_back_on_bad_config(monkeypatch):
    """配额读取失败 → 不抛异常（回退无限额 Job 或 None，均属可接受降级）"""
    from app.tools import bg_manager
    from app.tools.process_job import ProcessJob

    class _Boom:
        def get(self, key):
            raise RuntimeError("boom")

    import app.tools.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod.SandboxConfig, "get_instance", staticmethod(lambda: _Boom()))
    result = bg_manager.create_quota_job()
    if not ProcessJob.is_supported():
        assert result is None
    else:
        assert result is not None, "支持时仍应返回 Job（仅限额退化为 0，不阻断执行）"
        try:
            result.close()
        except Exception:
            pass


def test_spawn_session_signature_unchanged():
    """回归：_spawn_session 签名未被改动（内部实现可换）"""
    from app.tools.pty_session import PtyShellSession

    sig = inspect.signature(PtyShellSession._spawn_session)
    assert list(sig.parameters) == ["self"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
