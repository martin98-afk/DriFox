# -*- coding: utf-8 -*-
"""app_restart 单元测试：重启命令构造（参数过滤 + 环境变量剥离）。

重启是「渲染配置改完能生效」的唯一途径，两个易错点即本文件的重点用例：
- 必须剥离渲染环境变量：否则子进程继承旧值，apply_render_env 的 setdefault
  语义会让新配置被忽略（点了重启却什么都没变）
- 必须剥离一次性内部参数：否则新进程走 auto-start helper 分支直接退出
"""

import os
import sys

import pytest

from app.utils.app_restart import RENDER_ENV_KEYS, build_restart_command


def test_source_run_command_keeps_entry_script(monkeypatch):
    """源码运行：python <exe> <入口脚本> <原参数>"""
    monkeypatch.setattr(sys, "argv", ["main.py", "--foo", "1"])
    argv, cwd, _env = build_restart_command()
    assert argv[0] == sys.executable
    assert os.path.basename(argv[1]) == "main.py"
    assert argv[2:] == ["--foo", "1"]
    assert cwd == os.getcwd()


def test_one_shot_args_are_stripped(monkeypatch):
    """一次性内部参数不重放，普通参数保留"""
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "--configure-auto-start=on", "--startup-error-file=x.txt", "--keep"],
    )
    argv, _, _ = build_restart_command()
    assert "--configure-auto-start=on" not in argv
    assert "--startup-error-file=x.txt" not in argv
    assert "--keep" in argv


def test_frozen_run_uses_executable_only(monkeypatch):
    """打包运行：直接复用 exe 路径，不再追加入口脚本"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["DriFox.exe", "--x"])
    argv, _, _ = build_restart_command()
    assert argv == [sys.executable, "--x"]


def test_render_env_keys_are_stripped_from_child_env(monkeypatch):
    """子进程环境去掉全部渲染键，且不污染当前进程环境"""
    for key in RENDER_ENV_KEYS:
        monkeypatch.setenv(key, "stale")
    monkeypatch.setenv("KEEP_ME", "1")
    monkeypatch.setattr(sys, "argv", ["main.py"])

    _, _, env = build_restart_command()
    for key in RENDER_ENV_KEYS:
        assert key not in env
    assert env["KEEP_ME"] == "1"
    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "stale"
