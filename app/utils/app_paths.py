# -*- coding: utf-8 -*-
"""应用数据目录解析（纯路径模块，零重依赖）。

从 app.utils.utils 拆出的动机：utils 顶层 import Settings → qfluentwidgets →
AppKit(pyobjc)，core.store 等无 GUI 场景 import 该链路会被动拉起 AppKit
（实测 import 期 RSS +134MB）。本模块只允许 os/sys/pathlib 级依赖，
AppKit 延迟 import 保留在函数体内（仅打包环境调用时触发）。
禁止在本模块新增任何 UI/配置层 import。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_app_data_dir() -> Path:
    """获取应用数据目录（跨平台兼容）
    开发环境: 当前目录/.drifox
    PyInstaller打包: ~/.drifox（用户 home 目录，可写）
    macOS .app: ~/Library/Application Support/Drifox/.drifox
    测试/多实例隔离: 设置环境变量 DRIFOX_DATA_DIR 后一律使用该目录
    （优先级最高，置于 frozen 判断之前，UI 驱动库与测试脚手架依赖此隔离）。
    """
    env_dir = os.environ.get("DRIFOX_DATA_DIR")
    if env_dir:
        path = Path(env_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path
    # 开发环境
    if not hasattr(sys, "_MEIPASS") and not getattr(sys, "frozen", False):
        return Path(".drifox")
    # macOS .app: 使用 Application Support（用户可写）
    if sys.platform == "darwin":
        from AppKit import NSApplicationSupportDirectory, NSFileManager, NSUserDomainMask

        paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
            NSApplicationSupportDirectory, NSUserDomainMask
        )
        if paths:
            app_support_path = paths[0].fileSystemRepresentation().decode("utf-8")
            app_support = Path(app_support_path) / "Drifox"
            app_support.mkdir(parents=True, exist_ok=True)
            return app_support / ".drifox"
    # Windows/Linux 打包: 使用 ~/.drifox（用户 home，不受安装位置限制）
    return Path.home() / ".drifox"
