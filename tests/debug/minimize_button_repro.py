# 端到端验证：StaysOnTopHint → level 8（复现）→ 修复后软置顶分支 → level 0
import ctypes
import sys
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import Qt

app = QApplication(sys.argv)
ob = ctypes.CDLL("/usr/lib/libobjc.dylib")
ob.objc_msgSend.restype = ctypes.c_void_p
ob.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
ob.sel_registerName.restype = ctypes.c_void_p
ob.sel_registerName.argtypes = [ctypes.c_char_p]


def sel(n):
    return ob.sel_registerName(n.encode())


def msg(o, n):
    return ob.objc_msgSend(ctypes.c_void_p(o), sel(n))


def level_of(w):
    view = int(w.winId())
    nswin = msg(view, "window")
    return int(msg(nswin, "level") or 0)


from app.widgets import tab_manager_window as tmw

fake = type("Cfg", (), {"window_always_on_top": type("I", (), {"value": True})()})()

w = QWidget()
w.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)  # 模拟历史残留
w.show()
app.processEvents()
lv_before = level_of(w)
print(f"残留 StaysOnTopHint 时 level={lv_before}（复现：非0 → 最小化被系统丢弃）")

with (
    patch("app.utils.config.Settings.get_instance", return_value=fake),
    patch.object(tmw, "_IS_MAC", True),
):
    tmw._apply_window_topmost(w)  # 置顶开启也走软置顶分支
app.processEvents()
lv_after = level_of(w)
print(f"软置顶修复后 level={lv_after}（0=normal，最小化恢复）")
assert lv_before == 8 and lv_after == 0, "验证失败"
print("端到端验证通过")
sys.exit(0)
