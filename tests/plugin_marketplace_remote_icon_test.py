# -*- coding: utf-8 -*-
"""plugin-marketplace 远程 icon 加载回归测试

覆盖场景：
- resolve_remote_icon_urls() 多种元数据组合
- PluginIconWidget 本地优先 / 远程缓存命中 / 远程异步拉取 三个分支
- icon 缓存目录不存在时的容错
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


# ── resolve_remote_icon_urls 单元测试 ──────────────────────────────────


def test_resolve_remote_icon_urls_github_with_dict_icon():
    """GitHub + git-subdir + dict icon → 正确拼接 light/dark URL"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "breakout",
        "icon": {"light": "icon.svg", "dark": "icon_dark.svg"},
        "source": {
            "type": "git-subdir",
            "url": "https://github.com/martin98-afk/drifox-plugins",
            "ref": "main",
            "path": "plugins/breakout",
        },
    }
    urls = resolve_remote_icon_urls(meta)
    assert urls == {
        "light": "https://raw.githubusercontent.com/martin98-afk/drifox-plugins/main/plugins/breakout/icon.svg",
        "dark": "https://raw.githubusercontent.com/martin98-afk/drifox-plugins/main/plugins/breakout/icon_dark.svg",
    }


def test_resolve_remote_icon_urls_string_icon():
    """string icon → light/dark 使用同一路径"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "x",
        "icon": "icon.svg",
        "source": {
            "type": "git-subdir",
            "url": "https://github.com/foo/bar",
            "ref": "main",
            "path": "plugins/x",
        },
    }
    urls = resolve_remote_icon_urls(meta)
    assert urls == {
        "light": "https://raw.githubusercontent.com/foo/bar/main/plugins/x/icon.svg",
        "dark": "https://raw.githubusercontent.com/foo/bar/main/plugins/x/icon.svg",
    }


def test_resolve_remote_icon_urls_url_with_dot_git_suffix():
    """URL 含 .git 后缀应被正确剥离"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "x",
        "icon": "icon.svg",
        "source": {
            "type": "git-subdir",
            "url": "https://github.com/foo/bar.git",
            "ref": "v1.0",
            "path": "plugins/x",
        },
    }
    urls = resolve_remote_icon_urls(meta)
    assert urls == {
        "light": "https://raw.githubusercontent.com/foo/bar/v1.0/plugins/x/icon.svg",
        "dark": "https://raw.githubusercontent.com/foo/bar/v1.0/plugins/x/icon.svg",
    }


def test_resolve_remote_icon_urls_non_git_subdir_returns_none():
    """source.type != 'git-subdir' → None（不识别 url 类型市场）"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "x",
        "icon": "icon.svg",
        "source": {
            "type": "url",
            "url": "https://raw.githubusercontent.com/foo/bar/main/.claude-plugin/marketplace.json",
        },
    }
    assert resolve_remote_icon_urls(meta) is None


def test_resolve_remote_icon_urls_non_github_returns_none():
    """非 GitHub 来源 → None（如 gitlab.com、自托管等）"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "x",
        "icon": "icon.svg",
        "source": {
            "type": "git-subdir",
            "url": "https://gitlab.com/foo/bar.git",
            "ref": "main",
            "path": "plugins/x",
        },
    }
    assert resolve_remote_icon_urls(meta) is None


def test_resolve_remote_icon_urls_no_icon_field_returns_none():
    """无 icon 字段 → None（无可用图标）"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "x",
        "source": {
            "type": "git-subdir",
            "url": "https://github.com/foo/bar",
            "ref": "main",
            "path": "plugins/x",
        },
    }
    assert resolve_remote_icon_urls(meta) is None


def test_resolve_remote_icon_urls_no_source_returns_none():
    """无 source 字段 → None（无法构造 URL）"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {"name": "x", "icon": "icon.svg"}
    assert resolve_remote_icon_urls(meta) is None


def test_resolve_remote_icon_urls_dict_with_only_light():
    """dict icon 只含 light → dark 用 light 兜底"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "x",
        "icon": {"light": "icon.svg"},
        "source": {
            "type": "git-subdir",
            "url": "https://github.com/foo/bar",
            "ref": "main",
            "path": "plugins/x",
        },
    }
    urls = resolve_remote_icon_urls(meta)
    assert urls == {
        "light": "https://raw.githubusercontent.com/foo/bar/main/plugins/x/icon.svg",
        "dark": "https://raw.githubusercontent.com/foo/bar/main/plugins/x/icon.svg",
    }


# ── PluginIconWidget 集成测试 ──────────────────────────────────


def test_plugin_icon_widget_local_priority(tmp_path):
    """本地 plugin_dir 有 icon.svg → 立即渲染本地，不走远程"""
    from ui._squircle_avatar import PluginIconWidget, resolve_remote_icon_urls

    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "icon.svg").write_text("<svg/>")
    remote_urls = {"light": "https://example.com/icon.svg", "dark": "https://example.com/icon.svg"}

    w = PluginIconWidget(
        plugin_dir=plugin_dir,
        manifest={"name": "demo", "icon": "icon.svg"},
        remote_urls=remote_urls,
    )
    # 本地优先：应该有 svg_widget，无 avatar
    assert w._svg_widget is not None, "本地 SVG 必须被加载"
    assert w._avatar is None, "有本地 SVG 时不应渲染 avatar"


def test_plugin_icon_widget_remote_cache_hit(tmp_path, monkeypatch):
    """远程缓存命中 → 直接渲染缓存（不走网络）"""
    from ui import _squircle_avatar

    # 缓存命中：往 _icon_cache_dir 写一个 SVG
    name = "cached-plugin"
    cache = _squircle_avatar._icon_cache_dir() / f"{name}__light.svg"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("<svg/>")

    remote_urls = {
        "light": "https://raw.githubusercontent.com/foo/bar/main/plugins/x/icon.svg",
        "dark": "https://raw.githubusercontent.com/foo/bar/main/plugins/x/icon_dark.svg",
    }
    w = _squircle_avatar.PluginIconWidget(
        manifest={"name": name, "icon": {"light": "icon.svg", "dark": "icon_dark.svg"}},
        remote_urls=remote_urls,
    )
    # 缓存命中：应直接渲染缓存 SVG（无需网络）
    assert w._svg_widget is not None, "缓存命中时必须立即渲染"
    assert w._inflight == {}, "缓存命中时不应发起网络请求"


def test_plugin_icon_widget_no_source_shows_avatar():
    """无本地 / 无远程 → 显示 avatar 兜底"""
    from ui._squircle_avatar import PluginIconWidget

    w = PluginIconWidget(manifest={"name": "fallback"})
    assert w._svg_widget is None
    assert w._avatar is not None, "无任何 icon 源时必须显示 avatar 兜底"


def test_plugin_icon_widget_resolve_url_helper_integration():
    """cards 侧集成：resolve_remote_icon_urls → PluginIconWidget 接受 remote_urls"""
    from ui._squircle_avatar import PluginIconWidget, resolve_remote_icon_urls

    meta = {
        "name": "online-plugin",
        "icon": {"light": "icon.svg", "dark": "icon_dark.svg"},
        "source": {
            "type": "git-subdir",
            "url": "https://github.com/foo/bar",
            "ref": "main",
            "path": "plugins/online-plugin",
        },
    }
    urls = resolve_remote_icon_urls(meta)
    assert urls is not None
    w = PluginIconWidget(
        manifest={"name": meta["name"], "icon": meta["icon"]},
        remote_urls=urls,
    )
    # 无缓存时：avatar 兜底 + 异步下载启动
    assert w._avatar is not None
    assert "light" in w._inflight or "dark" in w._inflight, "必须启动异步下载"


def test_resolve_remote_icon_urls_with_real_drifox_cache():
    """端到端：从 .drifox 实际缓存读取 breakout 插件数据，验证 URL 可构造"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    cache_path = ROOT / ".drifox" / "cache" / "marketplaces" / "drifox-official.json"
    if not cache_path.exists():
        import pytest

        pytest.skip("marketplace cache not present, skipping integration check")
    import json

    data = json.loads(cache_path.read_text(encoding="utf-8"))
    breakout = next(p for p in data["plugins"] if p["name"] == "breakout")
    urls = resolve_remote_icon_urls(breakout)
    assert urls is not None
    assert "icon.svg" in urls["light"]
    assert "icon_dark.svg" in urls["dark"]
    assert urls["light"].startswith("https://raw.githubusercontent.com/martin98-afk/drifox-plugins/main/")
