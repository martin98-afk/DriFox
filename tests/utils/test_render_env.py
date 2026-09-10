# -*- coding: utf-8 -*-
"""
render_env 单元测试：渲染配置 → 环境变量的换算逻辑

render_env 运行于 Qt 之前、纯 stdlib，测试直接构造临时 app.config 验证：
- 默认行为与历史 main.py 硬编码一致（无配置文件/缺 key）
- auto 档检测链（环境变量 → 标记文件）
- 显式档位覆盖检测链
- 数值越界钳制 / 非法值回退
- QTWEBENGINE_CHROMIUM_FLAGS 外部优先（setdefault 语义）
"""

import json
import os
import sys
from pathlib import Path

import pytest

from app.utils.render_env import (
    apply_render_env,
    build_chromium_flags,
    compute_settings,
    describe_applied,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离环境变量与标记文件路径，避免本机状态污染断言。"""
    for key in (
        "QT_OPENGL",
        "QT_ANGLE_PLATFORM",
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "DRIFOX_SOFTWARE_RENDER",
        "DRIFOX_ENABLE_WEBGL",
    ):
        monkeypatch.delenv(key, raising=False)
    # 检测链打桩：默认全关（硬件、无 WebGL），由各用例按需覆盖
    monkeypatch.setattr("app.utils.render_env._detect_software_render", lambda: False)
    monkeypatch.setattr("app.utils.render_env._detect_webgl_enabled", lambda: False)


def _write_config(tmp_path: Path, render: dict) -> Path:
    path = tmp_path / "app.config"
    path.write_text(json.dumps({"Render": render}), encoding="utf-8")
    return path


def _flags() -> str:
    return os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]


# ══ 默认行为：升级零变化 ══


def test_no_config_file_keeps_legacy_defaults(tmp_path, monkeypatch):
    """无配置文件 → 环境变量与历史硬编码完全一致。"""
    apply_render_env(tmp_path / "missing.config")
    assert os.environ.get("QT_OPENGL") == "angle"
    assert os.environ.get("QT_ANGLE_PLATFORM") == "d3d11"
    assert "--renderer-process-limit=6" in _flags()
    assert "--js-flags=--max-old-space-size=128" in _flags()
    assert "--enable-low-end-device-mode" in _flags()
    assert "--disable-smooth-scrolling" in _flags()
    assert "--disable-gpu" in _flags()
    assert "--disable-canvas-aa" in _flags()
    assert "--disable-features=Translate,MediaRouter,optimizeHints,CalculateNativeWinOcclusion" in _flags()


def test_corrupt_config_file_falls_back_to_defaults(tmp_path):
    """JSON 损坏 → 静默回退默认。"""
    path = tmp_path / "app.config"
    path.write_text("{not valid json", encoding="utf-8")
    apply_render_env(path)
    assert os.environ.get("QT_ANGLE_PLATFORM") == "d3d11"
    assert "--renderer-process-limit=6" in _flags()


# ══ 渲染后端档位 ══


def test_backend_auto_uses_detect_chain(tmp_path, monkeypatch):
    """auto 档 → 检测链判定软件渲染 → warp。"""
    monkeypatch.setattr("app.utils.render_env._detect_software_render", lambda: True)
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "auto"}))
    assert os.environ.get("QT_ANGLE_PLATFORM") == "warp"
    assert "--disable-gpu" in _flags()


def test_backend_software_overrides_detect_chain(tmp_path, monkeypatch):
    """显式 software 覆盖检测链（检测链返回硬件仍走软件）。"""
    monkeypatch.setattr("app.utils.render_env._detect_software_render", lambda: False)
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "software"}))
    assert os.environ.get("QT_ANGLE_PLATFORM") == "warp"
    assert "--disable-gpu" in _flags()


def test_backend_hardware_disables_swiftshader(tmp_path):
    """显式 hardware → d3d11，不禁 GPU 但禁软件光栅兜底。"""
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "hardware"}))
    assert os.environ.get("QT_ANGLE_PLATFORM") == "d3d11"
    assert "--disable-software-rasterizer" in _flags()
    assert "--disable-gpu" not in _flags()


def test_backend_software_gl_sets_qt_opengl_software(tmp_path):
    """software_gl → QT_OPENGL=software，不设 ANGLE 平台。"""
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "software_gl"}))
    assert os.environ.get("QT_OPENGL") == "software"
    assert "QT_ANGLE_PLATFORM" not in os.environ
    assert "--disable-gpu" in _flags()


def test_invalid_backend_value_falls_back_to_auto(tmp_path, monkeypatch):
    """手改非法档位 → 按 auto 处理。"""
    monkeypatch.setattr("app.utils.render_env._detect_software_render", lambda: True)
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "turbo"}))
    assert os.environ.get("QT_ANGLE_PLATFORM") == "warp"


# ══ WebGL ══


def test_webgl_on_with_software_enables_swiftshader(tmp_path):
    """软件路径 + WebGL 开 → swiftshader 兜底，不 disable-gpu。"""
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "software", "WebglEnabled": "on"}))
    assert "--enable-unsafe-swiftshader" in _flags()
    assert "--disable-gpu" not in _flags()


def test_webgl_auto_uses_detect_chain(tmp_path, monkeypatch):
    """auto 档 → 标记文件/环境变量检测链。"""
    monkeypatch.setattr("app.utils.render_env._detect_webgl_enabled", lambda: True)
    apply_render_env(_write_config(tmp_path, {}))
    assert "--enable-unsafe-swiftshader" in _flags()


def test_webgl_off_overrides_detect_chain(tmp_path, monkeypatch):
    """显式 off 覆盖检测链。"""
    monkeypatch.setattr("app.utils.render_env._detect_webgl_enabled", lambda: True)
    apply_render_env(_write_config(tmp_path, {"WebglEnabled": "off"}))
    assert "--enable-unsafe-swiftshader" not in _flags()
    assert "--disable-gpu" in _flags()


def test_webgl_on_with_hardware_keeps_gpu_path(tmp_path):
    """硬件路径本就支持 WebGL，不叠加 swiftshader。"""
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "hardware", "WebglEnabled": "on"}))
    assert "--enable-unsafe-swiftshader" not in _flags()
    assert "--disable-software-rasterizer" in _flags()


# ══ 数值钳制与非法值 ══


def test_renderer_limit_clamped_to_range(tmp_path):
    apply_render_env(_write_config(tmp_path, {"RendererProcessLimit": 999}))
    assert "--renderer-process-limit=32" in _flags()


def test_renderer_limit_invalid_type_falls_back(tmp_path):
    apply_render_env(_write_config(tmp_path, {"RendererProcessLimit": "many"}))
    assert "--renderer-process-limit=6" in _flags()


def test_js_heap_clamped(tmp_path):
    apply_render_env(_write_config(tmp_path, {"JsHeapMb": 10}))
    assert "--max-old-space-size=64" in _flags()


def test_bool_items_reject_non_bool(tmp_path):
    """布尔项遇到字符串垃圾值 → 回退默认（low_end 默认开、smooth 默认关）。"""
    apply_render_env(_write_config(tmp_path, {"LowEndDeviceMode": "yes", "SmoothScrolling": "no"}))
    assert "--enable-low-end-device-mode" in _flags()
    assert "--disable-smooth-scrolling" in _flags()


def test_smooth_scrolling_on_drops_disable_flag(tmp_path):
    apply_render_env(_write_config(tmp_path, {"SmoothScrolling": True}))
    assert "--disable-smooth-scrolling" not in _flags()


def test_canvas_aa_on_drops_disable_flags(tmp_path):
    apply_render_env(_write_config(tmp_path, {"CanvasAA": True}))
    assert "--disable-canvas-aa" not in _flags()


def test_empty_disabled_features_drops_flag(tmp_path):
    apply_render_env(_write_config(tmp_path, {"DisabledFeatures": ""}))
    assert "--disable-features=" not in _flags()


# ══ 高级 flags 与外部环境变量优先级 ══


def test_extra_flags_appended(tmp_path):
    apply_render_env(_write_config(tmp_path, {"ExtraChromiumFlags": " --foo=bar"}))
    assert _flags().endswith("--foo=bar")


def test_extra_flags_can_override_builtin(tmp_path):
    """追加的重复 flag 排在后面（Chromium 后者覆盖前者）。"""
    apply_render_env(_write_config(tmp_path, {"ExtraChromiumFlags": "--renderer-process-limit=16"}))
    flags = _flags()
    assert flags.index("--renderer-process-limit=6") < flags.rindex("--renderer-process-limit=16")


def test_external_chromium_flags_win_setdefault(tmp_path, monkeypatch):
    """外部已设 QTWEBENGINE_CHROMIUM_FLAGS → 完全尊重外部（调试逃生门）。"""
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--my-own-flag")
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "software"}))
    assert _flags() == "--my-own-flag"


def test_external_qt_opengl_wins(tmp_path, monkeypatch):
    """外部 QT_OPENGL 保持 setdefault 语义。"""
    monkeypatch.setenv("QT_OPENGL", "desktop")
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "software"}))
    assert os.environ.get("QT_OPENGL") == "desktop"


# ══ 非 Windows 平台限定 ══


@pytest.mark.skipif(sys.platform != "win32", reason="模拟 Windows 行为仅在 Windows 跑")
def test_non_windows_skips_qt_backend_env(tmp_path, monkeypatch):
    """非 Windows 不设 QT_OPENGL/QT_ANGLE_PLATFORM（macOS 强设 d3d11 黑屏教训）。"""
    monkeypatch.setattr("app.utils.render_env.os.name", "posix")
    monkeypatch.setattr("app.utils.render_env._detect_software_render", lambda: True)
    apply_render_env(_write_config(tmp_path, {"RenderBackend": "software"}))
    assert "QT_OPENGL" not in os.environ
    assert "QT_ANGLE_PLATFORM" not in os.environ
    # GPU flags 仍按软件路径生效
    assert "--disable-gpu" in _flags()


# ══ 纯函数直接验证 ══


def test_compute_settings_defaults(monkeypatch):
    s = compute_settings({})
    assert s["backend"] == "hardware"
    assert s["renderer_process_limit"] == 6
    assert s["js_heap_mb"] == 128
    assert s["low_end_device_mode"] is True
    assert s["smooth_scrolling"] is False
    assert s["canvas_aa"] is False
    assert "Translate" in s["disabled_features"]


def test_build_chromium_flags_order():
    """flags 拼接顺序：进程上限 → GPU → 通用 → JS 堆 → 行为开关 → 追加。"""
    flags = build_chromium_flags(
        {
            "software_render": False,
            "webgl_enabled": False,
            "renderer_process_limit": 6,
            "js_heap_mb": 128,
            "low_end_device_mode": True,
            "smooth_scrolling": False,
            "canvas_aa": False,
            "disabled_features": "X,Y",
            "extra_flags": "--z",
        }
    )
    assert flags == (
        "--renderer-process-limit=6"
        " --disable-software-rasterizer"
        " --disable-dev-shm-usage"
        " --disable-extensions"
        " --disable-background-networking"
        " --disable-background-timer-throttling"
        " --js-flags=--max-old-space-size=128"
        " --enable-low-end-device-mode"
        " --disable-smooth-scrolling"
        " --disable-features=X,Y"
        " --disable-canvas-aa --disable-2d-canvas-clip-aa"
        " --z"
    )


# ══ 后台渲染节流（DisableBackgroundThrottling）══


def test_background_throttling_off_by_default():
    """默认关：不追加任何节流 flag（与历史硬编码行为一致）"""
    flags = build_chromium_flags(compute_settings({}))
    assert "--disable-renderer-backgrounding" not in flags
    assert "--disable-backgrounding-occluded-windows" not in flags


def test_background_throttling_on():
    """开启：追加两个 flag（离屏卡片不再被降优先级）"""
    flags = build_chromium_flags(compute_settings({"DisableBackgroundThrottling": True}))
    assert "--disable-renderer-backgrounding" in flags
    assert "--disable-backgrounding-occluded-windows" in flags


def test_background_throttling_invalid_value_falls_back():
    """非布尔值（"yes"）一律回退默认关闭"""
    flags = build_chromium_flags(compute_settings({"DisableBackgroundThrottling": "yes"}))
    assert "--disable-renderer-backgrounding" not in flags


# ══ describe_applied（设置页「当前生效参数」回显）══


def test_describe_applied_windows_hardware():
    s = describe_applied(
        {
            "QT_OPENGL": "angle",
            "QT_ANGLE_PLATFORM": "d3d11",
            "QTWEBENGINE_CHROMIUM_FLAGS": (
                "--renderer-process-limit=8 --js-flags=--max-old-space-size=256 --disable-gpu"
            ),
        }
    )
    assert s["backend"] == "hardware"
    assert s["renderer_process_limit"] == 8
    assert s["js_heap_mb"] == 256
    assert s["flag_count"] == 3


def test_describe_applied_software_variants():
    assert describe_applied({"QT_OPENGL": "angle", "QT_ANGLE_PLATFORM": "warp"})["backend"] == "software"
    assert describe_applied({"QT_OPENGL": "software"})["backend"] == "software_gl"
    # 外部改过但不在四档内 → custom
    assert describe_applied({"QT_OPENGL": "desktop"})["backend"] == "custom"


def test_describe_applied_without_env():
    """非 Windows（未设 ANGLE）：backend 空串，数值位 0"""
    s = describe_applied({})
    assert s["backend"] == ""
    assert s["flags"] == ""
    assert (s["renderer_process_limit"], s["js_heap_mb"], s["flag_count"]) == (0, 0, 0)
