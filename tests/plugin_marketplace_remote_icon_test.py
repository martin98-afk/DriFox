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


def test_resolve_remote_icon_urls_no_icon_field_returns_candidates():
    """无 icon 字段 → 返回 assets/ 候选列表（claudecode 约定）"""
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
    urls = resolve_remote_icon_urls(meta)
    assert urls is not None, "无 icon 字段时也应返回候选列表"
    assert isinstance(urls["light"], list), "候选列表应为 list"
    assert urls["light"][0] == ("https://raw.githubusercontent.com/foo/bar/main/plugins/x/assets/icon.svg")


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


# ── icon_size 覆盖测试 ──────────────────────────────────


def test_plugin_icon_widget_icon_size_override():
    """icon_size 参数直接覆盖默认算法"""
    from ui._squircle_avatar import PluginIconWidget

    w = PluginIconWidget(manifest={"name": "x"}, icon_size=48)
    assert w._icon_size() == 48, "icon_size 覆盖值必须生效"

    w2 = PluginIconWidget(manifest={"name": "x"}, font_size=14)
    assert w2._icon_size() == 23, "无覆盖时仍走默认算法 (14*1.7=23)"

    w3 = PluginIconWidget(manifest={"name": "x"}, font_size=14, icon_size=36)
    assert w3._icon_size() == 36, "覆盖值优先于默认算法"


def test_plugin_icon_widget_set_icon_size_runtime():
    """set_icon_size() 运行时更新"""
    from ui._squircle_avatar import PluginIconWidget

    w = PluginIconWidget(manifest={"name": "x"}, icon_size=36)
    assert w._icon_size() == 36
    w.set_icon_size(48)
    assert w._icon_size() == 48, "set_icon_size 必须生效"


def test_plugin_icon_widget_avatar_size_matches_svg_size(tmp_path):
    """占位符 avatar 与 SVG 图标尺寸必须一致（视觉对齐）

    场景：未安装、meta 无 icon 字段 → 显示 avatar → 尺寸应等于 icon_size
    场景：未安装、有 remote_urls 且缓存命中 → 显示 SVG → 尺寸 = icon_size
    两者必须相等，避免同一列表内行高不一致。
    """
    from ui._squircle_avatar import PluginIconWidget

    # 场景 1：纯占位符（无任何 icon 源）
    w1 = PluginIconWidget(manifest={"name": "x"}, icon_size=42)
    assert w1._svg_widget is None
    assert w1._avatar is not None
    assert w1._avatar.size().width() == 42
    assert w1._avatar.size().height() == 42

    # 场景 2：缓存命中显示 SVG
    from ui import _squircle_avatar as _sa_mod

    name = "consistency-test"
    cache = _sa_mod._icon_cache_dir() / f"{name}__light.svg"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("<svg/>")

    w2 = PluginIconWidget(
        manifest={"name": name, "icon": {"light": "icon.svg", "dark": "icon_dark.svg"}},
        remote_urls={
            "light": "https://raw.githubusercontent.com/foo/bar/main/x/icon.svg",
            "dark": "https://raw.githubusercontent.com/foo/bar/main/x/icon_dark.svg",
        },
        icon_size=42,
    )
    assert w2._svg_widget is not None
    assert w2._svg_widget.size().width() == 42
    assert w2._svg_widget.size().height() == 42

    # 两者尺寸必须完全一致
    assert w1._avatar.size() == w2._svg_widget.size(), "占位符与 SVG 尺寸不一致"


# ── claudecode 兼容测试 ──────────────────────────────────


def test_resolve_remote_icon_urls_claude_github_source():
    """claudecode source: {source: github, repo} → raw URL 可构造"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "airtable",
        "icon": "assets/icon.svg",
        "source": {"source": "github", "repo": "Airtable/skills", "ref": "main"},
    }
    urls = resolve_remote_icon_urls(meta)
    assert urls == {
        "light": "https://raw.githubusercontent.com/Airtable/skills/main/assets/icon.svg",
        "dark": "https://raw.githubusercontent.com/Airtable/skills/main/assets/icon.svg",
    }


def test_resolve_remote_icon_urls_claude_url_source():
    """claudecode source: {source: url, url: git 仓库} → raw URL 可构造"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "endorlabs",
        "icon": "assets/logo.png",
        "source": {"source": "url", "url": "https://github.com/endorlabs/ai-plugins.git"},
    }
    urls = resolve_remote_icon_urls(meta)
    assert urls == {
        "light": "https://raw.githubusercontent.com/endorlabs/ai-plugins/main/assets/logo.png",
        "dark": "https://raw.githubusercontent.com/endorlabs/ai-plugins/main/assets/logo.png",
    }


def test_resolve_remote_icon_urls_claude_relative_source_with_marketplace():
    """claudecode 相对路径 source（./plugins/xxx）+ 市场源 github → 仓库可反推"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "claude-md-manager",
        "source": "./plugins/claude-md-manager",
        "_marketplace_source": {
            "source": "github",
            "repo": "wshobson/agents",
            "ref": "main",
        },
    }
    urls = resolve_remote_icon_urls(meta)
    assert urls is not None
    assert urls["light"][0] == (
        "https://raw.githubusercontent.com/wshobson/agents/main/plugins/claude-md-manager/assets/icon.svg"
    )


def test_resolve_remote_icon_urls_non_github_still_none():
    """无 GitHub 仓库信息时仍返回 None（避免乱拼 URL）"""
    from ui._squircle_avatar import resolve_remote_icon_urls

    meta = {
        "name": "x",
        "source": {
            "source": "url",
            "url": "https://gitlab.com/foo/bar.git",
        },
    }
    assert resolve_remote_icon_urls(meta) is None


def test_plugin_icon_widget_remote_urls_candidates_format():
    """remote_urls 支持候选列表格式（{theme: [url...]}）"""
    from ui._squircle_avatar import PluginIconWidget

    w = PluginIconWidget(
        manifest={"name": "cand"},
        remote_urls={
            "light": [
                "https://raw.githubusercontent.com/foo/bar/main/assets/icon.svg",
                "https://raw.githubusercontent.com/foo/bar/main/assets/logo.png",
            ],
            "dark": "https://raw.githubusercontent.com/foo/bar/main/assets/icon_dark.svg",
        },
    )
    # str 值归一化为单元素列表
    assert w._remote_urls["light"] == [
        "https://raw.githubusercontent.com/foo/bar/main/assets/icon.svg",
        "https://raw.githubusercontent.com/foo/bar/main/assets/logo.png",
    ]
    assert w._remote_urls["dark"] == ["https://raw.githubusercontent.com/foo/bar/main/assets/icon_dark.svg"]


def test_plugin_icon_widget_cache_hit_png(tmp_path):
    """位图缓存命中 → 渲染 bitmap label（assets/logo.png 场景）"""
    from ui import _squircle_avatar

    name = "png-plugin"
    cache = _squircle_avatar._icon_cache_dir() / f"{name}__light.png"
    cache.parent.mkdir(parents=True, exist_ok=True)
    # 最小合法 1x1 透明 PNG
    import base64

    cache.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
    )
    w = _squircle_avatar.PluginIconWidget(
        manifest={"name": name},
        remote_urls={
            "light": "https://raw.githubusercontent.com/foo/bar/main/assets/icon.svg",
        },
        icon_size=36,
    )
    # 本地无 svg 缓存但按候选扩展名查 png 缓存 → 命中位图
    assert w._svg_widget is None, "命中 png 缓存不应渲染 svg widget"
    assert w._bitmap_label is not None, "命中 png 缓存应渲染 bitmap label"
    assert w._inflight == {}, "缓存命中时不应发起网络请求"


def test_plugin_icon_widget_local_assets_resolution(tmp_path):
    """已安装 claudecode 插件：本地 assets/icon.svg（无 manifest.icon）→ 直接渲染"""
    from ui._squircle_avatar import PluginIconWidget

    plugin_dir = tmp_path / "airtable"
    (plugin_dir / "assets").mkdir(parents=True)
    (plugin_dir / "assets" / "icon.svg").write_text("<svg/>")

    w = PluginIconWidget(
        plugin_dir=plugin_dir,
        manifest={"name": "airtable"},  # claudecode plugin.json 无 icon 字段
    )
    assert w._svg_widget is not None, "本地 assets/icon.svg 必须被加载"
    assert w._avatar is None, "有本地图标时不应显示 avatar 兜底"
