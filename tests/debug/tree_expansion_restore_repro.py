# -*- coding: utf-8 -*-
"""工作区树折叠态恢复复现（用户报告：一直没生效）

模拟 TabPanel 启动链：set_expansion_state(存储值) → rebuild(specs)，
检查各 header 的实际展开态是否与存储值一致。
"""

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

from app.widgets.workspace_tree import KIND_PROJECT, KIND_WORKTREE, WorkspaceTree
from app.widgets.workspace_tree import TreeNodeSpec

state = json.loads(
    """{
 "project:DriFox": true,
 "project:3d小镇": false,
 "worktree:3d小镇|": false,
 "project:默认项目": false,
 "worktree:默认项目|": false,
 "worktree:梅钢脱硫脱硝|": false,
 "worktree:DriFox|": false,
 "worktree:DriFox|D:/work/DriFox": false,
 "project:AI弹幕软件": false,
 "worktree:AI弹幕软件|": true,
 "project:DriFoxPlugins": true,
 "worktree:AI弹幕软件|C:/Users/black/.drifox/workspaces/AI弹幕软件": true,
 "project:canvas_mind": false
}"""
)

tree = WorkspaceTree()
tree.set_expansion_state(state)
print("set_expansion_state 后 _expanded 键数:", len(tree._expanded))

# 模拟真实 specs（当前项目=DriFox → expanded_by_default=True；
# DriFoxPlugins 是非当前项目、无打开 Tab → expanded_by_default=False）
specs = [
    TreeNodeSpec(key="project:DriFox", kind=KIND_PROJECT, title="DriFox", expanded_by_default=True, bold=True),
    TreeNodeSpec(key="worktree:DriFox|", kind=KIND_WORKTREE, title="主仓库", expanded_by_default=True),
    TreeNodeSpec(key="project:3d小镇", kind=KIND_PROJECT, title="3d小镇", expanded_by_default=False, bold=True),
    TreeNodeSpec(key="worktree:3d小镇|", kind=KIND_WORKTREE, title="主仓库", expanded_by_default=False),
    TreeNodeSpec(key="project:DriFoxPlugins", kind=KIND_PROJECT, title="DriFoxPlugins", expanded_by_default=False, bold=True),
    TreeNodeSpec(key="project:AI弹幕软件", kind=KIND_PROJECT, title="AI弹幕软件", expanded_by_default=False, bold=True),
    TreeNodeSpec(key="worktree:AI弹幕软件|", kind=KIND_WORKTREE, title="主仓库", expanded_by_default=False),
]

tree.rebuild(specs)

print()
print("rebuild 后各 header 实际展开态（存储值 vs 实际）：")
ok = True
for key in [
    "project:DriFox",
    "worktree:DriFox|",
    "project:3d小镇",
    "worktree:3d小镇|",
    "project:DriFoxPlugins",
    "project:AI弹幕软件",
    "worktree:AI弹幕软件|",
]:
    h = tree._headers.get(key)
    stored = state.get(key)
    actual = h._expanded if h is not None else None
    mark = "OK" if actual == stored else "!!"
    if actual != stored:
        ok = False
    print(f"  [{mark}] {key}: stored={stored} actual={actual}")

print()
print("结论：", "恢复生效" if ok else "恢复存在不一致")
