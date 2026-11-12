# -*- coding: utf-8 -*-
"""system-title-generator 插件：清单契约与纯生成链。"""
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "system-title-generator"


def test_manifest_declares_system_type_and_model_field():
    manifest = json.loads(
        (PLUGIN_ROOT / ".drifox-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["type"] == "system"
    assert manifest["api_version"] == 2
    assert manifest["components"]["ui"] is True
    fields = {f["key"]: f for f in manifest["config_schema"]["fields"]}
    assert fields["title_model"]["type"] == "model"
    assert fields["title_model"]["default"] == ""
    for cid in (
        "plugin.config.read",
        "model.complete",
        "provider.list",
        "model.enumerate",
        "session.read",
        "ui.events",
        "session.title.commit",
    ):
        assert cid in manifest["capabilities"]
    assert "session.title.snapshot" not in manifest["capabilities"]
    assert "session.title.cancel" not in manifest["capabilities"]
