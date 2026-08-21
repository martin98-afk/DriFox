# -*- coding: utf-8 -*-
"""引擎槽位选择卡：读 EngineRegistry 槽位/工厂，选择持久化到 Settings

EP1 后续 Task 8 — 缩限：仅展示与持久化选择；激活源过滤消费逻辑留 TODO
（避免一次性改 registry 选择语义引入回归风险）。
"""

import pytest

pytest.importorskip("PyQt5")


def test_card_lists_slots_and_persists_choice(qtbot, monkeypatch, tmp_path):
    """卡片渲染槽位行 + 选择 → Settings 写入"""
    from app.plugins.registries.engine_registry import EngineRegistry
    from app.widgets.cards.settings.engine_slot_card import EngineSlotCard

    reg = EngineRegistry()

    class _F:
        def __init__(self, slot, cls):
            self.id = slot
            self._cls = cls

        def create(self, **kw):
            return self._cls(**kw)

        @property
        def label(self):
            return f"demo-{self.id}"

    reg.register(_F("ui", type("E1", (), {})), source="plugin:demo")

    monkeypatch.setattr(EngineRegistry, "get_instance", staticmethod(lambda: reg))
    card = EngineSlotCard()
    qtbot.addWidget(card)

    # 槽位行渲染：内置 + demo 两个选项
    assert card.slot_count() >= 1
    # 选择 demo → Settings 写入
    card.select_factory("ui", "plugin:demo")
    from app.utils.config import Settings

    assert Settings.get_instance().engine_slot_ui.value == "plugin:demo"
