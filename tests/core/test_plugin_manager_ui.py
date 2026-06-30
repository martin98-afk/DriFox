# -*- coding: utf-8 -*-
"""PluginManager ui 组件检测测试"""
import json


def test_ui_component_auto_detected(tmp_path):
    """包含 ui/ 目录的插件自动声明 ui 组件"""
    from app.core.plugin_manager import PluginManager

    pm = PluginManager.get_instance()
    pm.reset()

    plugin_dir = tmp_path / "plug-with-ui"
    plugin_dir.mkdir()
    (plugin_dir / ".drifox-plugin").mkdir()
    (plugin_dir / ".drifox-plugin" / "plugin.json").write_text(json.dumps({
        "name": "plug-with-ui", "version": "1.0.0", "components": {}
    }), encoding="utf-8")
    (plugin_dir / "ui").mkdir()
    (plugin_dir / "ui" / "__init__.py").write_text("# empty", encoding="utf-8")

    info = pm._scan_one_plugin_dir(plugin_dir, "user")
    assert info is not None
    assert info.has_component("ui") is True
    pm.reset()


def test_ui_component_not_detected_without_dir():
    """没有 ui/ 目录则不声明 ui 组件"""
    from app.core.plugin_manager import PluginManager

    pm = PluginManager.get_instance()
    pm.reset()

    # 不创建 ui/ 目录
    plugin_dir = tmp_path_fixture() / "_plug_no_ui"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / ".drifox-plugin").mkdir(exist_ok=True)
    (plugin_dir / ".drifox-plugin" / "plugin.json").write_text(json.dumps({
        "name": "no-ui", "version": "1.0.0", "components": {}
    }), encoding="utf-8")

    info = pm._scan_one_plugin_dir(plugin_dir, "user")
    if info is not None:
        assert info.has_component("ui") is False
    pm.reset()


def tmp_path_fixture():
    """简单 tmp 路径创建（避免依赖 pytest fixture）"""
    import tempfile
    d = tempfile.mkdtemp(prefix="_plug_no_ui_")
    from pathlib import Path
    return Path(d)
