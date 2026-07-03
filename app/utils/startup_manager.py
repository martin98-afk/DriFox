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


def build_startup_command() -> str:
    """构建写入注册表的开机自启命令"""
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        # 打包后：后台静默启动，使用 cmd /c 设置工作目录为 exe 所在目录，
        # 避免注册表 Run 键默认 CWD（system32）导致相对路径资源加载失败
        exe = Path(sys.executable).resolve()
        return f'cmd.exe /c cd /d "{exe.parent}" && start "" "{exe}"'
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

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _reg_path(), 0, winreg.KEY_SET_VALUE)
    try:
        if enabled:
            cmd = build_startup_command()
            logger.info(f"[AutoStart] 写入注册表: {_reg_path()}\\{_reg_value_name()} = {cmd}")
            winreg.SetValueEx(key, _reg_value_name(), 0, winreg.REG_SZ, cmd)
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
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _reg_path(), 0, winreg.KEY_READ)
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
    启动时同步：双向合并策略，确保注册表与配置一致。

    - 配置为 True 但注册表缺失 → 补写注册表（修复注册表被误删的情况）
    - 配置为 False 但注册表残留 → 清理注册表（配置准确优先）
    - 两者一致 → 跳过，减少不必要的注册表写入

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

        config_enabled = bool(cfg.auto_start.value)
        reg_enabled = is_auto_start_enabled()
        logger.info(f"[AutoStart] 配置={config_enabled}, 注册表={reg_enabled}")

        if config_enabled and not reg_enabled:
            logger.info("[AutoStart] 配置已开启但注册表缺失，补写注册表")
            set_auto_start(True)
        elif not config_enabled and reg_enabled:
            logger.info("[AutoStart] 配置已关闭但注册表残留，清理注册表")
            set_auto_start(False)
        else:
            logger.info("[AutoStart] 状态一致，跳过同步")
    except Exception:
        logger.exception("[AutoStart] 同步开机自启状态失败")
