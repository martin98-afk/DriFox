"""窗口 resize 命中测试验证探针（不修改业务代码，仅诊断/回归验证）

判定依据：Windows 只把 WM_NCHITTEST 发给鼠标下的 HWND；主窗口 nativeEvent
对命中测试的返回值决定系统是否进入 resize 模态循环：
  HTCLIENT(1) → 客户区，不能 resize
  HTLEFT/HTRIGHT/HTTOP/HTBOTTOM/角落 → 可 resize

坐标基准用 mapToGlobal(QPoint(0,0)) 取客户区左上角，避免 frameGeometry 与
DWM 阴影带来的偏移（v2 曾因此出现"中心点报 HTTOPRIGHT"的假阳性）。
"""

import sys
import time
from ctypes import wintypes

import ctypes
from PyQt5.QtCore import Qt, QPoint
from PyQt5.QtWidgets import QApplication

WM_NCHITTEST = 0x0084
user32 = ctypes.windll.user32
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t

HT_NAMES = {
    1: "HTCLIENT  ❌ 不能 resize",
    2: "HTCAPTION  (可拖动)",
    10: "HTLEFT     ✅",
    11: "HTRIGHT    ✅",
    12: "HTTOP      ✅",
    13: "HTTOPLEFT  ✅",
    14: "HTTOPRIGHT ✅",
    15: "HTBOTTOM   ✅",
    16: "HTBOTTOMLEFT  ✅",
    17: "HTBOTTOMRIGHT ✅",
}


def makelong(lo: int, hi: int) -> int:
    return (hi << 16) | (lo & 0xFFFF)


def settle(app, n: int = 40, wait: float = 0.35) -> None:
    for _ in range(n):
        app.processEvents()
    time.sleep(wait)
    for _ in range(n):
        app.processEvents()


def probe(win, tag: str) -> None:
    hwnd = int(win.winId())
    # 客户区左上角的屏幕坐标（无边框窗口 ⇒ 即窗口左上角）
    origin = win.mapToGlobal(QPoint(0, 0))
    ox, oy = origin.x(), origin.y()
    w, h = win.width(), win.height()

    print(f"\n══ {tag} ══")
    print(f"HWND=0x{hwnd:X} 客户区原点=({ox},{oy}) 尺寸={w}x{h}")
    style = user32.GetWindowLongPtrW(wintypes.HWND(hwnd), -16)
    print(f"style=0x{style:08X}  WS_THICKFRAME={'✅' if style & 0x40000 else '❌'}")

    sbl = win._system_buttons_left()
    print(f"_system_buttons_left() = {sbl}   (0 ⇒ 顶边整条失效)")
    if 0 < sbl < w:
        print(f"  → 顶边 0..{sbl - 1}px 为 resize 热区，{sbl}..{w - 1}px 让位给按钮 ✅")
    elif sbl == 0:
        print("  → ❌ 顶边全部让位，顶边 resize 失效")
    else:
        print("  → ⚠️ 无让位（按钮未就绪），顶边整条可 resize")

    cases = [
        ("顶缘 左段 x=40", ox + 40, oy + 2),
        ("顶缘 中段", ox + w // 2, oy + 2),
        ("顶缘 右段(按钮区)", ox + w - 100, oy + 2),
        ("左上角", ox + 2, oy + 2),
        ("右上角", ox + w - 2, oy + 2),
        ("左缘", ox + 2, oy + h // 2),
        ("右缘", ox + w - 2, oy + h // 2),
        ("底缘", ox + w // 2, oy + h - 2),
        ("左下角", ox + 2, oy + h - 2),
        ("右下角", ox + w - 2, oy + h - 2),
        ("客户区中心", ox + w // 2, oy + h // 2),
    ]
    for name, px, py in cases:
        v = user32.SendMessageW(wintypes.HWND(hwnd), WM_NCHITTEST, 0, makelong(px, py))
        print(f"  {name:<20} → {HT_NAMES.get(v, f'未知 {v}')}")


def main() -> int:
    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_EnableHighDpiScaling, True)

    import logging

    logging.disable(logging.INFO)

    from app.widgets.tab_manager_window import TabManagerWindow

    win = TabManagerWindow.create_instance()
    win.resize(1000, 700)
    win.move(200, 150)
    win.show()
    settle(app)

    probe(win, "场景 A：默认启动（工作台关闭）")

    if getattr(win, "workbench_panel", None) is not None:
        win.reposition_workbench()
        win.workbench_panel.show()
        win.workbench_panel.raise_()
        settle(app, 20, 0.2)
        probe(win, "场景 B：工作台面板打开（贴右缘）")
        win.workbench_panel.hide()
        settle(app, 20, 0.2)

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
