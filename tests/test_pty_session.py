# -*- coding: utf-8 -*-
"""S5 验收：持久 shell 会话（pywinpty ConPTY）

验收标准：
1. cwd 跨调用保留：cd 后 pwd/%CD% 一致
2. env 跨调用保留：set 后 echo 可见
3. 超时可配置（非固定 120s）
4. close 杀进程树
5. 非 Windows 平台 is_supported()=False 安全降级
"""
import sys
import time

import pytest

from app.tools.pty_session import PtyShellSession


@pytest.mark.skipif(not PtyShellSession.is_supported(), reason="需要 Windows + pywinpty")
class TestPtySession:
    def test_cwd_persists_across_calls(self):
        """cwd 跨调用保留：cd 后 %CD% 一致"""
        with PtyShellSession() as sess:
            ok, out = sess.exec("cd /d C:\\Windows")
            assert ok, f"cd 应成功: {out}"
            ok, out = sess.exec("echo %CD%")
            assert ok
            assert "C:\\Windows" in out, f"cwd 应保留: {out!r}"

    def test_env_persists_across_calls(self):
        """env 跨调用保留：set 后 echo 可见"""
        with PtyShellSession() as sess:
            ok, out = sess.exec("set DRIFOX_S5=hello_pty")
            assert ok
            ok, out = sess.exec("echo %DRIFOX_S5%")
            assert ok
            assert "hello_pty" in out, f"env 应保留: {out!r}"

    def test_custom_timeout(self):
        """超时可配置：短超时返回 timeout 而非固定 120s 等待"""
        sess = PtyShellSession(timeout=2.0)
        try:
            t0 = time.monotonic()
            ok, out = sess.exec("ping -n 6 127.0.0.1 >nul")  # 约 5 秒 > 2s
            elapsed = time.monotonic() - t0
            assert not ok, "应超时"
            assert "超时" in out
            assert elapsed < 8.0, f"应在配置超时附近返回，实际 {elapsed:.1f}s"
        finally:
            sess.close()

    def test_session_still_alive_after_timeout(self):
        """超时后会话仍可用（状态保留，不因超时销毁）"""
        sess = PtyShellSession(timeout=1.0)
        try:
            ok, _ = sess.exec("ping -n 5 127.0.0.1 >nul")  # 超时
            assert not ok
            # 会话仍可执行新命令（可能已重建，新 cmd 首次执行有初始化开销）
            ok2, out2 = sess.exec("echo ALIVE_AFTER_TIMEOUT", timeout=8.0)
            assert ok2
            assert "ALIVE_AFTER_TIMEOUT" in out2
        finally:
            sess.close()

    def test_close_kills_process(self):
        """close 终止会话进程（无残留 cmd.exe）"""
        import psutil

        sess = PtyShellSession()
        pid = sess._proc.pid
        assert psutil.Process(pid).is_running()
        sess.close()
        time.sleep(0.5)
        with pytest.raises(psutil.NoSuchProcess):
            psutil.Process(pid).is_running() and psutil.Process(pid).status()
        # 幂等
        sess.close()

    def test_close_idempotent(self):
        sess = PtyShellSession()
        sess.close()
        sess.close()  # 不抛异常

    def test_sequence_commands(self):
        """多命令串行执行状态叠加：cwd + env 组合"""
        with PtyShellSession() as sess:
            ok, _ = sess.exec("set DRIFOX_N=1")
            assert ok
            ok, _ = sess.exec("cd /d C:\\Windows && set /a DRIFOX_N+=1")
            assert ok
            ok, out = sess.exec("echo %CD% %DRIFOX_N%")
            assert ok
            assert "C:\\Windows" in out
            assert "2" in out, f"DRIFOX_N 应累加为 2: {out!r}"


class TestPtySessionPlatform:
    def test_is_supported_returns_bool(self):
        """is_supported 返回 bool（非 Windows 平台为 False）"""
        assert isinstance(PtyShellSession.is_supported(), bool)
        if sys.platform != "win32":
            assert not PtyShellSession.is_supported()

    @pytest.mark.skipif(PtyShellSession.is_supported(), reason="仅非支持平台验证")
    def test_unsupported_platform_raises(self):
        with pytest.raises(RuntimeError):
            PtyShellSession()
