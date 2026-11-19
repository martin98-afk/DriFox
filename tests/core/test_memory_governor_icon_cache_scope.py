# -*- coding: utf-8 -*-
"""[E2-R] 会话清理范围回归：图标缓存不再随 _cleanup_global_lru_caches 摘除。

两处图标缓存的键空间与会话完全无关：
- app/utils/utils.py _ICON_CACHE：键=图标名，QIcon 持 _ThemeIconEngine
  （paint 时按当前主题解析），清理只会导致下轮图标重载，零收益。
- app/utils/provider_icons.py _ICON_CACHE：键含 provider 名与主题位，
  主题切换自动 miss 重解析，同样与会话无关。
其余四处（渲染/token 估算/工具块渲染/render_helpers）保持清理不动。
"""

import ast
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_GOVERNOR = PROJECT_ROOT / "app" / "core" / "infra" / "memory_governor.py"

def _cleanup_source() -> str:
    tree = ast.parse(_GOVERNOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cleanup_global_lru_caches":
            return ast.get_source_segment(_GOVERNOR.read_text(encoding="utf-8"), node) or ""
    raise AssertionError("找不到 _cleanup_global_lru_caches 函数定义")

def test_icon_caches_removed_from_session_cleanup():
    """函数体不再调用两个图标缓存 invalidate。"""
    src = _cleanup_source()
    assert "invalidate_icon_cache" not in src
    assert "invalidate_provider_icon_cache" not in src

def test_other_four_caches_kept():
    """其余四处清理保持不动（防回归误删）。"""
    src = _cleanup_source()
    assert "clear_global_render_cache" in src
    assert "estimate_tokens" in src and "cache_clear" in src
    assert "_render_tool_block_content" in src
    assert "invalidate_render_caches" in src

def test_session_switch_keeps_icon_cache_populated():
    """行为断言：触发会话清理后，已填充的图标缓存原样保留（零重载）。

    memory_governor 顶层 import sip（PyQt5 链），环境缺失时 skip；
    token 估算缓存属渲染组，预期被清，作为「清理确实执行了」的对照。
    """
    pytest.importorskip("sip")
    from importlib import import_module

    governor = import_module("app.core.infra.memory_governor")
    utils_mod = import_module("app.utils.utils")
    provider_mod = import_module("app.utils.provider_icons")

    icon = utils_mod.get_icon("copy")
    assert not icon.isNull()
    assert "copy" in utils_mod._ICON_CACHE
    provider_mod._ICON_CACHE["__probe__"] = provider_mod.QIcon()  # 探针键（未注册 provider 不入缓存，直填）

    governor._cleanup_global_lru_caches()

    assert "copy" in utils_mod._ICON_CACHE, "图标缓存不应随会话清理被摘除"
    assert "__probe__" in provider_mod._ICON_CACHE, "provider 图标缓存不应随会话清理被摘除"

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
