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
try:
    from PyQt5.QtCore import Qt
    Qt.QT_VERSION  # 触发 PyQt5 初始化
    # 必须在 QApplication 之前设置 OpenGL 共享
    try:
        from PyQt5.QtWidgets import QApplication
        if not QApplication.instance():
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    except Exception:
        pass
    # 预导入 WebEngineWidgets（如果可用）
    try:
        from PyQt5.QtWebEngineWidgets import (  # noqa: F401
            QWebEnginePage, QWebEngineSettings, QWebEngineView,
        )
    except Exception:
        pass
except Exception:
    pass
