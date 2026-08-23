# -*- coding: utf-8 -*-
"""UI 槽位契约 — Region（区域）+ SlotEntry（条目）通用挂载模型

宿主（主程序或插件宿主）声明区域，插件向区域挂载条目。
新增 UI 槽位 = 宿主 declare_region + 消费 get_region_entries，无需改注册表 API。
"""

from dataclasses import dataclass, field
from typing import Any, Dict

# ── 区域种类（字符串常量而非 Enum：允许宿主自定义扩展值）──
MENU = "menu"                # 菜单区域（右键菜单/下拉菜单，region_id 约定 "menu:<target>"）
LIST_ITEM = "list_item"      # 列表条目区域（侧边栏/会话列表）
TOOLBAR_BUTTON = "toolbar_button"  # 工具栏按钮区域（region_id 约定 "toolbar:<name>"）
PANEL = "panel"              # 面板容器区域（设置面板/卡片容器）
CONTENT = "content"          # 内容渲染区域（HTML 片段/widget 工厂）

VALID_REGION_KINDS = frozenset({MENU, LIST_ITEM, TOOLBAR_BUTTON, PANEL, CONTENT})


@dataclass(frozen=True)
class SlotEntry:
    """区域条目 — 插件挂载到某区域的单个 UI 单元

    Attributes:
        entry_id: 条目唯一 ID（region 内唯一；同 id 高 priority 覆盖低者）
        plugin_name: 所属插件名（unload 时按此清理）
        region_id: 所属区域 ID
        priority: 排序权重（大者在前）；同 id 覆盖判定也用它
        payload: 条目负载 — 由区域 kind 约定结构（dict/Info 对象/回调等）
        metadata: 附加元数据（图标路径、分组、可见性等）
    """

    entry_id: str
    plugin_name: str
    region_id: str
    priority: int = 0
    payload: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
