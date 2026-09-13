# -*- coding: utf-8 -*-
"""
开机自启管理 — HKLM Run 键 + UAC 提权 helper（PeekAgent 模式）

- 写 HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
  （带 KEY_WOW64_64KEY，避免 32/64 位注册表重定向），所有用户生效
- HKLM 写入需要管理员权限：主进程通过 runas 把自身拉起为提权 helper 子进程，
  helper 写完注册表立即退出，成败通过 error file 回传
- 开机自启命令：
  - 打包模式: cmd /c cd /d "<exe目录>" && start "" "<exe>"（设定 CWD，
    避免 Run 键默认 CWD=system32 导致相对路径资源加载失败）
  - 开发模式: pythonw main.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from loguru import logger

_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_VALUE_NAME = "Drifox"
_HELPER_FLAG_ON = "on"
_HELPER_FLAG_OFF = "off"


class AutoStartCancelled(RuntimeError):
    """用户在 UAC 弹窗取消提权请求"""

    def __init__(self, message: str = "已取消管理员权限请求。"):
        super().__init__(message)


def _is_windows() -> bool:
    return os.name == "nt"


def _main_py_path() -> Path:
    """开发模式 main.py 绝对路径（app/utils/startup_manager.py → 项目根三层上）"""
    return Path(__file__).resolve().parent.parent.parent / "main.py"


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
        return subprocess.list2cmdline([str(launcher), str(_main_py_path())])


def _reg_access(base: int) -> int:
    """注册表访问权限：附加 KEY_WOW64_64KEY 绕过 WoW64 重定向"""
    import winreg

    access = base
    if hasattr(winreg, "KEY_WOW64_64KEY"):
        access |= winreg.KEY_WOW64_64KEY
    return access


def configure_auto_start(enabled: bool):
    """
    直写 HKLM 注册表。需要管理员权限，正常只由提权 helper 进程调用。

    Raises:
        RuntimeError: 非 Windows 平台
        Exception: 注册表打开/写入失败（PermissionError = 权限不足）
    """
    if not _is_windows():
        raise RuntimeError("当前平台不支持开机自启配置。")

    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REG_PATH, 0, _reg_access(winreg.KEY_SET_VALUE)) as key:
        if enabled:
            cmd = build_startup_command()
            logger.info(f"[AutoStart] 写入 HKLM 注册表: {_REG_PATH}\\{_REG_VALUE_NAME} = {cmd}")
            winreg.SetValueEx(key, _REG_VALUE_NAME, 0, winreg.REG_SZ, cmd)
        else:
            logger.info(f"[AutoStart] 删除 HKLM 注册表项: {_REG_VALUE_NAME}")
            try:
                winreg.DeleteValue(key, _REG_VALUE_NAME)
            except FileNotFoundError:
                pass


def get_registered_command() -> str | None:
    """读取 HKLM 注册表当前记录的启动命令；不存在返回 None（读不需要提权）"""
    if not _is_windows():
        return None

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REG_PATH, 0, _reg_access(winreg.KEY_READ)) as key:
            value, _ = winreg.QueryValueEx(key, _REG_VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("[AutoStart] 读取 HKLM 注册表失败")
        return None


# ── 提权 helper 通道 ─────────────────────────────────────────


def _helper_invocation(enabled: bool, error_path: str) -> tuple[str, str]:
    """构造提权 helper 的可执行文件与命令行参数"""
    flag = _HELPER_FLAG_ON if enabled else _HELPER_FLAG_OFF
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        return sys.executable, subprocess.list2cmdline(
            [f"--configure-auto-start={flag}", f"--startup-error-file={error_path}"]
        )
    python_path = Path(sys.executable).resolve()
    pythonw_path = python_path.with_name("pythonw.exe")
    launcher = pythonw_path if pythonw_path.exists() else python_path
    params = [
        str(_main_py_path()),
        f"--configure-auto-start={flag}",
        f"--startup-error-file={error_path}",
    ]
    return str(launcher), subprocess.list2cmdline(params)


def shell_execute_and_wait(verb: str, file: str, parameters: str = "") -> int:
    """ShellExecuteExW 执行并等待进程退出（用于 runas 提权）"""
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_SHOWNORMAL = 1
    INFINITE = 0xFFFFFFFF

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    execute_info = SHELLEXECUTEINFOW()
    execute_info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    execute_info.fMask = SEE_MASK_NOCLOSEPROCESS
    execute_info.lpVerb = verb
    execute_info.lpFile = file
    execute_info.lpParameters = parameters
    execute_info.nShow = SW_SHOWNORMAL

    try:
        if not shell32.ShellExecuteExW(ctypes.byref(execute_info)):
            raise ctypes.WinError(ctypes.GetLastError())
        if not execute_info.hProcess:
            raise RuntimeError("系统没有返回可等待的进程。")

        kernel32.WaitForSingleObject(execute_info.hProcess, INFINITE)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(execute_info.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError()
        return int(exit_code.value)
    finally:
        if execute_info.hProcess:
            kernel32.CloseHandle(execute_info.hProcess)


def request_auto_start_update(enabled: bool):
    """
    请求更新开机自启：弹 UAC，由提权 helper 子进程完成实际写入。

    Raises:
        RuntimeError: 非 Windows / 用户取消 UAC / helper 写入失败（含错误详情）
    """
    if not _is_windows():
        raise RuntimeError("当前平台不支持开机自启配置。")

    error_file = tempfile.NamedTemporaryFile(prefix="drifox_startup_", suffix=".txt", delete=False)
    error_path = error_file.name
    error_file.close()

    executable, parameters = _helper_invocation(enabled, error_path)

    try:
        try:
            exit_code = shell_execute_and_wait("runas", executable, parameters)
        except OSError as exc:
            # 1223 = ERROR_CANCELLED：用户在 UAC 弹窗点了"否"
            if getattr(exc, "winerror", None) == 1223:
                raise AutoStartCancelled() from exc
            raise
        if exit_code != 0:
            message = ""
            try:
                message = Path(error_path).read_text(encoding="utf-8").strip()
            except Exception:
                message = ""
            raise RuntimeError(message or "开机自启系统配置失败。")
        logger.info(f"[AutoStart] 提权 helper 完成: enabled={enabled}")
    finally:
        try:
            Path(error_path).unlink(missing_ok=True)
        except Exception:
            pass


def maybe_handle_startup_helper(argv: list[str]) -> int | None:
    """
    检查命令行是否为提权 helper 调用。

    是则执行注册表写入并返回进程退出码（0 成功 / 1 失败）；
    否则返回 None，主进程继续正常启动。
    必须在 QApplication 创建之前调用（helper 进程不加载 Qt）。
    """
    flag = None
    error_path = ""
    for item in argv:
        if item.startswith("--configure-auto-start="):
            flag = item.split("=", 1)[1].strip().lower()
        elif item.startswith("--startup-error-file="):
            error_path = item.split("=", 1)[1]

    if flag not in {_HELPER_FLAG_ON, _HELPER_FLAG_OFF}:
        return None

    try:
        configure_auto_start(flag == _HELPER_FLAG_ON)
        return 0
    except Exception as exc:
        if error_path:
            try:
                Path(error_path).write_text(str(exc), encoding="utf-8")
            except Exception:
                pass
        return 1


# ── 启动时同步 ───────────────────────────────────────────────


def sync_auto_start_from_config():
    """
    启动时同步：校验 HKLM 注册表健康度，防止云端配置覆盖打回本机自启状态。

    HKLM 写入需要 UAC，启动时不能自动提权，因此本函数**只读不写注册表**：
    - 注册表命令与当前预期一致（本机真实开启）：
      配置为 False（多见于云端 app.config 覆盖）→ 回写配置为 True，注册表为准
    - 注册表命令过期（exe 挪位置 / 开发↔打包切换）：
      保持配置 False，日志提示需在设置中重新开启（重开会触发 UAC 重写）
    - 注册表无项：
      配置为 True 时仅日志提示（可能云端覆盖或提权未完成），不自动提权补写
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
        reg_cmd = get_registered_command()
        logger.info(f"[AutoStart] 配置={config_enabled}, HKLM注册表={'无' if reg_cmd is None else '有'}")

        if reg_cmd is None:
            if config_enabled:
                logger.warning("[AutoStart] 配置为开但注册表无项（云端覆盖或提权未完成），请在设置中重新开启")
            return

        expected_cmd = build_startup_command()
        if reg_cmd != expected_cmd:
            # 命令过期：exe 挪位置或开发↔打包切换。配置回 False 引导用户重开（触发 UAC 重写）
            logger.warning("[AutoStart] 注册表命令过期，自启不会生效，请在设置中关闭后重新开启")
            if config_enabled:
                cfg.set(cfg.auto_start, False, save=True)
            return

        # 本机真实开启：云端覆盖把配置打成 False 时，以本机注册表为准回写
        if not config_enabled:
            logger.info("[AutoStart] 配置为关但注册表有效（疑似云端覆盖），以本机注册表为准回写配置")
            cfg.set(cfg.auto_start, True, save=True)
    except Exception:
        logger.exception("[AutoStart] 同步开机自启状态失败")
