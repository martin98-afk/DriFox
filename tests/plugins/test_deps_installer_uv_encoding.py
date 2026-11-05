# -*- coding: utf-8 -*-
"""DepsInstaller._install_with_uv 子进程输出解码回归测试。

背景缺陷（2026-11-05 修）：
- subprocess.run(..., text=True) 未指定 encoding，Windows 上按 cp936 解码。
  uv 的报错含本地化中文（UTF-8 字节），解码在 _readerthread 里抛
  UnicodeDecodeError → stderr 变成 None → 日志只剩「uv 退出码 2:」，
  真实错误信息全部丢失，同时线程异常上报到全局 excepthook 变 ERROR。

覆盖口径：让替身 uv 输出「非本地编码」的 UTF-8 中文并退出码 2，断言
  1. 不抛异常（thread 级 UnicodeDecodeError 会逃出 subprocess.run）
  2. 错误信息完整落进 InstallResult.log（stderr 未被吞成 None）
"""
import subprocess
import sys

import pytest

from tests.plugins.test_deps_installer_wheel_tags import deps_installer  # noqa: F401

_UV_STDERR_TEXT = "由于目标计算机积极拒绝，无法连接。 (os error 10061)"


class _FakeSubprocess:
    """只替换模块内 subprocess 的 run，保留其余属性（TimeoutExpired/CREATE_NO_WINDOW）。"""

    TimeoutExpired = subprocess.TimeoutExpired
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    @staticmethod
    def run(cmd, **kwargs):
        # 替身 uv：忽略参数，输出 UTF-8 中文错误并返回退出码 2。
        # 关键：输出字节在 cp936 下非法，未指定 encoding 时必然解码失败。
        script = (
            "import sys;"
            f"sys.stderr.buffer.write({_UV_STDERR_TEXT!r}.encode('utf-8'));"
            "sys.stderr.flush();"
            "sys.exit(2)"
        )
        return subprocess.run([sys.executable, "-c", script], **kwargs)


def test_uv_non_local_encoding_output_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(deps_installer.shutil, "which", lambda _name: "uv")
    monkeypatch.setattr(deps_installer, "subprocess", _FakeSubprocess)

    res = deps_installer.DepsInstaller()._install_with_uv(["some-pkg"], tmp_path)

    assert res.success is False
    joined = "\n".join(res.log)
    assert "uv 退出码 2" in joined
    assert _UV_STDERR_TEXT in joined, f"stderr 被吞掉，log={res.log!r}"
