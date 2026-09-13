"""卡片系统模块 - 统一管理所有卡片组件的显示和布局"""

# ⚠️ ContainerType 的唯一权威定义在 card_manager（TOP/BOTTOM/COMPLETION/LEFT/RIGHT）。
# 本模块历史上重复定义过一份只含 TOP/BOTTOM 的同名枚举：两个类对象**互不相等**
# （Enum 成员按 `is` 比较，哈希虽相同），而 CardManager 用容器类型作 dict 键 ——
# 一旦有调用方混用两份枚举，注册进 A 桶、查询按 B 桶，结果是 KeyError 或"卡片
# 注册了却永远显示不出来"，且不报任何警告。此处改为纯转发，从机制上杜绝该隐患。
from app.widgets.cards.card_container import (
    BottomCardContainer,
    CardContainer,
    CompletionCardContainer,
    TopCardContainer,
)
from app.widgets.cards.card_manager import CardManager, ContainerType

__all__ = [
    "ContainerType",
    "CardContainer",
    "TopCardContainer",
    "BottomCardContainer",
    "CompletionCardContainer",
    "CardManager",
]
