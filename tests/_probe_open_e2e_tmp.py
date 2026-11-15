# -*- coding: utf-8 -*-
"""临时探针：离屏真实宿主下验证「首次打开工作台即落插件预览页」。

用完即删，只用于确认 open_workbench_page 的 reconcile 顺序在真实
TabManagerWindow + WorkbenchPanel 上成立。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication, QWidget


def drain(rounds=8, ms=10):
    for _ in range(rounds):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec_()


class _PreviewPage(QWidget):
    def __init__(self, parent=None, context=None):
        super().__init__(parent)
        self._context = context or {}
        self.previewed = []

    def preview_path(self, path):
        self.previewed.append(path)
        return True


def main():
    app = QApplication.instance() or QApplication([])
    from app.tray_manager import TrayManager
    from app.widgets.tab_manager_window import TabManagerWindow
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    tm = TabManagerWindow.create_instance()
    tm.complete_construction()
    tm.show()
    drain()

    reg = UIPluginRegistry.get_instance()
    # 模拟 file-tree：注册工作台页 + file opener，但不预先 sync（首次打开）
    reg.register_workbench_tab(
        plugin_name="probe-plugin",
        page_id="probe-tree",
        label="探针页",
        widget_class=_PreviewPage,
        priority=10,
        metadata={"source": "test", "order_hint": 15},
    )
    reg.register_file_opener(
        plugin_name="probe-plugin",
        opener_id="preview",
        can_open=lambda p: True,
        open_file=lambda p: bool(reg.open_workbench_page("probe-tree").preview_path(p)),
        priority=10,
    )
    tm.set_workbench_visible(False, animate=False)
    drain()
    print("before: visible=", tm.is_workbench_visible(), "page=", reg.get_workbench_page("probe-tree"))

    ok = reg.try_open_local_file("D:/work/DriFox/app/main.py")
    drain(20, 20)
    page = reg.get_workbench_page("probe-tree")
    panel = tm.workbench_panel
    print("after: taken=", ok)
    print("  visible=", tm.is_workbench_visible())
    print("  page=", page is not None)
    print("  previewed=", getattr(page, "previewed", None))
    print("  current_tab_id=", panel.current_tab_id())
    reg.unregister_file_openers("probe-plugin")
    reg.unregister_workbench_tabs("probe-plugin")
    print("after unload: taken=", reg.try_open_local_file("D:/work/DriFox/app/main.py"))
    tm.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
