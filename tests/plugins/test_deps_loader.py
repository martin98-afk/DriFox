# -*- coding: utf-8 -*-
"""deps_loader 单元测试 — 平台声明 / deps 注入 / pip 依赖解析

设计文档：docs/superpowers/specs/2026-08-27-plugin-platform-deps-design.md
"""

import sys
from pathlib import Path

import pytest

from app.plugins import deps_loader
from app.plugins.deps_loader import (
    check_platform,
    current_platform_key,
    deps_paths,
    ensure_deps_on_path,
    missing_pip_deps,
    normalize_platforms,
    resolve_pip_deps,
)

CUR = deps_loader.CURRENT_PLATFORM


# ============================================================
# normalize_platforms / check_platform
# ============================================================


class TestCheckPlatform:
    def test_no_platforms_field_means_compatible(self):
        ok, reason = check_platform({})
        assert ok and reason == ""

    def test_empty_platforms_means_compatible(self):
        ok, _ = check_platform({"platforms": []})
        assert ok

    def test_current_platform_declared(self):
        friendly = {"win32": "windows", "linux": "linux", "darwin": "darwin"}[CUR]
        ok, _ = check_platform({"platforms": [friendly]})
        assert ok

    def test_other_platform_declared(self):
        other = {"win32": "linux", "linux": "windows", "darwin": "windows"}[CUR]
        ok, reason = check_platform({"platforms": [other]})
        assert not ok
        assert CUR in reason or "不兼容" in reason

    def test_macos_alias(self):
        # 别名 macos/macos 归一到 darwin
        keys = normalize_platforms({"platforms": ["macos", "mac", "darwin"]})
        assert keys == ["darwin"]

    def test_win32_direct_value(self):
        keys = normalize_platforms({"platforms": ["win32"]})
        assert keys == ["win32"]

    def test_unknown_platform_ignored(self):
        keys = normalize_platforms({"platforms": ["windows", "freebsd"]})
        assert keys == ["win32"]

    def test_invalid_type_ignored(self):
        keys = normalize_platforms({"platforms": "windows"})
        assert keys is None  # 非数组 → 视为未声明

    def test_normalize_dedup(self):
        keys = normalize_platforms({"platforms": ["windows", "win32", "Windows"]})
        assert keys == ["win32"]


# ============================================================
# resolve_pip_deps
# ============================================================


class TestResolvePipDeps:
    def test_no_dependencies(self):
        assert resolve_pip_deps({}) == []

    def test_default_only(self):
        m = {"dependencies": {"pip": {"default": ["httpx>=0.27"]}}}
        assert resolve_pip_deps(m, platform_key="linux") == ["httpx>=0.27"]

    def test_merge_current_platform(self):
        m = {
            "dependencies": {
                "pip": {"default": ["httpx"], "win32": ["pywin32"], "linux": ["python-prctl"]}
            }
        }
        assert resolve_pip_deps(m, platform_key="win32") == ["httpx", "pywin32"]
        assert resolve_pip_deps(m, platform_key="linux") == ["httpx", "python-prctl"]
        assert resolve_pip_deps(m, platform_key="darwin") == ["httpx"]

    def test_dedup_and_string_form(self):
        m = {"dependencies": {"pip": {"default": "httpx", "win32": ["httpx", "pywin32"]}}}
        assert resolve_pip_deps(m, platform_key="win32") == ["httpx", "pywin32"]

    def test_invalid_entries_skipped(self):
        m = {"dependencies": {"pip": {"default": [123, "", "ok"], "win32": None}}}
        assert resolve_pip_deps(m, platform_key="win32") == ["ok"]


# ============================================================
# deps_paths / ensure_deps_on_path
# ============================================================


@pytest.fixture
def plugin_tree(tmp_path: Path) -> Path:
    """构造：deps/（公共）+ deps/<platform>/（平台）插件目录。"""
    plat_dir = tmp_path / "deps" / current_platform_key()
    plat_dir.mkdir(parents=True)
    (tmp_path / "deps" / "py_common_pkg").mkdir()
    (plat_dir / "py_plat_pkg").mkdir()
    return tmp_path


class TestEnsureDeps:
    def test_paths_order_platform_first(self, plugin_tree: Path):
        paths = deps_paths(plugin_tree)
        assert len(paths) == 2
        assert paths[0].name == current_platform_key()  # 平台目录在前
        assert paths[1].name == "deps"

    def test_inject_idempotent(self, plugin_tree: Path):
        first = ensure_deps_on_path(plugin_tree)
        assert len(first) == 2
        # sys.path 中平台目录排在公共目录之前
        assert sys.path.index(str(first[0])) < sys.path.index(str(first[1]))
        second = ensure_deps_on_path(plugin_tree)
        assert second == []  # 幂等
        # 清理，避免污染其它测试
        for p in first:
            sys.path.remove(p)

    def test_no_deps_noop(self, tmp_path: Path):
        assert deps_paths(tmp_path) == []
        assert ensure_deps_on_path(tmp_path) == []

    def test_empty_deps_dir_not_injected(self, tmp_path: Path):
        (tmp_path / "deps").mkdir()
        assert deps_paths(tmp_path) == []


# ============================================================
# missing_pip_deps
# ============================================================


class TestMissingPipDeps:
    def test_all_in_deps_common(self, tmp_path: Path):
        (tmp_path / "deps" / "httpx").mkdir(parents=True)
        m = {"dependencies": {"pip": {"default": ["httpx>=0.27"]}}}
        assert missing_pip_deps(tmp_path, m) == []

    def test_in_platform_deps(self, tmp_path: Path):
        plat = tmp_path / "deps" / current_platform_key() / "pycryptodome"
        plat.mkdir(parents=True)
        m = {"dependencies": {"pip": {"default": ["pycryptodome>=3.20"]}}}
        assert missing_pip_deps(tmp_path, m) == []

    def test_missing_when_not_importable(self, tmp_path: Path, monkeypatch):
        import importlib.util

        m = {"dependencies": {"pip": {"default": ["definitely_not_installed_pkg_xyz"]}}}
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        assert missing_pip_deps(tmp_path, m) == ["definitely_not_installed_pkg_xyz"]

    def test_importable_counts_as_present(self, tmp_path: Path):
        # json 为标准库，find_spec 可找到
        m = {"dependencies": {"pip": {"default": ["json"]}}}
        assert missing_pip_deps(tmp_path, m) == []


# ============================================================
# L1：版本校验 / 注入登记与摘除　L2：区间冲突判定
# ============================================================

def _make_dist_info(deps_dir: Path, dist: str, version: str) -> None:
    """在 deps 目录下造一个带 METADATA 的 dist-info。"""
    d = deps_dir / f"{dist}-{version}.dist-info"
    d.mkdir(parents=True)
    (d / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n", encoding="utf-8"
    )


class TestDepsVersionGate:
    def test_unsatisfied_version_counts_missing(self, tmp_path: Path):
        plat = tmp_path / "deps" / current_platform_key()
        plat.mkdir(parents=True)
        _make_dist_info(plat, "httpx", "0.20.0")
        m = {"dependencies": {"pip": {"default": ["httpx>=0.28"]}}}
        assert missing_pip_deps(tmp_path, m) == ["httpx>=0.28"]

    def test_satisfied_version_ready(self, tmp_path: Path):
        plat = tmp_path / "deps" / current_platform_key()
        plat.mkdir(parents=True)
        _make_dist_info(plat, "httpx", "0.28.1")
        m = {"dependencies": {"pip": {"default": ["httpx>=0.28"]}}}
        assert missing_pip_deps(tmp_path, m) == []

    def test_platform_dir_version_wins(self, tmp_path: Path):
        """平台目录版不满足、公共目录满足 → 按平台版判缺失（实际生效的是它）"""
        plat = tmp_path / "deps" / current_platform_key()
        plat.mkdir(parents=True)
        _make_dist_info(plat, "httpx", "0.20.0")
        _make_dist_info(tmp_path / "deps", "httpx", "0.28.1")
        m = {"dependencies": {"pip": {"default": ["httpx>=0.28"]}}}
        assert missing_pip_deps(tmp_path, m) == ["httpx>=0.28"]

    def test_loose_name_without_version_still_ready(self, tmp_path: Path):
        """vendor/源码形态（无 dist-info）沿用按名判定，不因无版本号误报"""
        (tmp_path / "deps" / "httpx").mkdir(parents=True)
        m = {"dependencies": {"pip": {"default": ["httpx>=0.28"]}}}
        assert missing_pip_deps(tmp_path, m) == []


class TestRemoveDepsFromPath:
    def test_remove_is_idempotent(self, plugin_tree: Path):
        injected = ensure_deps_on_path(plugin_tree)
        assert injected
        removed = deps_loader.remove_deps_from_path(plugin_tree)
        assert sorted(removed) == sorted(injected)
        for p in injected:
            assert p not in sys.path
        assert deps_loader.remove_deps_from_path(plugin_tree) == []

    def test_remove_does_not_touch_sibling_prefix(self, tmp_path: Path):
        """foo 的摘除不能命中 foobar（前缀必须按分隔符断开）"""
        a = tmp_path / "foo"
        b = tmp_path / "foobar"
        keep: list = []
        try:
            for p in (a, b):
                (p / "deps").mkdir(parents=True)
                (p / "deps" / "m.py").write_text("", encoding="utf-8")
            ensure_deps_on_path(a)
            keep = ensure_deps_on_path(b)
            deps_loader.remove_deps_from_path(a)
            for p in keep:
                assert p in sys.path
        finally:
            for p in keep:
                while p in sys.path:
                    sys.path.remove(p)
            deps_loader.remove_deps_from_path(a)
            deps_loader.remove_deps_from_path(b)


class TestSpecConflict:
    @pytest.mark.parametrize(
        "a,b,expected",
        [
            ("pkg==1.0", "pkg==2.0", True),
            ("pkg>=2", "pkg<2", True),
            ("pkg>2", "pkg<=2", True),
            ("pkg>=3", "pkg<3", True),
            ("pkg>=2", "pkg<=2", False),  # 2 同时满足
            ("pkg>=1", "pkg<2", False),
            ("pkg>=1.0", "pkg>=2.0", False),  # 两个下界必有交集
            ("pkg", "pkg==1", False),  # 裸包名
            ("pkg>=1.0", "other<1.0", False),  # 不同包
            ("PyYAML>=6", "pyyaml<6", True),  # 名字归一后同包
        ],
    )
    def test_pairs(self, a: str, b: str, expected: bool):
        assert deps_loader.specs_conflict(a, b) is expected

    def test_find_conflicts_cross_plugin(self):
        decl = {"a": ["httpx>=0.28"], "b": ["httpx<0.24"], "c": ["httpx>=0.28.1"]}
        out = deps_loader.find_deps_conflicts(decl)
        assert ("httpx", "a", "b") in out
        assert not [pair for pair in out if {pair[1], pair[2]} == {"a", "c"}]

    def test_same_plugin_not_reported(self):
        assert deps_loader.find_deps_conflicts({"a": ["httpx==1", "httpx==2"]}) == []
