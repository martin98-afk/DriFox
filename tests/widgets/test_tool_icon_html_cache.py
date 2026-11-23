# -*- coding: utf-8 -*-
"""`_get_tool_icon_html` 图标 HTML 缓存契约。

## 为什么要这组测试

`_get_tool_icon_html` 每次调用都 `Path.exists()` + `read_bytes()` + base64，
在逐帧渲染栈里分别贡献 16 / 18 次阻塞（合计 17.9s）。加缓存后必须同时锁住
四件事，任何一件破了都会变成「省了 IO 但图标错了」：

1. **重复渲染零磁盘 IO**（缓存真的命中）
2. **主题切换不串色**（`is_light` 必须在 key 里）
3. **插件重载能刷新**（世代号失效真的生效）
4. **负结果也缓存**（图标不存在时不反复打盘）

## 关键设计考虑

- 键必须含 `(icon_name, size, tool_name, is_light)` 四元：少任一项都会串号
  （错尺寸 / 错工具目录 / 错主题）。
- 失效用 `ToolRegistry.version()` 世代号而不是手挂清缓存点：version 在
  `register` / `unregister` / `unregister_by_source` / `clear` 四处自增，
  其中 `unregister_by_source` 是插件卸载的真实路径，手挂容易漏。
- 负结果同样缓存：否则「图标名拼错」会持续打盘。
"""

import base64
import sys

import pytest

import app.widgets.render_helpers as rh
from app.tools.registry import ToolRegistry

_FAKE_TOOL = "_icon_cache_contract_tool"


def _svg(fill: str) -> str:
    """构造内容可辨识的 SVG（用 fill 颜色区分深浅两版）。"""
    return f'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="{fill}"/></svg>'


def _decode_svg(html: str) -> str:
    """从 `<img src="data:image/svg+xml;base64,...">` 里解出 SVG 源文本。"""
    return base64.b64decode(html.split("base64,")[1].split('"')[0]).decode("utf-8")


class _FakeTheme:
    def __init__(self, light: bool):
        self._light = light

    def is_light_theme(self) -> bool:
        return self._light


@pytest.fixture
def icon_dirs(tmp_path):
    """真实的 icons/ 与 icons_light/ 目录，各含同名但内容不同的 SVG。"""
    dark = tmp_path / "icons"
    light = tmp_path / "icons_light"
    dark.mkdir()
    light.mkdir()
    (dark / "probe.svg").write_text(_svg("#FFFFFF"), encoding="utf-8")
    (light / "probe.svg").write_text(_svg("#000000"), encoding="utf-8")
    return dark, light


@pytest.fixture
def registered_tool(icon_dirs):
    """注册一个带 icon_dir / icon_dir_light 的临时工具，用完注销。"""
    dark, light = icon_dirs
    reg = ToolRegistry.get_instance()
    reg.register(
        name=_FAKE_TOOL,
        schema={"type": "object", "properties": {}},
        impl=lambda **k: None,
        danger="safe",
        source="icon-cache-contract",
        icon="probe",
        icon_dir=str(dark),
        icon_dir_light=str(light),
    )
    yield reg
    try:
        reg.unregister(_FAKE_TOOL)
    except Exception:
        pass


@pytest.fixture
def fake_theme(monkeypatch):
    """替换 theme_manager 为可切换的假对象，返回一个设置器。"""
    import app.utils.theme_manager as tm_mod

    holder = {"tm": _FakeTheme(False)}
    monkeypatch.setattr(tm_mod, "theme_manager", holder["tm"], raising=False)

    def _set(light: bool) -> None:
        holder["tm"] = _FakeTheme(light)
        tm_mod.theme_manager = holder["tm"]

    return _set


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个用例前后清缓存与世代号，避免用例间串扰。"""
    rh._ICON_HTML_CACHE.clear()
    rh._ICON_HTML_CACHE_VER = -1
    yield
    rh._ICON_HTML_CACHE.clear()
    rh._ICON_HTML_CACHE_VER = -1


@pytest.fixture
def io_counter(monkeypatch):
    """统计 .svg 路径的 exists / read_bytes 次数。"""
    from pathlib import Path

    counter = {"exists": 0, "read_bytes": 0}
    orig_exists = Path.exists
    orig_read = Path.read_bytes

    def _exists(self, *a, **k):
        if str(self).endswith(".svg"):
            counter["exists"] += 1
        return orig_exists(self, *a, **k)

    def _read(self, *a, **k):
        if str(self).endswith(".svg"):
            counter["read_bytes"] += 1
        return orig_read(self, *a, **k)

    monkeypatch.setattr(Path, "exists", _exists)
    monkeypatch.setattr(Path, "read_bytes", _read)
    return counter


class TestCacheHitEliminatesIO:
    def test_repeated_render_has_zero_disk_io(self, registered_tool, fake_theme, io_counter):
        """连续 100 次渲染：第 1 次有 IO，后 99 次为 0。"""
        fake_theme(False)

        first = rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)
        assert io_counter["exists"] >= 1, "首次必须真的查盘"
        assert io_counter["read_bytes"] >= 1, "首次必须真的读盘"

        io_counter["exists"] = 0
        io_counter["read_bytes"] = 0
        for _ in range(99):
            again = rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)

        assert io_counter["exists"] == 0, f"后 99 次仍在 exists 打盘：{io_counter['exists']} 次"
        assert io_counter["read_bytes"] == 0, f"后 99 次仍在 read_bytes 打盘：{io_counter['read_bytes']} 次"
        assert again == first, "缓存命中必须返回同一 HTML"

    def test_negative_result_also_cached(self, registered_tool, fake_theme, io_counter):
        """图标不存在（负结果）也必须缓存，不能每次都 exists 打盘。"""
        fake_theme(False)

        n1 = rh._get_tool_icon_html("__no_such_icon__", 18, _FAKE_TOOL)
        assert io_counter["exists"] >= 1, "首次必须查盘"

        io_counter["exists"] = 0
        n2 = rh._get_tool_icon_html("__no_such_icon__", 18, _FAKE_TOOL)

        assert io_counter["exists"] == 0, "负结果未缓存：图标名拼错会持续打盘"
        assert n1 == n2


class TestCacheKeyDimensions:
    def test_theme_switch_does_not_cross_contaminate(self, registered_tool, fake_theme):
        """深浅两态必须各取对应文件，不能串色。"""
        fake_theme(False)
        dark_svg = _decode_svg(rh._get_tool_icon_html("probe", 18, _FAKE_TOOL))

        fake_theme(True)
        light_svg = _decode_svg(rh._get_tool_icon_html("probe", 18, _FAKE_TOOL))

        assert "#FFFFFF" in dark_svg, f"深色应取 icons/probe.svg，实际 {dark_svg}"
        assert "#000000" in light_svg, f"浅色应取 icons_light/probe.svg，实际 {light_svg}"
        assert dark_svg != light_svg, "两态拿到了同一份 SVG = is_light 未进 key"

    def test_theme_switch_back_hits_own_cache(self, registered_tool, fake_theme):
        """切回深色必须拿到深色那份（不能停在浅色缓存）。"""
        fake_theme(False)
        dark_first = rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)
        fake_theme(True)
        rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)
        fake_theme(False)
        dark_again = rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)

        assert dark_again == dark_first, "切回深色拿到了浅色缓存 = 串主题"

    def test_size_and_tool_name_are_distinct_keys(self, registered_tool, fake_theme):
        """size / tool_name 各维度独立，不能互相覆盖。"""
        fake_theme(False)
        rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)
        n1 = len(rh._ICON_HTML_CACHE)
        rh._get_tool_icon_html("probe", 24, _FAKE_TOOL)
        n2 = len(rh._ICON_HTML_CACHE)
        rh._get_tool_icon_html("probe", 18, "another_tool")
        n3 = len(rh._ICON_HTML_CACHE)

        assert n2 == n1 + 1, "不同 size 必须是不同 key"
        assert n3 == n2 + 1, "不同 tool_name 必须是不同 key"

        html18 = rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)
        html24 = rh._get_tool_icon_html("probe", 24, _FAKE_TOOL)
        assert "width:18px" in html18
        assert "width:24px" in html24, "尺寸未进 key → 24px 拿到了 18px 的缓存"


class TestGenerationInvalidation:
    def test_registry_version_change_invalidates(self, registered_tool, fake_theme, icon_dirs):
        """registry version 变化（≈插件重载）后，磁盘上新内容必须生效。"""
        dark, _light = icon_dirs
        fake_theme(False)

        old_html = rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)
        old_ver = rh._ICON_HTML_CACHE_VER

        # 改磁盘内容但不动 registry：应仍命中旧缓存（证明缓存生效）
        (dark / "probe.svg").write_text(_svg("#FF0000"), encoding="utf-8")
        still_old = rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)
        assert still_old == old_html, "未触发重载却刷新了 = 缓存没生效"

        # 触发 version 自增
        registered_tool.unregister(_FAKE_TOOL)
        registered_tool.register(
            name=_FAKE_TOOL,
            schema={"type": "object", "properties": {}},
            impl=lambda **k: None,
            danger="safe",
            source="icon-cache-contract",
            icon="probe",
            icon_dir=str(dark),
            icon_dir_light=str(icon_dirs[1]),
        )

        new_html = rh._get_tool_icon_html("probe", 18, _FAKE_TOOL)
        assert rh._ICON_HTML_CACHE_VER != old_ver, "version 未变 → 世代号失效没接上"
        assert "#FF0000" in _decode_svg(new_html), "重载后仍是旧图标内容"

    def test_cache_bounded(self, registered_tool, fake_theme):
        """缓存条目达到上限后清空（不无限增长）。"""
        fake_theme(False)
        limit = rh._ICON_HTML_CACHE_MAX
        assert limit > 0

        for i in range(limit + 5):
            rh._get_tool_icon_html(f"icon_{i}", 18, _FAKE_TOOL)

        assert len(rh._ICON_HTML_CACHE) <= limit, (
            f"缓存无界：{len(rh._ICON_HTML_CACHE)} > 上限 {limit}"
        )
