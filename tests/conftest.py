# -*- coding: utf-8 -*-
"""
Pytest 配置

提供测试所需的 Python 路径和 Qt 环境初始化。
Drifox 主入口位于仓库根目录，运行 `pytest` 时需将根目录加入 sys.path，
以便 `import app.*`。

Qt 环境说明：
- QWebEngineWidgets 必须在 QApplication 创建前导入
- Qt.AA_ShareOpenGLContexts 必须在 QApplication 创建前设置
"""

import sys
from pathlib import Path

# 仓库根目录 = tests/ 的父目录
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 在导入任何 QApplication 之前，先设置 Qt 属性并导入 WebEngineWidgets
#
# 修复历史：
# - 原版使用 ``Qt.QT_VERSION`` 触发 PyQt5 初始化，但该属性在 PyQt5 5.15 中不存在
#   （PyQt5 对应属性为 ``QT_VERSION_STR``，PyQt6 才是 ``Qt.QT_VERSION``）。
# - 由于整段逻辑被一个大 ``try/except`` 包裹，此 AttributeError 被完全静默吞掉，
#   后续 ``setAttribute(Qt.AA_ShareOpenGLContexts, True)`` 与
#   ``QWebEngineWidgets`` 预导入都没有真正生效。
# - 下游测试模块在创建 ``QApplication`` 时触发
#   ``STATUS_STACK_BUFFER_OVERRUN``（Windows 退出码 3221226505），
#   现象是 pytest 收到一个 0 字节输出后崩溃。
# - 现版本改用 PyQt5 实际支持的 ``QT_VERSION_STR``，并用
#   ``warnings.warn`` 将仍然存在的初始化错误显式报告出来，避免再次静默失败。
_QT_INIT_ERRORS: list[str] = []
try:
    from PyQt5.QtCore import Qt, QT_VERSION_STR as _QT_VERSION_STR  # noqa: F401

    # 必须在 QApplication 之前设置 OpenGL 共享
    try:
        from PyQt5.QtWidgets import QApplication

        if not QApplication.instance():
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    except Exception as _e:
        _QT_INIT_ERRORS.append(f"setAttribute: {_e!r}")
    # 预导入 WebEngineWidgets（如果可用），必须在 QApplication 之前完成
    try:
        from PyQt5.QtWebEngineWidgets import (  # noqa: F401
            QWebEnginePage,
            QWebEngineSettings,
            QWebEngineView,
        )
    except Exception as _e:
        _QT_INIT_ERRORS.append(f"WebEngineWidgets: {_e!r}")
except Exception as _e:
    _QT_INIT_ERRORS.append(f"Qt import: {_e!r}")

if _QT_INIT_ERRORS:
    import warnings

    warnings.warn(
        f"conftest.py Qt 预初始化存在问题（{len(_QT_INIT_ERRORS)} 项），"
        f"部分 UI 测试可能受影响。详情: {_QT_INIT_ERRORS}",
        RuntimeWarning,
        stacklevel=1,
    )
