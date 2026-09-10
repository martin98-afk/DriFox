# -*- coding: utf-8 -*-
"""应用自重启：渲染类配置改完能立刻生效的唯一途径。

为什么需要
----------
QtWebEngine 只在进程首次初始化时读取 QT_OPENGL / QT_ANGLE_PLATFORM /
QTWEBENGINE_CHROMIUM_FLAGS（见 app/utils/render_env.py），运行中改配置无效 ——
所以「渲染与性能」页所有配置项都标注重启生效，并由本模块提供「拉起新进程 +
退出当前进程」的一键重启。

三个必须注意的细节
------------------
1. **剥离渲染环境变量**：子进程默认继承当前进程的 QTWEBENGINE_CHROMIUM_FLAGS
   等，而 apply_render_env() 走 setdefault（外部环境变量优先的调试逃生门）——
   不剥离的话新进程沿用旧值，用户点了重启却什么都没变，这类 bug 极难自查。
2. **剥离一次性内部参数**：--configure-auto-start=* / --startup-error-file=*
   是开机自启提权 helper 的开关（main.py 在 Qt 加载前处理完就 sys.exit），
   原样重放会让新进程直接退出，表现为「重启后程序没了」。
3. **单实例锁**：main.py 中的 SingleInstanceGuard 段目前被注释停用，无需处理
   锁交接；若日后恢复，需先释放锁再拉起新进程（否则新进程会通知旧窗口后退出）。
"""

from __future__ import annotations

import os
import subprocess
import sys

from loguru import logger

__all__ = ["RENDER_ENV_KEYS", "build_restart_command", "restart_application"]

# 重启前必须从子进程环境剥离的键（见模块文档细节 1 / QSG_RHI* 为场景图防御项）
RENDER_ENV_KEYS = (
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "QT_OPENGL",
    "QT_ANGLE_PLATFORM",
    "QSG_RHI",
    "QSG_RHI_BACKEND",
)

# 一次性内部参数前缀（见模块文档细节 2）
_ONE_SHOT_ARG_PREFIXES = (
    "--configure-auto-start=",
    "--startup-error-file=",
)

# 优雅退出等待上限（ms）：超时未退出则 os._exit 兜底，防清理流程卡住退出
_QUIT_FALLBACK_MS = 5000


def entry_script() -> str:
    """源码运行时的入口脚本绝对路径（frozen 场景不需要）。

    sys.argv[0] 在 `python main.py` 下是脚本路径；交互式 / 打包器注入场景可能为
    空串，回退到仓库根的 main.py。
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return os.path.abspath("main.py")
    return os.path.abspath(argv0)


def build_restart_command() -> tuple[list[str], str, dict[str, str]]:
    """构造重启命令：``(argv, cwd, env)``，纯函数便于单测。

    - frozen（PyInstaller）：``[<exe>, *过滤后的原参数]``
    - 源码运行：``[<python>, <入口脚本>, *过滤后的原参数]``
    - env：当前环境去掉 RENDER_ENV_KEYS，保证新进程重新按 app.config 换算
    """
    args = [a for a in sys.argv[1:] if not a.startswith(_ONE_SHOT_ARG_PREFIXES)]
    if getattr(sys, "frozen", False):
        argv = [sys.executable, *args]
    else:
        argv = [sys.executable, entry_script(), *args]
    env = {k: v for k, v in os.environ.items() if k not in RENDER_ENV_KEYS}
    return argv, os.getcwd(), env


def restart_application() -> bool:
    """拉起新进程并退出当前实例。

    Returns:
        True：新进程已成功拉起（当前实例随后退出）；False：拉起失败，调用方应恢复 UI。
    """
    argv, cwd, env = build_restart_command()
    # 必须先放锁：开启「单实例限制」时，新进程 try_lock 若撞上旧进程的
    # QSharedMemory，会走「通知已有实例 + 退出」分支 → 表现为「点了重启程序没了」
    _release_single_instance_lock()
    try:
        kwargs: dict = {"cwd": cwd, "env": env, "shell": False}
        if os.name == "nt":
            # 与 update_checker 一致：脱离父进程控制台，避免父进程退出时子进程被连带终止
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)
    except Exception as e:
        logger.error(f"[restart] 拉起新进程失败: {e}")
        return False

    logger.info(f"[restart] 已拉起新进程: {argv}")
    _quit_current()
    return True


def _release_single_instance_lock() -> None:
    """释放单实例锁（失败不影响重启尝试，最坏只是新进程多试一次）。"""
    try:
        from app.core.single_instance import release_current_lock

        release_current_lock()
    except Exception as e:
        logger.debug(f"[restart] 释放单实例锁跳过: {e}")


def _quit_current() -> None:
    """优雅退出当前实例；兜底定时器防清理流程卡死（正常退出时该定时器不触发）。"""
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication

    QTimer.singleShot(_QUIT_FALLBACK_MS, lambda: os._exit(0))
    app = QApplication.instance()
    if app is None:
        os._exit(0)
    app.quit()
