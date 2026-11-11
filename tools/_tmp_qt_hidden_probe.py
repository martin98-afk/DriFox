# -*- coding: utf-8 -*-
"""[DEBUG-reopen] Qt 行为实验：父不可见时 setVisible(True) 是否清 isHidden"""
import sys

from PySide6.QtWidgets import QApplication, QWidget

app = QApplication(sys.argv)

parent = QWidget()  # 从不 show
child = QWidget(parent)

print("初始: isHidden =", child.isHidden(), "isVisible =", child.isVisible())

child.setVisible(True)
print("父不可见 setVisible(True): isHidden =", child.isHidden(), "isVisible =", child.isVisible())

parent.show()
print("父 show 后: isHidden =", child.isHidden(), "isVisible =", child.isVisible())

# 对照：父可见时再 hide/show
child.setVisible(False)
child.setVisible(True)
print("父可见 setVisible(True): isHidden =", child.isHidden(), "isVisible =", child.isVisible())
