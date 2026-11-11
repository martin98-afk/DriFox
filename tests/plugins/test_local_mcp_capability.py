# -*- coding: utf-8 -*-
"""mcp.register 注册型能力单测。

Run: pytest tests/plugins/test_local_mcp_capability.py -v
"""
from unittest.mock import MagicMock

from app.plugins.capabilities import catalog
from app.plugins.capabilities.providers import mcp_core
from app.plugins.contracts.capabilities import PoolContext


def test_catalog_has_mcp_register():
    spec = catalog.CATALOG.get("mcp.register")
    assert spec is not None
    assert spec.group == "mcp"
    assert "ui" in spec.surfaces


def test_provider_returns_register_closure_and_records_plugin():
    reg = MagicMock()
    mcp_core.register(reg)
    assert reg.register.call_count == 1
    assert reg.register.call_args.args[0] == "mcp.register"
    provider = reg.register.call_args.args[1]
    closure = provider(None, PoolContext("browser", "ui", reg))
    assert closure("browser", lambda req: {"text": "x"}, tools=[]) is True
