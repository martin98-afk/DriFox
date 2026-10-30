# -*- coding: utf-8 -*-
"""接收 Beta 更新开关（allow_beta / _pick_release 回归）。

背景：`/releases/latest` 接口恒不返回 prerelease，开启「接收 Beta 版更新」
后必须改拉 releases 列表；列表里可能混入 draft（带 token 且有权限时可见），
需跳过取第一个可发布项。
"""
from app.utils.utils import AsyncUpdateChecker

BETA = {"tag_name": "v0.6.14-beta.1", "prerelease": True, "draft": False}
STABLE = {"tag_name": "v0.6.13", "prerelease": False, "draft": False}
DRAFT = {"tag_name": "v0.6.14", "prerelease": False, "draft": True}


def test_pick_release_passthrough_dict():
    """latest 接口（非 beta 路径）单对象原样透传。"""
    assert AsyncUpdateChecker._pick_release(STABLE) is STABLE


def test_pick_release_takes_first_prerelease():
    """beta 模式列表第一项是 prerelease 也直接选中。"""
    assert AsyncUpdateChecker._pick_release([BETA, STABLE]) is BETA


def test_pick_release_skips_draft():
    """draft 不可发布，跳过取下一个。"""
    got = AsyncUpdateChecker._pick_release([DRAFT, BETA, STABLE])
    assert got is BETA


def test_pick_release_all_draft_returns_none():
    assert AsyncUpdateChecker._pick_release([DRAFT]) is None


def test_pick_release_empty_list_returns_none():
    assert AsyncUpdateChecker._pick_release([]) is None
    assert AsyncUpdateChecker._pick_release(None) is None


def test_default_allow_beta_off():
    """默认关闭：checker 构造后 allow_beta 必须为 False（latest 路径不变）。"""
    checker = AsyncUpdateChecker.__new__(AsyncUpdateChecker)
    AsyncUpdateChecker.__init__(checker)
    assert checker.allow_beta is False
