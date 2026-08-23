# -*- coding: utf-8 -*-
"""验收夹具：看板工作区页面（复制到 ~/.drifox/plugins/workspace-demo/ 手动验收）

手动点检步骤：
  1. cp -r tests/plugins/e2e/workspace-demo ~/.drifox/plugins/
  2. 启动 DriFox，侧边栏出现「看板」入口
  3. 点击进入页面（content_area 切页）
  4. 输入 `/workspace-demo:kanban` 命令直达
  5. 删除插件目录热卸载 → 页面与入口消失，回退对话页
"""

from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget


class KanbanPage(QWidget):
    def __init__(self, parent=None, context=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        wid = context.get("window_id") if context else "?"
        lay.addWidget(QLabel(f"看板页面（window={wid}）", self))


def register_ui(registry):
    registry.register_workspace_page(
        "workspace-demo", "kanban", "看板", KanbanPage, order_hint=10, icon_path=""
    )