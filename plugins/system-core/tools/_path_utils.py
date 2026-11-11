# -*- coding: utf-8 -*-
"""
系统工具插件 — 共享路径工具（自包含）

供 file_tools / diagnostics_tools 的预览闭包使用：把绝对路径转成相对项目根
目录的展示路径。基准取项目根路径（tool_executor.get_workdir，经
BackgroundTaskManager 全局通道暴露），而非进程运行目录 os.getcwd()——两者不同
会导致预览相对路径错误。无 workdir 时回退 os.getcwd()，保持原行为。
"""
import os


def to_rel_path(path: str) -> str:
    """将绝对路径转为相对项目根目录的路径（便于预览展示）"""
    if not path or not os.path.isabs(path):
        return path
    base = _preview_workdir()
    try:
        # normpath 统一分隔符后再比较
        if os.path.normpath(path).startswith(os.path.normpath(str(base))):
            rel = os.path.relpath(path, base)
            return rel.replace("\\", "/")
    except (ValueError, OSError):
        pass
    return path


def _preview_workdir() -> str:
    """获取预览相对路径的基准目录：优先项目根路径，回退运行目录"""
    try:
        from app.tools.bg_manager import BackgroundTaskManager

        inst = BackgroundTaskManager._instance
        if inst is not None:
            wd = inst._effective_workdir()
            if wd is not None:
                return str(wd)
    except Exception:
        pass
    return os.getcwd()
