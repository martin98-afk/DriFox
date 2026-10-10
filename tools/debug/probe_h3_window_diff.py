# -*- coding: utf-8 -*-
"""H3 下沉写法整窗像素等价性验证（临时产物，不进 git）

H3 = handle 自身 WA_StyledBackground + `QSplitterHandle {base} QSplitterHandle:hover {hover}`。
已在单 handle grab 上验证：基础态像素与现状一致（同色像素计数），hover 伪状态可触发。

本脚本对**整窗合成结果**做逐像素比对（grab 伪影在此口径下消失），
覆盖三个 splitter，并给出成本对比。hover 用 QEvent.Enter 触发后再整窗比对。

跑法::

    .venv\\Scripts\\python.exe .drifox-theme-perf\\probe_h3_window_diff.py
"""

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

os.environ["DRIFOX_DATA_DIR"] = str(HERE)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

out: list[str] = []


def _img(w):
    img = w.grab().toImage().convertToFormat(5)
    ptr = img.bits()
    ptr.setsize(img.byteCount())
    return img, bytes(ptr), img.width(), img.height()


def _diff(a, b, w, h, label) -> str:
    if len(a) != len(b):
        return f"{label}: 尺寸不同"
    diff = 0
    first = None
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            if a[i : i + 4] != b[i : i + 4]:
                diff += 1
                if first is None:
                    first = (x, y, a[i : i + 4].hex(), b[i : i + 4].hex())
    if diff == 0:
        return f"{label}: 完全一致 (0/{w*h})"
    return f"{label}: 差异 {diff}/{w*h} ({diff*100/(w*h):.2f}%) 首差 px{first[:2]} {first[2]}→{first[3]}"


def main() -> int:
    from PyQt5.QtCore import QCoreApplication, QEvent, Qt, QTimer
    from PyQt5.QtWidgets import QApplication, QWidget

    QCoreApplication.setAttribute(Qt.AA_UseOpenGLES)
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])

    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.utils.config import Settings
    from qfluentwidgets import Theme, setFontFamilies, setTheme

    init_shared_web_profile(app)
    settings = Settings.get_instance()
    try:
        setTheme(Theme.LIGHT if settings.theme_mode.value == "light" else Theme.DARK)
    except Exception:  # noqa: BLE001
        setTheme(Theme.DARK)
    setFontFamilies([settings.llm_font_family.value])

    class _FakePage(QWidget):
        def __init__(self):
            super().__init__()
            self.cfg = settings
            setFontFamilies([self.cfg.llm_font_family.value])

        def isActiveWindow(self):
            return True

        @property
        def workflow_name(self):
            return "theme_probe"

        @property
        def global_variables_changed(self):
            class _FakeSignal:
                def connect(self, *args, **kwargs):
                    pass

            return _FakeSignal()

        def setUpdatesEnabled(self, enabled):
            pass

        def update(self):
            pass

        def show_splitter(self):
            pass

        def hide_splitter(self):
            pass

    from app.main_widget import OpenAIChatToolWindow
    from app.widgets.tab_manager_window import TabManagerWindow
    from tools.ui_driver import place_offscreen

    page = _FakePage()
    tm = TabManagerWindow.create_instance()
    chat_window = OpenAIChatToolWindow(page)
    tm.add_window(chat_window)
    tm.show()
    place_offscreen(tm)

    def _t(fn, n=3):
        best = 1e9
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            best = min(best, (time.perf_counter() - t0) * 1000)
        return best

    def _bench():
        from app.utils.design_tokens import Colors

        B, BA = Colors.BORDER, Colors.BORDER_ACCENT
        cases = {
            "_splitter": (
                getattr(tm, "_splitter", None),
                "QSplitter::handle:horizontal { background: transparent; }",
                # 常量串，无主题色 → 下沉/短路均可
                "QSplitterHandle { background: transparent; }",
            ),
            "_dock_splitter": (
                getattr(tm, "_dock_splitter", None),
                (
                    f"#dockSplitter::handle:horizontal {{ background: transparent;"
                    f" border-left: 2px solid {B}; margin: 10px 2px; border-radius: 1px; }}"
                    f" #dockSplitter::handle:horizontal:hover {{ border-left: 2px solid {BA}; }}"
                ),
                (
                    f"QSplitterHandle {{ background: transparent; border-left: 2px solid {B};"
                    f" margin: 10px 2px; border-radius: 1px; }}"
                    f" QSplitterHandle:hover {{ border-left: 2px solid {BA}; }}"
                ),
            ),
            "_chat_vsplitter": (
                getattr(tm, "_chat_vsplitter", None),
                (
                    f"#chatVsplitter::handle:vertical {{ background: transparent;"
                    f" border-top: 2px solid {B}; margin: 2px 10px; border-radius: 1px; }}"
                    f" #chatVsplitter::handle:vertical:hover {{ border-top: 2px solid {BA}; }}"
                ),
                (
                    f"QSplitterHandle {{ background: transparent; border-top: 2px solid {B};"
                    f" margin: 2px 10px; border-radius: 1px; }}"
                    f" QSplitterHandle:hover {{ border-top: 2px solid {BA}; }}"
                ),
            ),
        }

        for name, (sp, qss_old, qss_h3) in cases.items():
            if sp is None:
                out.append(f"{name}: None")
                continue
            handles = [sp.handle(i) for i in range(sp.count())]
            out.append(f"\n===== {name} subtree={len(sp.findChildren(QWidget))} handles={len(handles)} =====")

            def _reset():
                sp.setStyleSheet("")
                for h in handles:
                    h.setStyleSheet("")
                    h.setAttribute(Qt.WA_StyledBackground, False)
                app.processEvents()

            # A 现状
            _reset()
            sp.setStyleSheet(qss_old)
            app.processEvents()
            _, ba, w, hgt = _img(tm)

            # 噪声基线
            app.processEvents()
            _, bn, _, _ = _img(tm)
            out.append("  " + _diff(ba, bn, w, hgt, "噪声基线"))

            # H3 下沉
            _reset()
            for hh in handles:
                hh.setAttribute(Qt.WA_StyledBackground, True)
                hh.setStyleSheet(qss_h3)
            app.processEvents()
            _, bh, _, _ = _img(tm)
            out.append("  " + _diff(ba, bh, w, hgt, "A(现状) vs H3(下沉)"))

            # hover 整窗比对：h0 进 hover
            if handles:
                h0 = handles[0]
                # 现状 hover
                _reset()
                sp.setStyleSheet(qss_old)
                h0.setAttribute(Qt.WA_Hover, True)
                QApplication.sendEvent(h0, QEvent(QEvent.Enter))
                app.processEvents()
                _, ba_hov, _, _ = _img(tm)
                QApplication.sendEvent(h0, QEvent(QEvent.Leave))
                # H3 hover
                _reset()
                for hh in handles:
                    hh.setAttribute(Qt.WA_StyledBackground, True)
                    hh.setStyleSheet(qss_h3)
                h0.setAttribute(Qt.WA_Hover, True)
                QApplication.sendEvent(h0, QEvent(QEvent.Enter))
                app.processEvents()
                _, bh_hov, _, _ = _img(tm)
                QApplication.sendEvent(h0, QEvent(QEvent.Leave))
                app.processEvents()
                out.append("  " + _diff(ba_hov, bh_hov, w, hgt, "hover 态: A vs H3"))
                # hover 是否真的改变渲染（整窗口径）
                out.append("  " + _diff(ba, ba_hov, w, hgt, "A 常态 vs A hover(整窗)"))

            # 成本
            ms_a = _t(lambda: sp.setStyleSheet(qss_old))
            ms_h = _t(lambda: [hh.setStyleSheet(qss_h3) for hh in handles])
            out.append(f"  成本 A={ms_a:.1f}ms  H3={ms_h:.1f}ms")

            _reset()
            sp.setStyleSheet(qss_old)

        for line in out:
            print("[h3] " + line, flush=True)

    QTimer.singleShot(3500, _bench)

    def _finish():
        p = HERE / "probe_h3_window_diff.txt"
        p.write_text("\n".join(out), encoding="utf-8")
        print(f"[probe] 写入 {p}", flush=True)
        app.quit()

    QTimer.singleShot(30_000, _finish)
    QTimer.singleShot(90_000, app.quit)

    rc = app.exec_()
    try:
        tm.cleanup()
    except Exception:  # noqa: BLE001
        pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
