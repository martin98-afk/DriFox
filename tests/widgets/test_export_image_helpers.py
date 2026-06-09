# -*- coding: utf-8 -*-
"""
消息卡片导出图片相关函数的单元测试

覆盖：
- _get_card_bg_color: rgba 字符串解析、强制实心、找不到父链兜底
- _split_and_stitch: 拼接宽度、列数选择、短图不切分
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pytest


# ─────────────────────────────────────────────
#  PyQt 应用 fixture（测试只创建不弹窗的 widget，需要 QApplication）
# ─────────────────────────────────────────────
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app
    # 不退出 app，会话复用
