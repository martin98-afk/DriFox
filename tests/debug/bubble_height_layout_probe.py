# -*- coding: utf-8 -*-
"""[DEBUG-bubble-height] 实验：QTextDocument 改 textWidth 后 size() 缓存行为 + 强制同步重排手段对比。"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QSizeF
from PyQt5.QtGui import QFont, QTextDocument
from PyQt5.QtWidgets import QApplication

URL = "https://example.com/long/path?q=1 " * 3

qapp = QApplication.instance() or QApplication(sys.argv)
doc = QTextDocument()
doc.setDefaultFont(QFont("Microsoft YaHei", 10))

# 阶段1：宽 999 正常排版（基准）
doc.setPlainText(URL)
doc.setTextWidth(999)
print(f"基准(999):        h={doc.size().height():.1f}")

# 阶段2：窄 64 排版（模拟窄 viewport 的 setPlainText）
doc.setTextWidth(64)
print(f"窄(64):           h={doc.size().height():.1f}")

# 阶段3：改宽 999 后立即读 size() —— 复现 bug
doc.setTextWidth(999)
print(f"改回999立即size:  h={doc.size().height():.1f}   ← 缓存未失效则为 404 级旧值")

# 手段A：markContentsDirty
doc.setTextWidth(64)
doc.markContentsDirty(0, doc.characterCount())
doc.setTextWidth(999)
print(f"A markDirty后:    h={doc.size().height():.1f}")

# 手段B：idealWidth() 触发
doc.setTextWidth(64)
doc.setTextWidth(999)
_ = doc.idealWidth()
print(f"B idealWidth后:   h={doc.size().height():.1f}")

# 手段C：setPageSize
doc.setTextWidth(64)
doc.setPageSize(QSizeF(999, 0))
print(f"C setPageSize后:  h={doc.size().height():.1f}")

# 手段D：setTextWidth(-1) 再设回
doc.setTextWidth(64)
doc.setTextWidth(-1)
_ = doc.size().height()
doc.setTextWidth(999)
print(f"D 置-1再回:       h={doc.size().height():.1f}")

# 手段E：documentLayout 首块 layout force —— QTextLayout 式逐块求和
doc.setTextWidth(64)
doc.setTextWidth(999)
total = 0.0
block = doc.begin()
while block.isValid():
    lay = block.layout()
    lay.setTextWidth(999) if hasattr(lay, "setTextWidth") else None
    block = block.next()
print(f"E 逐块layout后:   h={doc.size().height():.1f}")

# 手段F：QTextEdit 环境下（与产品一致）复现 + idealWidth 修复验证
from PyQt5.QtWidgets import QTextEdit

te = QTextEdit()
te.resize(64, 100)  # 模拟窄 viewport
te.show()
qapp.processEvents()
te.setPlainText(URL)
doc2 = te.document()
doc2.setTextWidth(64)
h_narrow = doc2.size().height()
doc2.setTextWidth(999)
h_cached = doc2.size().height()
_ = doc2.idealWidth()
h_fixed = doc2.size().height()
print(f"\nQTextEdit 内: 窄排版h={h_narrow:.1f} 改宽后立即={h_cached:.1f} idealWidth后={h_fixed:.1f}")
print(f"真值（新 doc 直接 999 排版）: ", end="")
doc3 = QTextDocument()
doc3.setDefaultFont(doc2.defaultFont())
doc3.setPlainText(URL)
doc3.setTextWidth(999)
print(f"{doc3.size().height():.1f}")
