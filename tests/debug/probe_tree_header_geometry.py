# -*- coding: utf-8 -*-
"""探针：打印项目头 / 会话行各子控件的实际几何（用于确定引导线 x 坐标）

offscreen 下也能拿到布局后的坐标（需先 show + processEvents）。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from app.widgets.tab_panel import _project_icon_data  # noqa: E402
from app.widgets.workspace_tree import KIND_PROJECT, KIND_SESSION, TreeNodeSpec, WorkspaceTree  # noqa: E402

initials, color = _project_icon_data("DriFox")
tree = WorkspaceTree()
tree.resize(260, 400)
tree.rebuild(
    [
        TreeNodeSpec(
            key="project:DriFox",
            kind=KIND_PROJECT,
            title="DriFox",
            bold=True,
            count=1294,
            active_count=1,
            initials=initials,
            color=color,
            expanded_by_default=True,
        ),
        TreeNodeSpec(key="session:s1", kind=KIND_SESSION, title="会话一行", indent=42, subtitle="预览 · 时间"),
    ]
)
tree.show()


def dump():
    h = tree._headers.get("project:DriFox")
    print("=== 项目头 ===")
    for name in ("_arrow", "_icon_label", "_badge", "_title", "_active_dot", "_active_num", "_count_label", "_new_btn"):
        w = getattr(h, name, None)
        if w is None:
            continue
        g = w.geometry()
        print(f"  {name:14s} visible={not w.isHidden()} x={g.x()} y={g.y()} w={g.width()} h={g.height()}")
    print("  header height:", h.height(), "margins:", h.layout().getContentsMargins())
    r = tree._rows.get("session:s1")
    print("=== 会话行 ===", "height:", r.height(), "margins:", r.layout().getContentsMargins())
    sub = getattr(r, "_sub", None)
    if sub is not None:
        g = sub.geometry()
        print(f"  _sub x={g.x()} w={g.width()} (绝对 x={g.x() + r.x()})")
    app.quit()


QTimer.singleShot(0, dump)
app.exec_()
