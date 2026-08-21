# -*- coding: utf-8 -*-
"""对话引擎槽位卡 — 设置页：查看/选择各槽位激活的引擎工厂。

- 数据源 EngineRegistry（槽位 → 工厂列表）；选择写 Settings.engine_slot_<slot>
- 选择不热替换运行中实例（重启/新窗口生效——与 registry 语义一致）
- Task 8 缩限：UI 先行；激活源过滤消费 Settings 留 TODO（见 _on_changed）
"""

from __future__ import annotations

from typing import Dict

from PyQt5.QtWidgets import QComboBox, QFrame, QHBoxLayout, QVBoxLayout
from qfluentwidgets import BodyLabel, CardWidget, SubtitleLabel

from app.plugins.contracts.dialogue_engine import ENGINE_SLOT_GATEWAY, ENGINE_SLOT_UI
from app.plugins.registries.engine_registry import EngineRegistry
from app.utils.config import Settings

# 槽位 → Settings 键（新增槽位在此登记）
_SLOT_SETTINGS_KEY = {
    ENGINE_SLOT_UI: "engine_slot_ui",
    ENGINE_SLOT_GATEWAY: "engine_slot_gateway",
}
_SLOT_LABELS = {
    ENGINE_SLOT_UI: "主对话引擎（输入框 / API）",
    ENGINE_SLOT_GATEWAY: "消息平台引擎（Gateway）",
}


class EngineSlotCard(CardWidget):
    """引擎槽位选择卡（每槽位一行：槽位名 + 内置/插件工厂下拉）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(20, 16, 20, 16)
        self._layout.setSpacing(10)

        title = SubtitleLabel("对话引擎")
        self._layout.addWidget(title)
        desc = BodyLabel("选择各入口使用的对话引擎。改动在重启或新窗口后生效。")
        desc.setWordWrap(True)
        self._layout.addWidget(desc)

        self._rows: Dict[str, QComboBox] = {}
        for slot in (ENGINE_SLOT_UI, ENGINE_SLOT_GATEWAY):
            self._rows[slot] = self._build_row(slot)
        self.refresh()

    def _build_row(self, slot: str) -> QComboBox:
        row = QFrame()
        row.setLayout(QHBoxLayout())
        row.layout().addWidget(BodyLabel(_SLOT_LABELS.get(slot, slot)))
        combo = QComboBox()
        row.layout().addWidget(combo, stretch=1)
        combo.currentIndexChanged.connect(lambda _i, s=slot: self._on_changed(s))
        self._layout.addWidget(row)
        return combo

    def refresh(self):
        reg = EngineRegistry.get_instance()
        for slot, combo in self._rows.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("内置（默认）", userData="")
            if reg.get_factory(slot) is not None:
                source = reg.get_source(slot) or slot
                combo.addItem(f"插件引擎: {source}", userData=source)
            combo.blockSignals(False)

    def _on_changed(self, slot: str):
        combo = self._rows.get(slot)
        if combo is None:
            return
        data = combo.currentData()
        settings_key = _SLOT_SETTINGS_KEY.get(slot)
        if settings_key:
            # TODO: EngineRegistry 激活源过滤消费 Settings（下版）
            item = getattr(Settings, settings_key, None)
            if item is not None:
                item.value = data or ""

    def slot_count(self) -> int:
        return len(self._rows)

    def select_factory(self, slot: str, source: str):
        combo = self._rows.get(slot)
        if combo is None:
            return
        for i in range(combo.count()):
            if combo.itemData(i) == source:
                combo.setCurrentIndex(i)
                break
