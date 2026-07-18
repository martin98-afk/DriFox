# -*- coding: utf-8 -*-
"""PluginManager ui 组件检测测试

注意（2026-07-18 測試體系整改）：
    ``UIPluginRegistry.reset()`` 会把单例本身置为 ``None``，下一次
    ``get_instance()`` 会得到全新实例。因此 reset 后必须重新
    ``get_instance()``，否则 enable/rescan 内部的 UI 加载会落到旧
    实例上，而 ``is_loaded``/``get_content_renderer`` 查询的也是
    旧实例，导致断言失败。
"""

import json


def test_ui_component_auto_detected(tmp_path):
    """包含 ui/ 目录的插件自动声明 ui 组件"""
    from app.core.plugin_manager import PluginManager

    pm = PluginManager.get_instance()
    pm.reset()

    plugin_dir = tmp_path / "plug-with-ui"
    plugin_dir.mkdir()
    (plugin_dir / ".drifox-plugin").mkdir()
    (plugin_dir / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps({"name": "plug-with-ui", "version": "1.0.0", "components": {}}), encoding="utf-8"
    )
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
    (plugin_dir / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps({"name": "no-ui", "version": "1.0.0", "components": {}}), encoding="utf-8"
    )

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


def test_enable_plugin_loads_ui(tmp_path):
    """启用插件时触发 UI 加载

    说明：``UIPluginRegistry.reset()`` 会把单例置 ``None``，所以 reset 之后
    必须重新 ``get_instance()``，否则后续断言查询的不是 enable_plugin 内部
    引用到的那个实例。
    """
    from app.core.plugin_manager import PluginManager
    from app.core.ui_plugin_registry import UIPluginRegistry

    pm = PluginManager.get_instance()
    pm.reset()
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    # reset() 会重置单例本身，重新拿一次保证后续查询命中正确实例
    reg = UIPluginRegistry.get_instance()

    # 准备插件目录
    plugin_dir = tmp_path / "plug-ui-load"
    plugin_dir.mkdir()
    (plugin_dir / ".drifox-plugin").mkdir()
    (plugin_dir / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps({"name": "plug-ui-load", "version": "1.0.0", "components": {"ui": True}}), encoding="utf-8"
    )
    (plugin_dir / "ui").mkdir()
    (plugin_dir / "ui" / "__init__.py").write_text(
        """
from app.core.ui_plugin_registry import UIPluginRegistry
def register_ui(r: UIPluginRegistry):
    r.register_content_renderer('plug-ui-load', 'hello', lambda d, c: 'world', priority=0)
""",
        encoding="utf-8",
    )

    # 准备 PluginManager 数据目录
    app_data = tmp_path / "_app"
    app_data.mkdir()
    user_dir = app_data / "plugins"
    user_dir.mkdir()
    import shutil

    target = user_dir / "plug-ui-load"
    shutil.copytree(plugin_dir, target)

    pm._app_data_dir = app_data
    # 模拟 _discover_user_plugins：直接注册到 _plugins
    info = pm._scan_one_plugin_dir(target, "user")
    assert info is not None
    pm._plugins[info.name] = info

    # 启用插件
    pm.enable_plugin("plug-ui-load")
    # UI 应已加载：必须再拿一次以确保命中 enable 内部引用的实例
    reg = UIPluginRegistry.get_instance()
    assert reg.is_loaded("plug-ui-load") is True
    assert reg.get_content_renderer("hello") is not None
    reg.reset()
    pm.reset()


def test_rescan_new_plugin_loads_ui(tmp_path):
    """rescan 发现新插件时自动加载 UI

    同上：reset() 后需重新拿单例。
    """
    from app.core.plugin_manager import PluginManager
    from app.core.ui_plugin_registry import UIPluginRegistry

    pm = PluginManager.get_instance()
    pm.reset()
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg = UIPluginRegistry.get_instance()

    # 初始化（空环境）
    app_data = tmp_path / "_app"
    app_data.mkdir()
    pm.initialize(app_data)

    # 后续添加新插件到用户目录
    user_plugins = app_data / "plugins"
    user_plugins.mkdir(exist_ok=True)
    target = user_plugins / "new-plug"
    target.mkdir()
    (target / ".drifox-plugin").mkdir()
    (target / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps({"name": "new-plug", "version": "1.0.0", "components": {"ui": True}}), encoding="utf-8"
    )
    (target / "ui").mkdir()
    (target / "ui" / "__init__.py").write_text(
        """
from app.core.ui_plugin_registry import UIPluginRegistry
def register_ui(r: UIPluginRegistry):
    r.register_content_renderer('new-plug', 'x', lambda d, c: '1', priority=0)
""",
        encoding="utf-8",
    )

    # 默认新插件是启用的 → rescan 应自动加载 UI
    result = pm.rescan()
    assert "new-plug" in [p.name for p in result["added"]]
    # UI 已加载
    reg = UIPluginRegistry.get_instance()
    assert reg.is_loaded("new-plug") is True
    assert reg.get_content_renderer("x") is not None
    reg.reset()
    pm.reset()
