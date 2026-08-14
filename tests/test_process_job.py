# -*- coding: utf-8 -*-
"""S3 验收：Windows Job Object 进程树杀灭 + command_safety 集成

验收标准：
1. ProcessJob.close()（kill-on-close）能杀灭含子进程树的命令
2. ProcessJob.kill() 立即终止 Job 内全部进程
3. run_safe / run_with_shell 传 job 参数后进程自动入 Job
4. command_safety 原行为零回归（不传 job 时行为不变）
"""
import subprocess
import sys
import time

import pytest

from app.tools.command_safety import run_safe, run_with_shell
from app.tools.process_job import ProcessJob


def _alive(pid: int) -> bool:
    try:
        import psutil

        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (ImportError, psutil.NoSuchProcess):
        return False


def _children(pid: int):
    try:
        import psutil

        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except (ImportError, psutil.NoSuchProcess):
        return []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object 仅 Windows 可用")
class TestProcessJob:
    def test_close_kills_process_tree(self):
        """kill-on-close：close() 后 Job 内全部进程（含子进程）被杀"""
        job = ProcessJob()
        proc = subprocess.Popen(
            ["cmd", "/c", "ping -n 8 127.0.0.1 >nul & ping -n 8 127.0.0.1 >nul"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert job.assign(proc.pid), "进程应成功加入 Job"
        time.sleep(0.8)
        # cmd 派生了两个 ping 子进程
        assert _alive(proc.pid), "命令进程应存活"
        job.close()  # 触发 kill-on-close
        time.sleep(0.5)
        assert not _alive(proc.pid), "close 后主进程应被杀"
        assert _children(proc.pid) == [], "close 后子进程树应无残留"

    def test_kill_terminates_immediately(self):
        """kill()：立即终止 Job 内全部进程"""
        job = ProcessJob()
        proc = subprocess.Popen(
            ["cmd", "/c", "ping -n 8 127.0.0.1 >nul"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert job.assign(proc.pid)
        time.sleep(0.5)
        assert _alive(proc.pid)
        ok = job.kill()
        assert ok, "TerminateJobObject 应成功"
        time.sleep(0.5)
        assert not _alive(proc.pid)
        job.close()

    def test_run_safe_with_job(self):
        """run_safe(job=...) 自动入 Job，close 杀灭"""
        job = ProcessJob()
        proc = run_safe("ping -n 6 127.0.0.1", job=job)
        time.sleep(0.5)
        assert _alive(proc.pid)
        job.close()
        time.sleep(0.5)
        assert not _alive(proc.pid), "run_safe + job 应能被 close 杀灭"

    def test_run_with_shell_with_job(self):
        """run_with_shell(job=...) 自动入 Job，含 shell 元字符命令可被杀"""
        job = ProcessJob()
        proc = run_with_shell("ping -n 6 127.0.0.1 > nul", job=job)
        time.sleep(0.5)
        assert _alive(proc.pid)
        job.close()
        time.sleep(0.5)
        assert not _alive(proc.pid), "run_with_shell + job 应能被 close 杀灭"

    def test_context_manager_kills_on_exit(self):
        """with ProcessJob() 退出时自动杀灭"""
        with ProcessJob() as job:
            proc = run_safe("ping -n 6 127.0.0.1", job=job)
            time.sleep(0.5)
            assert _alive(proc.pid)
        time.sleep(0.5)
        assert not _alive(proc.pid), "with 退出后应自动杀灭"

    def test_double_close_idempotent(self):
        """close() 幂等"""
        job = ProcessJob()
        job.close()
        job.close()  # 不应抛异常


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 特性")
class TestCommandSafetyNoRegression:
    """command_safety 零回归：不传 job 时行为与原来一致"""

    def test_run_safe_without_job(self):
        proc = run_safe("ping -n 2 127.0.0.1", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=15)
        assert proc.returncode == 0
        text = out.decode("utf-8", "ignore") if isinstance(out, bytes) else out
        assert "TTL=" in text

    def test_run_safe_rejects_bad_command(self):
        with pytest.raises(ValueError):
            run_safe("")  # 空命令应抛 ValueError（原行为）

    def test_run_with_shell_without_job(self):
        proc = run_with_shell("echo S3_NO_REGRESSION", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=15)
        assert proc.returncode == 0
        text = out.decode("utf-8", "ignore") if isinstance(out, bytes) else out
        assert "S3_NO_REGRESSION" in text
