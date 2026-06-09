# -*- coding: utf-8 -*-
"""
消息卡片导出图片相关函数的单元测试

覆盖：
- _get_card_bg_color: rgba 字符串解析、强制实心、找不到父链兜底
- _split_and_stitch: 拼接宽度、列数选择、短图不切分

注意：PyQt5.QtWebEngineWidgets 必须在 QApplication 创建之前导入，
否则会抛 ImportError: ... must be imported or Qt.AA_ShareOpenGLContexts
must be set before a QCoreApplication instance is created
"""
import sys
from pathlib import Path

# 必须在 QApplication 之前 import
from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from PyQt5.QtWidgets import QApplication


# ─────────────────────────────────────────────
#  PyQt 应用 fixture（测试只创建不弹窗的 widget，需要 QApplication）
# ─────────────────────────────────────────────
@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app
    # 不退出 app，会话复用


# ─────────────────────────────────────────────
#  _split_and_stitch 测试
# ─────────────────────────────────────────────
class TestSplitAndStitch:
    """_split_and_stitch: 横向拼接多段为横幅"""

    def test_horizontal_strip_width_equals_sum(self, qapp):
        """超长图（高宽比 6:1）切 3 段，拼接后宽=3×单段宽"""
        from PyQt5.QtGui import QPixmap, QColor
        from app.widgets.message_card import CodeWebViewer

        # 100x600 = 高宽比 6:1
        # _split_and_stitch 会计算 best_cols:
        #   cols=2: ratio=0.667, diff=0.833
        #   cols=3: ratio=1.500, diff=0  ← 最佳
        #   cols=4: ratio=2.667, diff=1.167
        # 所以 best_cols=3，拼接后宽=300，高=200
        full = QPixmap(100, 600)
        full.fill(QColor(255, 0, 0))

        result = CodeWebViewer._split_and_stitch(None, full, max_cols=6)

        # 3 段水平拼接：宽=3×100=300
        assert result.width() == 300, f"expected 300, got {result.width()}"
        # 单段高=200（最后一段 600-400=200）
        assert result.height() == 200, f"expected 200, got {result.height()}"

    def test_near_square_image_not_split(self, qapp):
        """高宽比 1:1 的近方图不切分，返回原图"""
        from PyQt5.QtGui import QPixmap, QColor
        from app.widgets.message_card import CodeWebViewer

        full = QPixmap(400, 400)
        full.fill(QColor(0, 255, 0))

        result = CodeWebViewer._split_and_stitch(None, full, max_cols=6)

        # 不切分时返回原图
        assert result.width() == 400
        assert result.height() == 400

    def test_slight_tall_image_split_into_2(self, qapp):
        """高宽比 1.2:1 的图仍会切 2 段（_split_and_stitch 自己的判断）"""
        from PyQt5.QtGui import QPixmap, QColor
        from app.widgets.message_card import CodeWebViewer

        # 400x480: (h+w-1)//w+1 = (480+400-1)//400+1 = 3
        # best_cols=2 (cols=2 时 ratio=2*400/240=3.33, diff=1.83)
        # 切 2 段：拼接宽=800, 高=240
        # 注：_export_as_image 不会调用到这里（它有 full.height() > full.width() * 1.5 保护）
        full = QPixmap(400, 480)
        full.fill(QColor(0, 0, 255))

        result = CodeWebViewer._split_and_stitch(None, full, max_cols=6)

        assert result.width() == 800, f"expected 800, got {result.width()}"
        assert result.height() == 240, f"expected 240, got {result.height()}"
