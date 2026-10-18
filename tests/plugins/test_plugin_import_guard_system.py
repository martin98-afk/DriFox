"""system 插件分级：type=system 的插件只禁 app.main_widget，其余放行。"""

import json
from pathlib import Path

from tools.plugin_import_guard import classify, plugin_type, scan


def _mk(root: Path, plugin: str, ptype: str = "user"):
    d = root / "plugins" / plugin / ".drifox-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"name": plugin, "type": ptype}), encoding="utf-8")


def test_user_plugin_strict():
    assert classify("app.utils.config") is False
    assert classify("app.core.store.session_store") is False
    assert classify("app.main_widget") is False
    assert classify("app.plugins.sdk") is True


def test_system_plugin_allows_internals_but_not_main_widget():
    assert classify("app.core.store.session_store", is_system=True) is True
    assert classify("app.core.conversation", is_system=True) is True
    assert classify("app.core.modelmeta.provider_profile", is_system=True) is True
    # 唯一例外：核心巨型模块仍禁
    assert classify("app.main_widget", is_system=True) is False
    assert classify("app.main_widget.sub", is_system=True) is False


def test_plugin_type_reads_manifest(tmp_path):
    _mk(tmp_path, "sys-plug", "system")
    _mk(tmp_path, "usr-plug", "user")
    assert plugin_type(tmp_path, "sys-plug") == "system"
    assert plugin_type(tmp_path, "usr-plug") == "user"
    # 无 manifest → 最严口径
    assert plugin_type(tmp_path, "ghost") == "user"
    # 解析失败 → 最严口径
    d = tmp_path / "plugins" / "bad" / ".drifox-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text("{not json", encoding="utf-8")
    assert plugin_type(tmp_path, "bad") == "user"


def test_scan_separates_system_usages(tmp_path):
    _mk(tmp_path, "sys-plug", "system")
    _mk(tmp_path, "usr-plug", "user")
    (tmp_path / "plugins" / "sys-plug" / "a.py").write_text(
        "from app.core.store.session_store import SessionStore\n", encoding="utf-8"
    )
    (tmp_path / "plugins" / "usr-plug" / "b.py").write_text(
        "from app.utils.config import Settings\n", encoding="utf-8"
    )
    report = scan(tmp_path)
    # system 的内部依赖不计入违规
    assert report.violations == {"app.utils.config": 1}
    assert report.by_plugin == {"usr-plug": {"app.utils.config": 1}}
    assert report.system_usages == {"app.core.store.session_store": 1}


def test_system_plugin_forbidden_main_widget_is_reported(tmp_path):
    _mk(tmp_path, "sys-plug", "system")
    (tmp_path / "plugins" / "sys-plug" / "a.py").write_text(
        "import app.main_widget\n", encoding="utf-8"
    )
    report = scan(tmp_path)
    # 禁用的模块对 system 插件也算违规（唯一禁止项）
    assert report.violations == {"app.main_widget": 1}
    assert report.system_usages == {}


def test_missing_type_defaults_to_user(tmp_path):
    d = tmp_path / "plugins" / "no-type" / ".drifox-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({"name": "no-type"}), encoding="utf-8")
    (tmp_path / "plugins" / "no-type" / "a.py").write_text(
        "from app.utils.config import Settings\n", encoding="utf-8"
    )
    report = scan(tmp_path)
    assert report.violations == {"app.utils.config": 1}, "缺 type 按最严口径处理"
