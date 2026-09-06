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
