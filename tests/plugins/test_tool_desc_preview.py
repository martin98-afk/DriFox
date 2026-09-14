# -*- coding: utf-8 -*-
"""工具 description 预览拼接规则测试（plugins/system-tools/tools/_tool_desc.py）"""

import importlib.util
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parents[2] / "plugins" / "system-tools" / "tools" / "_tool_desc.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("_tool_desc_under_test", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_long_description_is_truncated_and_tail_kept():
    mod = _load_mod()
    long_desc = "抽离 token 估算函数并补上缓存与回归测试用例覆盖边界情况" * 2  # 54 字，必然超上限
    preview = mod.prefer_description(
        lambda args: "编辑文件",
        lambda args: "a.py",
    )({"description": long_desc, "path": "a.py"})
    assert preview.endswith("a.py"), "尾部路径必须保留"
    assert "…" in preview
    assert len(preview.split(" ")[0]) <= mod._DESC_PREVIEW_MAX


def test_short_description_untouched():
    mod = _load_mod()
    preview = mod.prefer_description(lambda args: "编辑文件", lambda args: "a.py")(
        {"description": "加缓存", "path": "a.py"}
    )
    assert preview == "加缓存 a.py"


def test_replacement_mode_not_truncated():
    """tail_fn=None 的替换式（bash 等）不截断"""
    mod = _load_mod()
    long_desc = "详细说明" * 20
    preview = mod.prefer_description(lambda args: "cmd")({"description": long_desc})
    assert preview == long_desc


def test_falls_back_to_preview_fn_without_description():
    mod = _load_mod()
    preview = mod.prefer_description(lambda args: "编辑文件", lambda args: "a.py")({"path": "a.py"})
    assert preview == "编辑文件"
