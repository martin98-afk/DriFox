# -*- coding: utf-8 -*-
"""验证 sys.modules 模块身份与 GC 假说"""
import gc
import sys

import sip

import qfluentwidgets.common.config as _qfw_mod
from qfluentwidgets.common.config import qconfig


def test_modules_identity():
    print("in sys.modules:", "qfluentwidgets.common.config" in sys.modules)
    print("sys.modules entry is module:", sys.modules.get("qfluentwidgets.common.config") is _qfw_mod)
    print("module attr qconfig is imported qconfig:", _qfw_mod.qconfig is qconfig)
    print("deleted now:", sip.isdeleted(qconfig))

    gc.collect()
    print("after gc.collect: deleted =", sip.isdeleted(qconfig))

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    print("after QApplication: deleted =", sip.isdeleted(qconfig))

    import app.constants  # noqa: F401

    print("after app.constants: deleted =", sip.isdeleted(qconfig))
    print("post: in sys.modules:", "qfluentwidgets.common.config" in sys.modules)
    print("post: sys.modules entry is module:", sys.modules.get("qfluentwidgets.common.config") is _qfw_mod)
    print("post: module attr qconfig is imported qconfig:", _qfw_mod.qconfig is qconfig)
