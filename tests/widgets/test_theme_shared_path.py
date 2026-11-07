# -*- coding: utf-8 -*-
"""主题资源路径解析：../ 跨主题共享、绝对路径、qrc 前缀、不存在回落

_resolve_theme_image 是 TabManagerWindow 的静态方法，依赖 theme_manager
单例的 get_theme_dir()。测试通过 monkeypatch 定点注入主题目录，不启动窗口。
"""
from pathlib import Path

import pytest


def _make_theme_dir(tmp_path: Path) -> Path:
    """构造 <root>/themes/demo/ 与 <root>/themes/shared/rain.mp3"""
    theme_dir = tmp_path / "themes" / "demo"
    shared = tmp_path / "themes" / "shared"
    theme_dir.mkdir(parents=True)
    shared.mkdir(parents=True)
    (shared / "rain.mp3").write_bytes(b"fake")
    (theme_dir / "local.mp4").write_bytes(b"fake")
    return theme_dir


@pytest.fixture()
def resolve(tmp_path, monkeypatch, qapp):
    import app.widgets.tab_manager_window as tmw
    from app.widgets.tab_manager_window import TabManagerWindow

    theme_dir = _make_theme_dir(tmp_path)
    monkeypatch.setattr(tmw.theme_manager, "get_theme_dir",
                        lambda theme_id=None: theme_dir, raising=True)
    return TabManagerWindow._resolve_theme_image


def test_parent_relative_reaches_sibling_dir(tmp_path, resolve):
    theme_dir = tmp_path / "themes" / "demo"
    got = resolve("../shared/rain.mp3")
    assert Path(got) == theme_dir.parent / "shared" / "rain.mp3"
    assert Path(got).exists()


def test_local_relative_resolves_in_theme_dir(tmp_path, resolve):
    theme_dir = tmp_path / "themes" / "demo"
    assert Path(resolve("local.mp4")) == theme_dir / "local.mp4"


def test_absolute_path_untouched(tmp_path, resolve):
    abs_path = str(tmp_path / "elsewhere.mp3")
    assert resolve(abs_path) == abs_path


def test_qrc_prefix_untouched(tmp_path, resolve):
    assert resolve(":/icons/fox_bg.png") == ":/icons/fox_bg.png"


def test_missing_file_falls_back_to_raw_value(tmp_path, resolve):
    assert resolve("../shared/nope.mp3") == "../shared/nope.mp3"
