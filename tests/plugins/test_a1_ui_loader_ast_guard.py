# -*- coding: utf-8 -*-
"""A1：ui loader AST 安全网（T8.1 基准表 行 1）。

验证 UIPluginRegistry.load_plugin 在 exec 前拒载：
- sys.modules 污染（update / 下标赋值两形态）→ 拒载 + 日志含 [ASTGuard] + 副作用未发生
- 缺 register_ui 入口 → 拒载
- 正常 register_ui → 放行

恶意 fixture 全部 tmp_path 语义，零写入真实插件目录。
"""
import sys

import pytest
from loguru import logger

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def log_capture():
    """loguru WARNING+ 捕获为文本列表。"""
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


def _make_ui_plugin(root, body: str):
    ui_dir = root / "ui"
    ui_dir.mkdir(parents=True)
    (ui_dir / "__init__.py").write_text(body, encoding="utf-8")
    return root


def test_ui_loader_rejects_sys_modules_pollution(tmp_path, log_capture):
    """恶意样本（sys.modules.update / 下标赋值两形态）必须 exec 前拒载。"""
    reg = UIPluginRegistry()
    # 形态 1：sys.modules.update(...)
    p1 = _make_ui_plugin(tmp_path / "ui-bad-update", (
        "import sys\n"
        "import types\n"
        "sys.modules.update({'ui_canary_update': types.ModuleType('ui_canary_update')})\n"
        "def register_ui(registry):\n"
        "    pass\n"
    ))
    assert reg.load_plugin("ui-bad-update", p1) is False
    # 形态 2：sys.modules[...] = ...
    p2 = _make_ui_plugin(tmp_path / "ui-bad-subscript", (
        "import sys\n"
        "sys.modules['ui_canary_sub'] = object\n"
        "def register_ui(registry):\n"
        "    pass\n"
    ))
    assert reg.load_plugin("ui-bad-subscript", p2) is False
    # exec 前拒载：副作用未发生（canary 模块未进 sys.modules）、插件模块未注册
    assert "ui_canary_update" not in sys.modules
    assert "ui_canary_sub" not in sys.modules
    assert "ui_plugin_ui_bad_update" not in sys.modules
    assert "ui_plugin_ui_bad_subscript" not in sys.modules
    # 拒载日志含 [ASTGuard] 标记
    assert any("[ASTGuard]" in r for r in log_capture)


def test_ui_loader_rejects_missing_register_ui(tmp_path, log_capture):
    """ui/__init__.py 缺 register_ui 入口 → exec 前拒载并留痕。"""
    reg = UIPluginRegistry()
    p = _make_ui_plugin(tmp_path / "ui-no-entry", (
        "PLUGIN_LOADED = True\n"
        "def register_something_else(registry):\n"
        "    pass\n"
    ))
    assert reg.load_plugin("ui-no-entry", p) is False
    assert reg.is_loaded("ui-no-entry") is False
    assert any("[ASTGuard]" in r or "未通过 AST 安全网" in r for r in log_capture)


def test_ui_loader_allows_clean_register_ui(tmp_path, log_capture):
    """正常 register_ui 插件放行，注册成功。"""
    reg = UIPluginRegistry()
    p = _make_ui_plugin(tmp_path / "ui-guard-ok", (
        "def register_ui(registry):\n"
        "    pass\n"
    ))
    assert reg.load_plugin("ui-guard-ok", p) is True
    assert reg.is_loaded("ui-guard-ok") is True
    # 正常插件不得触发安全网拒载日志
    assert not any("[ASTGuard]" in r for r in log_capture)


def _make_manifest(root, prefixes):
    """在插件根写 .drifox-plugin/plugin.json 声明模块命名空间。"""
    import json

    mdir = root / ".drifox-plugin"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "plugin.json").write_text(
        json.dumps({"name": root.name, "module_prefixes": prefixes}), encoding="utf-8"
    )
    return root


def test_ui_loader_allows_declared_module_prefixes(tmp_path, log_capture):
    """声明式放行：plugin.json 声明的命名空间内注册自有模块 → 放行且副作用发生。

    覆盖存量写法（assistant_hub 的 assistant_hub_manager：ui 与 hooks 共享同一
    模块对象），声明同时供 PluginHostService._purge_module_prefixes 热重载清理。
    """
    reg = UIPluginRegistry()
    p = _make_manifest(tmp_path / "ui-declared", ["ui_declared_owned.", "ui_declared_mgr"])
    _make_ui_plugin(p, (
        "import sys\n"
        "import types\n"
        "sys.modules['ui_declared_mgr'] = types.ModuleType('ui_declared_mgr')\n"
        "sys.modules.update({'ui_declared_owned.sub': types.ModuleType('ui_declared_owned.sub')})\n"
        "def register_ui(registry):\n"
        "    pass\n"
    ))
    try:
        assert reg.load_plugin("ui-declared", p) is True
        assert "ui_declared_mgr" in sys.modules
        assert "ui_declared_owned.sub" in sys.modules
        assert not any("[ASTGuard] 拒绝" in r for r in log_capture)
    finally:
        sys.modules.pop("ui_declared_mgr", None)
        sys.modules.pop("ui_declared_owned.sub", None)


def test_ui_loader_rejects_declared_prefix_shadowing_host(tmp_path, log_capture):
    """声明也不能用来遮蔽真实模块：声明 app. 前缀并覆盖 app.* → 仍拒载。"""
    reg = UIPluginRegistry()
    p = _make_manifest(tmp_path / "ui-shadow", ["app."])
    _make_ui_plugin(p, (
        "import sys\n"
        "import types\n"
        "sys.modules['app.canary_shadow'] = types.ModuleType('app.canary_shadow')\n"
        "def register_ui(registry):\n"
        "    pass\n"
    ))
    assert reg.load_plugin("ui-shadow", p) is False
    assert "app.canary_shadow" not in sys.modules
    assert any("[ASTGuard]" in r and "遮蔽" in r for r in log_capture)


def test_ui_loader_rejects_dynamic_sys_modules_key(tmp_path, log_capture):
    """键为动态表达式（无法静态确认落点）→ 即便有声明也拒载。"""
    reg = UIPluginRegistry()
    p = _make_manifest(tmp_path / "ui-dyn", ["ui_dyn_owned."])
    _make_ui_plugin(p, (
        "import sys\n"
        "def _name():\n"
        "    return 'ui_dyn_owned.x'\n"
        "sys.modules[_name()] = object\n"
        "def register_ui(registry):\n"
        "    pass\n"
    ))
    assert reg.load_plugin("ui-dyn", p) is False
    assert any("[ASTGuard]" in r and "动态" in r for r in log_capture)


def test_read_manifest_module_prefixes_missing_manifest(tmp_path):
    """无清单 / 缺字段 → 空表（未声明即不可写，最严口径）。"""
    from app.plugins.contracts.manifest_schema import read_manifest_module_prefixes

    assert read_manifest_module_prefixes(tmp_path / "nope") == []
    _make_ui_plugin(tmp_path / "ui-nodecl", "def register_ui(registry):\n    pass\n")
    assert read_manifest_module_prefixes(tmp_path / "ui-nodecl") == []
