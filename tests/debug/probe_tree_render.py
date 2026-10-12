# -*- coding: utf-8 -*-
"""渲染探针：把工作区树画到 PNG，检查行高 / 引导线 / 缩进 / 时间右对齐

输出 _tmp_tree_after.png（工作区树区域）。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

from app.utils.design_tokens import Colors  # noqa: E402

# 探针环境没有加载主题文件，Colors 停在深色 fallback（白字），白底上看不见文字。
# 显式注入浅色（lumia）主题色值，让 grab 出的图反映真实浅色主题观感。
Colors.TEXT_PRIMARY = "#0f172a"
Colors.TEXT_MUTED = "#5b6b84"
Colors.HOVER_BG = "rgba(37, 99, 235, 0.08)"
Colors.INFO = "#2563eb"
Colors.DIVIDER_COLOR = "rgba(0, 0, 0, 0.08)"

from app.widgets.tab_panel import _project_icon_data  # noqa: E402
from app.widgets.workspace_tree import (  # noqa: E402
    KIND_PROJECT,
    KIND_SESSION,
    TreeNodeSpec,
    WorkspaceTree,
    tree_content_left,
    tree_guide_x,
)

initials, color = _project_icon_data("DriFox")
print(f"tree_content_left={tree_content_left()} tree_guide_x={tree_guide_x()}")

tree = WorkspaceTree()
tree.setStyleSheet("background: #ffffff;")
tree.resize(260, 420)
tree.rebuild(
    [
        TreeNodeSpec(
            key="project:DriFox",
            kind=KIND_PROJECT,
            title="DriFox",
            bold=True,
            is_project=True,
            subtitle="9 个会话",
            active_count=1,
            initials=initials,
            color=color,
            expanded_by_default=True,
        ),
        TreeNodeSpec(
            key="tab:0",
            kind=KIND_SESSION,
            title="对话 10-12 18:51",
            indent=42,
            subtitle="",
            subtitle_time="更早",
            record={"session_id": "cur"},
            tooltip="当前对话",
        ),
        TreeNodeSpec(
            key="session:s1",
            kind=KIND_SESSION,
            title="强制重新发布版本",
            indent=42,
            subtitle="再重新发布一下，我…",
            subtitle_time="20小时前",
            record={"session_id": "s1"},
        ),
        TreeNodeSpec(
            key="session:s2",
            kind=KIND_SESSION,
            title="修复git仓库",
            indent=42,
            subtitle="修复当前git仓库",
            subtitle_time="21小时前",
            record={"session_id": "s2"},
        ),
        TreeNodeSpec(
            key="project:Other",
            kind=KIND_PROJECT,
            title="OtherProj",
            bold=True,
            is_project=True,
            subtitle="3 个会话",
            initials="OP",
            color="rgba(200,120,60,255)",
            expanded_by_default=False,
        ),
    ]
)
tree.show()
app.processEvents()

pix = tree.grab()
pix.save("_tmp_tree_after.png")
print("saved _tmp_tree_after.png", pix.width(), "x", pix.height())
for key, h in tree._headers.items():
    print(f"  header {key}: h={h.height()} has_children={h._has_children}")
for key, r in tree._rows.items():
    m = r.layout().getContentsMargins()
    print(f"  row {key}: h={r.height()} margins={m} sub={r._sub.text()!r} time={r._sub_time.text()!r}")
