# -*- coding: utf-8 -*-
"""``process.start`` 能力 provider（host 无关，ui / tool 双面共用）。

启动的进程**独立于宿主生命周期**：不入 ProcessJob、不随宿主退出被杀，stdout/
stderr 直接丢弃。适合插件拉起本地常驻服务（如 magpie web 的网关）；一次性
命令与需要输出回传的场景应走工具插件的 command 工具，那里有审批与输出管理。

安全边界：
- 只收 argv 数组，拒绝字符串（杜绝 shell 注入面），shell=False 固定；
- env 是**增量**：与宿主进程环境合并后传入，插件拿不到也无需构造全量环境；
- 进程不进任何 Job/kill 组——服务类进程必须活过宿主重启，这是能力语义的一部分；
- 审计日志记 plugin / name / pid / 参数个数，不打 env（可能含密钥）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from loguru import logger


def process_start(host: Any, ctx: Any):
    """返回 ``start(cmd, *, cwd="", env=None, name="") -> {"pid": int, "name": str}``。

    双参 (host, ctx)：host 完全不用（进程启动与窗口无关），ctx.plugin 进审计日志。
    """

    def start(cmd, cwd: str = "", env: dict | None = None, name: str = "") -> dict:
        if isinstance(cmd, str) or not isinstance(cmd, (list, tuple)) or not cmd:
            raise TypeError(
                "process.start 的 cmd 必须是非空 list[str]（如 [\"magpie\", \"web\"]），不接受字符串"
            )
        argv = [str(c) for c in cmd]

        merged_env = None
        if env:
            merged_env = dict(os.environ)
            merged_env.update({str(k): str(v) for k, v in env.items()})

        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # CREATE_NO_WINDOW：控制台子系统程序不弹黑框；
            # CREATE_NEW_PROCESS_GROUP：脱离宿主信号组，宿主收到的控制台事件不传导
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )

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
        tag = str(name or argv[0])
        logger.info(
            f"[process.start] plugin={ctx.plugin} name={tag} pid={proc.pid} "
            f"argv={argv[0]}（共 {len(argv)} 参数）"
        )
        return {"pid": proc.pid, "name": tag}

    return start
