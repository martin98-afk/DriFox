# -*- coding: utf-8 -*-
"""标题能力池：事件常量 / catalog 登记 / commit provider 契约。"""


def test_title_event_constant():
    from app.core.infra.ui_event_bus import EV_TITLE_REQUEST

    assert EV_TITLE_REQUEST == "title.request"


def test_only_one_title_capability_registered():
    from app.plugins.capabilities.catalog import CATALOG

    assert "session.title.commit" in CATALOG
    assert CATALOG["session.title.commit"].group == "session"
    assert "ui" in CATALOG["session.title.commit"].surfaces
    assert CATALOG["session.title.commit"].value_style == "factory"
    assert "session.title.snapshot" not in CATALOG
    assert "session.title.cancel" not in CATALOG
