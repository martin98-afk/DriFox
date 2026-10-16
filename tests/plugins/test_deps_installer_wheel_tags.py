# -*- coding: utf-8 -*-
"""DepsInstaller wheel tag 候选集回归测试（平台裁剪 + universal2 覆盖）。

背景缺陷（2026-10 修）：
- _platform_wheel_tags 无条件放入 win_{arch}，Intel Mac 上 win_amd64 排第 2 位、
  压过 macosx/manylinux，装回 Windows 原生模块（.pyd）。实例：gateway-feishu 的
  pycryptodome 装成 win_amd64，darwin 上 import 时
  `Cannot load native module 'Crypto.Util._cpuid_c'` → 插件静默不工作。
- 只写 macosx_11_0_{arch}，漏掉 PyPI 上常见的 macosx_10_9_universal2
  （pycryptodome / charset-normalizer 等均以此形态分发）→ 依赖装不上。

覆盖口径：win32 / linux / darwin × x86_64 / arm64 六组合，断言
  1. 候选集不含他平台 OS tag（跨平台串台防护）
  2. 候选集含当前平台原生 tag
  3. 通用 tag（py3-none-any / abi3）恒在
  4. 真实 PyPI wheel 文件名在本平台的匹配结果符合预期
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))
if "pm_ui" not in sys.modules:
    import types

    _pkg = types.ModuleType("pm_ui")
    _pkg.__path__ = [str(PLUGIN_MARKETPLACE / "ui")]
    _pkg.__package__ = "pm_ui"
    sys.modules["pm_ui"] = _pkg

from pm_ui import deps_installer  # noqa: E402

# 平台 × 架构矩阵（monkeypatch sys.platform / platform.machine）
_PLATFORMS = ["win32", "darwin", "linux"]
_ARCHES = ["x86_64", "arm64"]


def _tags_for(monkeypatch, platform: str, machine: str):
    """在指定 (sys.platform, platform.machine()) 下取 tag 候选集。

    _platform_wheel_tags 内部 `import platform as _pf` 后调 _pf.machine()，
    故须 patch platform.machine（而非某模块属性）才能穿透。
    """
    import platform as _pf

    monkeypatch.setattr(deps_installer.sys, "platform", platform)
    monkeypatch.setattr(_pf, "machine", lambda: machine)
    return deps_installer._platform_wheel_tags()


# ── 1. 跨平台串台防护：候选集不含他平台 OS tag ─────────────────
@pytest.mark.parametrize("platform", _PLATFORMS)
@pytest.mark.parametrize("machine", _ARCHES)
def test_no_foreign_os_tags(monkeypatch, platform, machine):
    """非当前 OS 的原生 tag 一律不得出现在候选集中。"""
    tags = _tags_for(monkeypatch, platform, machine)
    foreign = {
        "win32": "win_",
        "darwin": "macosx_",
        "linux": ("manylinux", "linux_"),
    }[{"win32": "darwin", "darwin": "linux", "linux": "win32"}[platform]]
    if isinstance(foreign, tuple):
        assert not any(t.startswith(foreign) for t in tags), f"{platform}/{machine} 含 linux tag"
    else:
        assert not any(t.startswith(foreign) for t in tags), f"{platform}/{machine} 含 {foreign} tag"


# ── 2. 当前平台原生 tag 必须在场 ────────────────────────────────
@pytest.mark.parametrize("machine", _ARCHES)
def test_win32_has_win_tag(monkeypatch, machine):
    tags = _tags_for(monkeypatch, "win32", machine)
    assert any(t.startswith("win_") for t in tags)


@pytest.mark.parametrize("machine", _ARCHES)
def test_darwin_has_macosx_tag(monkeypatch, machine):
    tags = _tags_for(monkeypatch, "darwin", machine)
    assert any(t.startswith("macosx_") for t in tags)


@pytest.mark.parametrize("machine", _ARCHES)
def test_linux_has_manylinux_tag(monkeypatch, machine):
    tags = _tags_for(monkeypatch, "linux", machine)
    assert any(t.startswith("manylinux") for t in tags)


# ── 3. 通用 tag 恒在（跨 OS 可用形态） ─────────────────────────
@pytest.mark.parametrize("platform", _PLATFORMS)
@pytest.mark.parametrize("machine", _ARCHES)
def test_universal_tags_always_present(monkeypatch, platform, machine):
    """py3-none-any 与 abi3 与 OS 无关，任何平台都应可命中。"""
    tags = _tags_for(monkeypatch, platform, machine)
    assert "py3-none-any" in tags
    assert any(t.endswith("-abi3") for t in tags)


# ── 4. darwin 必须覆盖 universal2（PyPI 主流分发形态） ──────────
@pytest.mark.parametrize("machine", _ARCHES)
def test_darwin_covers_universal2(monkeypatch, machine):
    """pycryptodome / charset-normalizer 等以 universal2 分发，漏配即装不上。"""
    tags = _tags_for(monkeypatch, "darwin", machine)
    assert "macosx_10_9_universal2" in tags


# ── 5. 真实 PyPI wheel 文件名 → 本平台匹配结果 ─────────────────
# pycryptodome 3.23.0 在 PyPI 上的三种平台形态
_PYCRYPTODOME_WHEELS = {
    "win_amd64": "pycryptodome-3.23.0-cp37-abi3-win_amd64.whl",
    "macosx_10_9_universal2": "pycryptodome-3.23.0-cp37-abi3-macosx_10_9_universal2.whl",
    "manylinux_2_17_x86_64.manylinux2014_x86_64": (
        "pycryptodome-3.23.0-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    ),
}


def _rank(tags, filename: str):
    tag = filename[:-4].split("-")[-1]
    return next((i for i, p in enumerate(tags) if tag.startswith(p)), None)


@pytest.mark.parametrize(
    "platform,expect_hit,expect_miss",
    [
        ("darwin", "macosx_10_9_universal2", "win_amd64"),
        ("win32", "win_amd64", "macosx_10_9_universal2"),
    ],
)
def test_pycryptodome_wheel_picks_native_platform(monkeypatch, platform, expect_hit, expect_miss):
    """核心回归：本平台原生 wheel 命中，Windows wheel 在非 Windows 上不得命中。"""
    machine = "x86_64" if platform == "win32" else "arm64"
    tags = _tags_for(monkeypatch, platform, machine)
    assert _rank(tags, _PYCRYPTODOME_WHEELS[expect_hit]) is not None
    assert _rank(tags, _PYCRYPTODOME_WHEELS[expect_miss]) is None


def test_darwin_arm64_matches_universal2_pycryptodome(monkeypatch):
    """原始故障场景：darwin/arm64 必须能装上 pycryptodome（universal2 含 arm64）。"""
    tags = _tags_for(monkeypatch, "darwin", "arm64")
    target = _PYCRYPTODOME_WHEELS["macosx_10_9_universal2"]
    rank = _rank(tags, target)
    assert rank is not None, "darwin/arm64 匹配不到 universal2 wheel，依赖将装不上"
    # universal2 应排在其它 macosx 单架构 tag 之前（更通用的形态优先）
    other_macosx_ranks = [
        i
        for i, t in enumerate(tags)
        if t.startswith("macosx_") and t != "macosx_10_9_universal2"
    ]
    assert all(rank < r for r in other_macosx_ranks)
