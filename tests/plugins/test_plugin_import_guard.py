# -*- coding: utf-8 -*-
"""插件 import 守卫：白名单判定 / 聚合报告 / --strict 退出码。"""
from pathlib import Path


def test_allowed_and_denied():
    from tools.plugin_import_guard import classify

    assert classify("app.plugins.sdk") is True
    assert classify("app.plugins.contracts.capabilities") is True
    assert classify("app.plugins.registries.ui_plugin_registry") is True
    assert classify("app.plugins.managers.plugin_config_store") is True
    assert classify("app.utils.design_tokens") is False
    assert classify("app.main_widget") is False
    assert classify("app.core.conversation.backend") is False
    assert classify("os.path") is True  # 非 app.* 不属本守卫范围


def test_scan_reports_and_aggregates(tmp_path):
    from tools.plugin_import_guard import scan

    plug = tmp_path / "plugins" / "demo"
    (plug / "ui").mkdir(parents=True)
    (plug / "ui" / "__init__.py").write_text(
        "from app.utils.design_tokens import Colors\n"
        "from app.plugins.sdk import ToolResult\n"
        "import app.main_widget\n",
        encoding="utf-8",
    )
    report = scan(tmp_path)
    assert report.violations["app.utils.design_tokens"] == 1
    assert report.violations["app.main_widget"] == 1
    assert "app.plugins.sdk" not in report.violations
    assert "demo" in report.by_plugin


def test_submodule_counts_once_per_statement(tmp_path):
    """`from app.utils.utils import get_icon, get_app_data_dir` 只算 1 次（模块级去重）。"""
    from tools.plugin_import_guard import scan

    plug = tmp_path / "plugins" / "demo"
    plug.mkdir(parents=True)
    (plug / "x.py").write_text(
        "from app.utils.utils import get_icon, get_app_data_dir\n", encoding="utf-8"
    )
    report = scan(tmp_path)
    assert report.violations["app.utils.utils"] == 1


def test_skip_dirs(tmp_path):
    """deps / skills / examples / __pycache__ 不扫（第三方与示例代码不算违规）。"""
    from tools.plugin_import_guard import scan

    for d in ("deps", "skills", "examples"):
        p = tmp_path / "plugins" / "demo" / d
        p.mkdir(parents=True)
        (p / "m.py").write_text("import app.main_widget\n", encoding="utf-8")
    report = scan(tmp_path)
    assert report.violations == {}


def test_syntax_error_skipped(tmp_path):
    from tools.plugin_import_guard import scan

    plug = tmp_path / "plugins" / "demo"
    plug.mkdir(parents=True)
    (plug / "bad.py").write_text("def (: pass\n", encoding="utf-8")
    report = scan(tmp_path)
    assert report.violations == {}


def test_main_exit_code(tmp_path, capsys):
    from tools import plugin_import_guard as guard

    plug = tmp_path / "plugins" / "demo"
    plug.mkdir(parents=True)
    (plug / "x.py").write_text("import app.main_widget\n", encoding="utf-8")
    assert guard.main(["--root", str(tmp_path)]) == 0
    assert guard.main(["--root", str(tmp_path), "--strict"]) == 1


def test_main_clean_repo(tmp_path, capsys):
    from tools import plugin_import_guard as guard

    (tmp_path / "plugins").mkdir()
    assert guard.main(["--root", str(tmp_path), "--strict"]) == 0
    out = capsys.readouterr().out
    assert "未发现越界 import" in out
