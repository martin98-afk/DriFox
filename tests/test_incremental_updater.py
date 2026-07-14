# -*- coding: utf-8 -*-
"""增量更新引擎 单元测试。

测试范围：
    - 版本号解析与跨度保护
    - manifest 对比与差异计算
    - 路径安全过滤
    - zip 前缀检测
"""

import json
import os
import tempfile
import zipfile

import pytest

from app.utils.incremental_updater import DiffReport, IncrementalUpdater


# ============================================================================
# Fixtures
# ============================================================================


def _make_updater() -> IncrementalUpdater:
    """创建一个未启动的 IncrementalUpdater 实例（纯逻辑测试用）。"""
    return IncrementalUpdater()


def _make_manifest(files: dict[str, str]) -> dict:
    """快速构造 manifest dict。

    Args:
        files: {path: sha256_hex}，size 自动置 0。
    """
    return {
        "version": "v1.0.0",
        "build_time": "2026-01-01T00:00:00Z",
        "total_size": 0,
        "file_count": len(files),
        "files": {
            path: {"sha256": sha, "size": 0} for path, sha in files.items()
        },
    }


# ============================================================================
# 版本号解析与跨度保护
# ============================================================================


class TestVersionParsing:
    """测试 _parse_version 和 _check_version_span。"""

    def test_parse_normal(self):
        assert IncrementalUpdater._parse_version("v0.3.8") == (0, 3, 8)

    def test_parse_no_v_prefix(self):
        assert IncrementalUpdater._parse_version("1.2.3") == (1, 2, 3)

    def test_parse_with_suffix(self):
        assert IncrementalUpdater._parse_version("v0.3.8-beta") == (0, 3, 8)

    def test_parse_invalid(self):
        assert IncrementalUpdater._parse_version("garbage") == (0, 0, 0)
        assert IncrementalUpdater._parse_version("") == (0, 0, 0)

    def test_span_same_version(self):
        updater = _make_updater()
        local = {"version": "v0.3.8"}
        remote = {"version": "v0.3.8"}
        assert updater._check_version_span(local, remote) is True

    def test_span_one_minor(self):
        updater = _make_updater()
        local = {"version": "v0.3.8"}
        remote = {"version": "v0.5.0"}
        # minor: 3 → 5 = span 2, < 3 → OK
        assert updater._check_version_span(local, remote) is True

    def test_span_three_minor_force_full(self):
        updater = _make_updater()
        local = {"version": "v0.3.8"}
        remote = {"version": "v0.6.0"}
        # minor: 3 → 6 = span 3, >= 3 → 全量
        assert updater._check_version_span(local, remote) is False

    def test_span_major_change_force_full(self):
        updater = _make_updater()
        local = {"version": "v0.3.8"}
        remote = {"version": "v1.0.0"}
        assert updater._check_version_span(local, remote) is False

    def test_span_first_install_allows_incremental(self):
        updater = _make_updater()
        local = {"version": "local"}  # 首次安装
        remote = {"version": "v3.0.0"}
        assert updater._check_version_span(local, remote) is True


# ============================================================================
# 路径安全过滤
# ============================================================================


class TestPathSanitization:
    """测试 _sanitize_path。"""

    def test_normal_path(self):
        assert IncrementalUpdater._sanitize_path("_internal/app/utils.pyc") == "_internal/app/utils.pyc"

    def test_absolute_path_rejected(self):
        assert IncrementalUpdater._sanitize_path("/etc/passwd") is None
        assert IncrementalUpdater._sanitize_path("C:\\Windows\\system32") is None

    def test_parent_traversal_rejected(self):
        assert IncrementalUpdater._sanitize_path("../secret.txt") is None
        assert IncrementalUpdater._sanitize_path("../../outside") is None

    def test_mid_path_traversal_rejected(self):
        # normpath 会把 app/../outside → outside，然后以 .. 开头被拒绝
        result = IncrementalUpdater._sanitize_path("app/../outside")
        assert result is None


# ============================================================================
# Manifest 对比
# ============================================================================


class TestCompareManifests:
    """测试 _compare_manifests。"""

    def test_empty_both(self):
        updater = _make_updater()
        diff = updater._compare_manifests(
            _make_manifest({}), _make_manifest({})
        )
        assert diff.is_empty
        assert diff.total_files == 0

    def test_added_files(self):
        updater = _make_updater()
        local = _make_manifest({"a.pyc": "aaa"})
        remote = _make_manifest({"a.pyc": "aaa", "b.pyc": "bbb"})
        diff = updater._compare_manifests(local, remote)
        assert diff.added == ["b.pyc"]
        assert diff.modified == []
        assert diff.total_files == 1

    def test_modified_files(self):
        updater = _make_updater()
        local = _make_manifest({"a.pyc": "aaa", "b.pyc": "bbb_old"})
        remote = _make_manifest({"a.pyc": "aaa", "b.pyc": "bbb_new"})
        diff = updater._compare_manifests(local, remote)
        assert diff.modified == ["b.pyc"]
        assert diff.added == []
        assert diff.total_files == 1

    def test_deleted_files(self):
        updater = _make_updater()
        local = _make_manifest({"a.pyc": "aaa", "old.pyc": "old"})
        remote = _make_manifest({"a.pyc": "aaa"})
        diff = updater._compare_manifests(local, remote)
        assert diff.deleted == ["old.pyc"]
        assert diff.total_files == 0

    def test_mixed_diff(self):
        updater = _make_updater()
        local = _make_manifest({
            "keep.pyc": "k",
            "mod.pyc": "m1",
            "del.pyc": "d",
        })
        remote = _make_manifest({
            "keep.pyc": "k",
            "mod.pyc": "m2",
            "add.pyc": "a",
        })
        diff = updater._compare_manifests(local, remote)
        assert diff.added == ["add.pyc"]
        assert diff.modified == ["mod.pyc"]
        assert diff.deleted == ["del.pyc"]
        assert diff.total_files == 2

    def test_malicious_paths_skipped(self):
        """不安全路径应在对比阶段被过滤掉。"""
        updater = _make_updater()
        local = _make_manifest({"safe.pyc": "s"})
        remote = _make_manifest({
            "safe.pyc": "s",
            "../../evil.pyc": "evil",
            "/abs/path.pyc": "bad",
        })
        diff = updater._compare_manifests(local, remote)
        # 恶意路径被跳过 → 差异为空
        assert diff.is_empty


# ============================================================================
# Zip 前缀检测
# ============================================================================


class TestDetectZipPrefix:
    """测试 _detect_zip_prefix。"""

    def test_with_prefix(self):
        """zip 内文件有统一顶层目录前缀。"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            zip_path = f.name
        try:
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("Drifox/Drifox.exe", "exe")
                zf.writestr("Drifox/_internal/python.exe", "py")
                zf.writestr("Drifox/_internal/app/core.pyc", "core")

            with zipfile.ZipFile(zip_path, "r") as zf:
                prefix = IncrementalUpdater._detect_zip_prefix(zf)
                assert prefix == "Drifox/"
        finally:
            os.remove(zip_path)

    def test_no_prefix(self):
        """zip 内文件无统一前缀（扁平结构）。"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            zip_path = f.name
        try:
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("file_a.txt", "a")
                zf.writestr("sub/file_b.txt", "b")

            with zipfile.ZipFile(zip_path, "r") as zf:
                prefix = IncrementalUpdater._detect_zip_prefix(zf)
                # 第一个是 file_a.txt，第一级是 "file_a.txt" 本身
                # split 后 parts 长度为 1，所以返回 ""
                assert prefix == ""
        finally:
            os.remove(zip_path)


# ============================================================================
# DiffReport 数据结构
# ============================================================================


class TestDiffReport:
    """测试 DiffReport 数据类。"""

    def test_empty_report(self):
        d = DiffReport()
        assert d.is_empty
        assert d.total_files == 0

    def test_non_empty(self):
        d = DiffReport(added=["a"], modified=["b"], total_download_size=100)
        assert not d.is_empty
        assert d.total_files == 2
