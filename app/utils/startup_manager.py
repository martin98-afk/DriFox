# -*- coding: utf-8 -*-
"""
开机自启管理 — 使用 HKEY_CURRENT_USER，无需管理员权限

基于 PeekAgent 的 startup_manager 模式简化而来：
- 使用 HKCU Run 键，免 UAC 提权
- 开发模式: pythonw main.py
- 打包模式: exe 自身
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger


def _is_windows() -> bool:
    return os.name == "nt"


def _get_app_exe_path() -> str:
    """
    获取应用可执行文件路径。
    
    打包后 (PyInstaller): sys.executable 就是 exe 路径
    开发环境: 返回 pythonw/python + main.py
    """
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        return str(Path(sys.executable).resolve())
    
    # 开发环境：尝试 pythonw，回退到 python
    python_path = Path(sys.executable).resolve()
    pythonw_path = python_path.with_name("pythonw.exe")
    launcher = pythonw_path if pythonw_path.exists() else python_path
    main_path = Path(__file__).resolve().parent.parent.parent / "main.py"
    return subprocess.list2cmdline([str(launcher), str(main_path)])


def build_startup_command() -> str:
    """构建写入注册表的开机自启命令"""
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        # 打包后：后台静默启动，不弹窗口
        return subprocess.list2cmdline([sys.executable])
    else:
        # 开发环境：pythonw main.py（无控制台窗口）
        python_path = Path(sys.executable).resolve()
        pythonw_path = python_path.with_name("pythonw.exe")
        launcher = pythonw_path if pythonw_path.exists() else python_path
        main_path = Path(__file__).resolve().parent.parent.parent / "main.py"
        return subprocess.list2cmdline([str(launcher), str(main_path)])


def _reg_path() -> str:
    return r"Software\Microsoft\Windows\CurrentVersion\Run"


def _reg_value_name() -> str:
    return "Drifox"


def set_auto_start(enabled: bool):
    """
    设置开机自启状态。

    写入 HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
    仅对当前用户生效，无需管理员权限。

    Args:
        enabled: True 启用自启，False 禁用

    Raises:
        RuntimeError: 非 Windows 平台
        Exception: 注册表写入失败（由调用方决定是否处理）
    """
    if not _is_windows():
        raise RuntimeError("当前平台不支持开机自启配置。")

    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _reg_path(), 0, winreg.KEY_SET_VALUE
    )
    try:
        if enabled:
            cmd = build_startup_command()
            logger.info(f"[AutoStart] 写入注册表: {_reg_path()}\\{_reg_value_name()} = {cmd}")
            winreg.SetValueEx(
                key, _reg_value_name(), 0, winreg.REG_SZ, cmd
            )
        else:
            logger.info(f"[AutoStart] 删除注册表项: {_reg_value_name()}")
            try:
                winreg.DeleteValue(key, _reg_value_name())
            except FileNotFoundError:
                logger.info("[AutoStart] 注册表项不存在，无需删除")
    finally:
        winreg.CloseKey(key)


def is_auto_start_enabled() -> bool:
    """检查当前是否已启用开机自启"""
    if not _is_windows():
        return False

    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _reg_path(), 0, winreg.KEY_READ
        )
        try:
            winreg.QueryValueEx(key, _reg_value_name())
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def sync_auto_start_from_config():
    """
    启动时同步：根据配置文件中的 auto_start 设置，确保注册表状态一致。
    
    用于 app 启动入口，防止注册表项被意外删除后自启失效。
    
    注意：只有在配置成功从文件加载后才执行同步（_config_loaded=True），
    避免因配置加载失败时使用默认值（False）而误删注册表项。
    """
    if not _is_windows():
        return

    try:
        from app.utils.config import Settings
        cfg = Settings.get_instance()
        if not Settings._config_loaded:
            logger.warning("[AutoStart] 配置未成功加载，跳过注册表同步，保留现有状态")
            return
        enabled = bool(cfg.auto_start.value)
        logger.info(f"[AutoStart] 配置值: {enabled}，正在同步注册表...")
        set_auto_start(enabled)
        logger.info(f"[AutoStart] 同步完成，注册表状态: {is_auto_start_enabled()}")
    except Exception:
        logger.exception("[AutoStart] 同步开机自启状态失败")
