"""卡片系统模块 - 统一管理所有卡片组件的显示和布局"""

from enum import Enum


class ContainerType(Enum):
    TOP = "top"
    BOTTOM = "bottom"


from app.widgets.cards.card_container import (
    CardContainer,
    TopCardContainer,
    BottomCardContainer,
)
from app.widgets.cards.card_manager import CardManager

__all__ = [
    "ContainerType",
    "CardContainer",
    "TopCardContainer",
    "BottomCardContainer",
    "CardManager",
]