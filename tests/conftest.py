# -*- coding: utf-8 -*-
"""
Pytest 配置

提供测试所需的 Python 路径和 Qt 环境初始化。
Drifox 主入口位于仓库根目录，运行 `pytest` 时需将根目录加入 sys.path，
以便 `import app.*`。

Qt 环境说明（Fixture 策略）：
- 模块级只做属性设置和导入声明，不创建 QApplication
- ``Qt.AA_ShareOpenGLContexts`` 在 QApplication 创建前立即设置（修复 STATUS_STACK_BUFFER_OVERRUN）
- QWebEngineWidgets 在 QApplication 创建前预导入
- QApplication 通过 session-scoped autouse fixture 创建，确保时机可控
"""

import sys
from pathlib import Path

import pytest

# 仓库根目录 = tests/ 的父目录
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 收集 Qt 初始化错误
_QT_INIT_ERRORS: list[str] = []

# 必须在 QApplication 之前设置 OpenGL 共享（方案 B）
# 注意：此属性必须在 QApplication 实例化前设置，否则不生效
try:
    from PyQt5.QtCore import Qt, QT_VERSION_STR  # noqa: F401

    from PyQt5.QtWidgets import QApplication

    # 立即设置，QApplication 创建前生效
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
except Exception as _e:
    _QT_INIT_ERRORS.append(f"Qt/Attribute: {_e!r}")

# 预导入 WebEngineWidgets，必须在 QApplication 之前完成
try:
    from PyQt5.QtWebEngineWidgets import (  # noqa: F401
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineView,
    )
except Exception as _e:
    _QT_INIT_ERRORS.append(f"WebEngineWidgets: {_e!r}")

if _QT_INIT_ERRORS:
    import warnings

    warnings.warn(
        f"conftest.py Qt 预初始化存在问题（{len(_QT_INIT_ERRORS)} 项），"
        f"部分 UI 测试可能受影响。详情: {_QT_INIT_ERRORS}",
        RuntimeWarning,
        stacklevel=1,
    )


@pytest.fixture(scope="session", autouse=True)
def _qt_app():
    """创建 session 级 QApplication（fixture 化确保时机可控）。"""
    app = QApplication([])
    yield app
    app.quit()
