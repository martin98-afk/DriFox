# -*- coding: utf-8 -*-
"""``process.start`` 能力 provider（host 无关，ui / tool 双面共用）。

启动的进程**绑定宿主存续期**（2026-10 定稿，替代早期"独立于宿主生命周期"
语义）：每个进程入专属 kill-on-close Job，宿主退出/崩溃即被内核树杀；
插件禁用/卸载时由 ProcessRegistry.kill_plugin 主动回收。台账、限额与
env 白名单见 app/plugins/capabilities/process_registry.py。

安全边界：
- 只收 argv 数组，拒绝字符串（杜绝 shell 注入面），shell=False 固定；
- env 只继承白名单子集 + 插件显式增量，宿主密钥不泄漏给子进程；
- 首启确认：每插件会话内首次启动弹窗（插件名 + 命令），拒绝后本会话内
  该插件一律拒绝（防骚扰式重试）；无 GUI（headless/测试）跳过确认；
- 限额：per-plugin 8 / 全局 32，超限拒绝；
- 审计日志记 plugin / name / pid / 参数个数，不打 env（可能含密钥）。

一次性命令与需要输出回传的场景应走工具插件的 command 工具，那里有审批
与输出管理。
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

from loguru import logger

from app.plugins.capabilities.process_registry import (
    ProcessRecord,
    ProcessRegistry,
    build_process_env,
)

# 首启确认弹窗等待上限（秒）：用户在场很快点击；超时按拒绝处理
_CONFIRM_TIMEOUT_S = 120.0


def process_start(host: Any, ctx: Any):
    """返回 ``start(cmd, *, cwd="", env=None, name="") -> {"pid": int, "name": str}``。

    双参 (host, ctx)：host 完全不用（弹窗 parent 走 activeWindow），ctx.plugin
    进审计日志与台账归属。
    """

    def start(cmd, cwd: str = "", env: dict | None = None, name: str = "") -> dict:
        if isinstance(cmd, str) or not isinstance(cmd, (list, tuple)) or not cmd:
            raise TypeError('process.start 的 cmd 必须是非空 list[str]（如 ["magpie", "web"]），不接受字符串')
        argv = [str(c) for c in cmd]

        registry = ProcessRegistry.get_instance()
        registry.check_limit(ctx.plugin)
        _confirm_once(registry, ctx.plugin, argv, cwd)

        merged_env = build_process_env(env)

        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # CREATE_NO_WINDOW：控制台子系统程序不弹黑框；
            # CREATE_NEW_PROCESS_GROUP：脱离宿主信号组，宿主收到的控制台事件不传导
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
            argv,
            shell=False,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )

        job = None
        from app.tools.process_job import ProcessJob

        if ProcessJob.is_supported():
            job = ProcessJob()
            if not job.assign(proc.pid):
                # 已知窗口：Popen 返回到 assign 之间进程若秒退则失败，无害；
                # 降级为仅 Popen 引用杀（不覆盖孙进程）
                logger.warning(f"[process.start] Job assign 失败 pid={proc.pid}，降级为直接杀")
                job.close()
                job = None

        tag = str(name or argv[0])
        registry.register(
            ProcessRecord(
                plugin=str(ctx.plugin),
                name=tag,
                pid=proc.pid,
                argv0=argv[0],
                started_at=time.time(),
                proc=proc,
                job=job,
            )
        )
        logger.info(
            f"[process.start] plugin={ctx.plugin} name={tag} pid={proc.pid} argv={argv[0]}（共 {len(argv)} 参数）"
        )
        return {"pid": proc.pid, "name": tag}

    return start


def _confirm_once(registry: ProcessRegistry, plugin: str, argv: list[str], cwd: str) -> None:
    """会话内每插件首启确认。拒绝/超时 → PermissionError；拒绝入会话记忆防骚扰。"""
    state = registry.approval_state(plugin)
    if state == "approved":
        return
    if state == "denied":
        raise PermissionError(f"插件 {plugin} 的后台进程启动在本会话内已被用户拒绝")
    if _confirm_dialog(plugin, argv, cwd):
        registry.approve(plugin)
    else:
        registry.deny(plugin)
        raise PermissionError(f"插件 {plugin} 请求启动后台进程被拒绝或确认超时")


def _confirm_dialog(plugin: str, argv: list[str], cwd: str) -> bool:
    """弹首启确认框（主线程执行）。无 GUI 放行（headless/测试），异常按拒绝。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        logger.warning("[process.start] 无 GUI 环境，跳过首启确认（headless/测试语义）")
        return True
    try:
        from app.plugins.capabilities.providers.ui_bridge import _dispatch, preheat_main_thread_dispatcher
        from app.widgets.common_dialogs import ConfirmDialog

        # ★ 预热兜底（幂等）：启动期 ui_core 注册已在主线程预热 _Dispatcher；
        # 若单例仍未建（异常路径），此处以调用线程兜底建——至少保证单例存在。
        preheat_main_thread_dispatcher()

        def _ask() -> bool:
            box = {"ok": False}
            cwd_line = f"\n工作目录：{cwd}" if cwd else ""
            dlg = ConfirmDialog(
                title="插件请求启动后台进程",
                content=(
                    f"插件「{plugin}」请求启动后台进程：\n"
                    f"{argv[0]}（共 {len(argv)} 个参数）{cwd_line}\n\n"
                    "该进程随主程序退出而终止；拒绝后本会话内该插件不能再启动进程。"
                ),
                confirm_text="允许",
                cancel_text="拒绝",
                parent=QApplication.activeWindow(),
            )
            dlg.confirmed.connect(lambda: box.__setitem__("ok", True))
            dlg.exec()
            return bool(box["ok"])

        return bool(_dispatch(_ask, timeout=_CONFIRM_TIMEOUT_S))
    except Exception as e:
        logger.warning(f"[process.start] 首启确认弹窗失败（{e}），按拒绝处理")
        return False
