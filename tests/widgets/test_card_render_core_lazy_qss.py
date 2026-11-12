# -*- coding: utf-8 -*-
"""card_render_core 惰性 QSS 回归（#24 L1）

_PLACEHOLDER_QSS 原为模块级 f-string，import 期级联 scale_font_size /
get_font_family_css → design_tokens → config.Settings → 主题校验 → theme_manager
70 主题 YAML 全量解析。改 @lru_cache(maxsize=1) 惰性函数 _placeholder_qss()。

断言：
1. 返回值与原表达式逐字等价
2. 缓存只算一次（lru_cache hits + 同对象）
3. 模块级不再有 _PLACEHOLDER_QSS 立即求值（AST 守卫）
"""

import ast
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtGui")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "app" / "widgets" / "card_render_core.py"


def test_placeholder_qss_matches_original_expression():
    """惰性函数返回值与原模块级 f-string 表达式逐字等价"""
    from app.utils.design_tokens import scale_font_size
    from app.utils.utils import get_font_family_css
    from app.widgets.card_render_core import _placeholder_qss

    expected = f"color: #888888; font-size: {scale_font_size(14)}px; padding: 8px; {get_font_family_css()}"
    assert _placeholder_qss() == expected
    assert _placeholder_qss().startswith("color: #888888; font-size: ")
    assert "padding: 8px;" in _placeholder_qss()


def test_placeholder_qss_cached_single_evaluation():
    """缓存只算一次：两次调用命中 lru_cache（hits ≥1）且返回同一对象"""
    from app.widgets.card_render_core import _placeholder_qss

    _placeholder_qss.cache_clear()
    a = _placeholder_qss()
    info_after_first = _placeholder_qss.cache_info()
    b = _placeholder_qss()
    info_after_second = _placeholder_qss.cache_info()

    assert info_after_first.misses == 1, "首次调用应真实计算（misses=1）"
    assert info_after_second.hits >= 1, "第二次调用必须命中缓存"
    assert a is b, "缓存命中应返回同一对象（零重复求值）"


def test_module_level_no_eager_placeholder_qss():
    """AST 守卫：模块级不得再有 _PLACEHOLDER_QSS 立即求值赋值（防回退）"""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"), filename=str(_SRC))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_PLACEHOLDER_QSS" in targets:
                raise AssertionError("_PLACEHOLDER_QSS 回退为模块级立即求值（启动优化回归）")


def test_module_has_no_other_eager_theme_chain_calls():
    """同文件扫描：模块级（零缩进）不得残留 scale_font_size/get_font_family_css
    直接调用表达式（_CODE_FONT_SIZE 例外已另案回报——它是 from-import 值绑定
    语义，惰性化需连带 message_card/card_viewers，超本任务范围）"""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"), filename=str(_SRC))
    offenders = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            src_text = ast.unparse(node)
            if "scale_font_size(" in src_text or "get_font_family_css(" in src_text:
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                offenders.append(f"{names} = ...")
    # 当前已知例外：_CODE_FONT_SIZE（另案）。断言除它之外零残留。
    offenders = [o for o in offenders if "_CODE_FONT_SIZE" not in o]
    assert not offenders, f"模块级残留主题链立即求值: {offenders}"


# ═══════════════════════════════════════════════════════════════
# #25：_CODE_FONT_SIZE 跨文件惰性化（字号切换链语义等价）
# ═══════════════════════════════════════════════════════════════

def test_code_font_size_lazy_and_switch_semantics(monkeypatch):
    """切换链等价：首调装配 → refresh 失效 → 下次调用取新值（原 global 重绑定语义）"""
    import app.widgets.card_render_core as crc

    crc._code_font_size.cache_clear()

    # 首调：真实 scale_font_size（真实装配链走一遍）
    first = crc._code_font_size()
    assert isinstance(first, int) and first > 0

    # 模拟用户切字号档位：scale_font_size 返回值变化
    monkeypatch.setattr(
        "app.widgets.card_render_core.scale_font_size", lambda v: v * 2
    )
    # 未失效时持旧值（lru 语义，与原模块级常量「不刷新则不更新」一致）
    assert crc._code_font_size() == first

    # 字号切换链：refresh 失效 → 新值立即可读（等价原 global 重绑定）
    # monkeypatch 形态：scale_font_size(v)=v*2，基线 13 → 新档位 26
    crc._refresh_code_font_size()
    assert crc._code_font_size() == 13 * 2, "切换后必须取新档位"

    # 清场：恢复真实值
    crc._code_font_size.cache_clear()


def test_message_card_and_viewers_no_snapshot_leftover():
    """import 期快照残留守卫：三文件不得再把字号值存进模块级常量/from-import"""
    for rel in (
        "app/widgets/card_render_core.py",
        "app/widgets/message_card.py",
        "app/widgets/card_viewers.py",
    ):
        tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            # 模块/类/函数内的重绑定赋值全部禁止（值快照残留）
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "_CODE_FONT_SIZE" in targets:
                    raise AssertionError(f"{rel} 残留 _CODE_FONT_SIZE 值快照（L1 回归）")
            # from-import 快照
            if isinstance(node, ast.ImportFrom) and node.module == "app.widgets.card_render_core":
                for alias in node.names:
                    assert alias.name != "_CODE_FONT_SIZE", f"{rel} 残留 from-import 值快照"


def test_refresh_used_at_theme_switch_sites():
    """主题/字号切换三处必须调 _refresh_code_font_size（切换链完整性）"""
    for rel, expect_count in (
        ("app/widgets/message_card.py", 1),   # refresh_theme
        ("app/widgets/card_viewers.py", 2),  # 主题刷新 + RenderCtx 构建前
    ):
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        count = src.count("_refresh_code_font_size()")
        assert count == expect_count, f"{rel} 切换链刷新点数不符: {count} != {expect_count}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
