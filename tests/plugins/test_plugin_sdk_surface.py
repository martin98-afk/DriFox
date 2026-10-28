# -*- coding: utf-8 -*-
"""sdk 静态面：高频符号可导入 / 公开别名对齐 / 不暴露宿主对象。"""
import pytest

SYMBOLS = [
    "Colors",
    "font_size_css",
    "get_unified_scrollbar_style",
    "make_card_content_opaque",
    "scale_font_size",
    "Animations",
    "Shadows",
    "BorderRadius",
    "scale_icon_size",
    "get_font_family_css",
    "get_app_data_dir",
    "get_icon",
    "get_unified_font",
    "ToolResult",
    "GitWorktreeDetector",
    "ElidedLabel",
    "SquareAvatar",
    "EmptyHint",
    "SectionHeader",
    "alpha_tint",
    "SingleInputDialog",
    "ConfirmDialog",
    "get_project_color",
    "bind_theme_qss",
    "format_relative_time",
    "retarget",
    "ContainerType",
    "estimate_tokens",
    "count_messages_tokens",
    "clear_cache",
]


@pytest.mark.parametrize("name", SYMBOLS)
def test_symbol_exported(name):
    import app.plugins.sdk as sdk

    assert name in sdk.__all__
    assert getattr(sdk, name) is not None


def test_public_alias_identity():
    import app.plugins.sdk as sdk
    from app.widgets._workbench_helpers import _EmptyHint, _SectionHeader
    from app.widgets.elided_label import _ElidedLabel
    from app.widgets.square_avatar import _SquareAvatar

    assert sdk.ElidedLabel is _ElidedLabel
    assert sdk.SquareAvatar is _SquareAvatar
    assert sdk.EmptyHint is _EmptyHint
    assert sdk.SectionHeader is _SectionHeader


def test_no_host_object_in_sdk():
    import app.plugins.sdk as sdk

    for banned in ("main_widget", "backend", "tool_executor", "session_manager"):
        assert banned not in sdk.__all__


def test_sdk_import_does_not_pull_main_widget():
    """sdk 不得间接引入 main_widget（会把整个主程序拖进插件 import 期）。"""
    import subprocess
    import sys

    code = (
        "import sys; import app.plugins.sdk; "
        "assert 'app.main_widget' not in sys.modules, 'sdk 污染了 main_widget'"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="ignore"
    )
    assert r.returncode == 0, r.stderr[-800:]


def test_sdk_import_is_lazy_no_qt():
    """sdk 采用延迟导入：光 import 不得拉起 Qt 栈（否则纯值插件被迫吃全量开销）。

    实测基线：import 只 +36 模块且不碰 QtWidgets；若有人改回顶层 import，
    这里会立刻失败（历史上曾 +420 模块并连带 qfluentwidgets）。
    """
    import subprocess
    import sys

    code = (
        "import sys; n0=len(sys.modules); import app.plugins.sdk; "
        "added=len(sys.modules)-n0; "
        "qt=any('PySide6.QtWidgets' in m for m in sys.modules); "
        "qf=any('qfluentwidgets' in m for m in sys.modules); "
        "print(added, qt, qf); "
        "assert not qt and not qf, f'sdk 顶层拉起了 Qt: {qt}/{qf}'; "
        "assert added < 120, f'sdk import 过重: {added} 模块'"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="ignore"
    )
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")[-600:]


def test_sdk_pure_symbol_does_not_pull_qt():
    """取纯值符号（estimate_tokens）也不得连带 Qt。"""
    import subprocess
    import sys

    code = (
        "import sys; from app.plugins.sdk import estimate_tokens; "
        "assert not any('PySide6.QtWidgets' in m for m in sys.modules), 'estimate_tokens 拉起了 Qt'"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="ignore"
    )
    assert r.returncode == 0, r.stderr[-600:]


def test_unknown_symbol_raises_attribute_error():
    import app.plugins.sdk as sdk

    try:
        sdk.not_a_real_symbol
    except AttributeError as e:
        assert "not_a_real_symbol" in str(e)
    else:
        raise AssertionError("未知符号应抛 AttributeError")
