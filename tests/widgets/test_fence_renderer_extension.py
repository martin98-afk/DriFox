# -*- coding: utf-8 -*-
"""插件 fence 渲染器扩展点（架构档）回归测试。

对应设计稿：docs/superpowers/specs/2026-09-04-plugin-fence-renderer-design.md
方案 B：fence 注册表 + 宿主分发接线。

回归红线（设计稿 §8 第 7 条）：未注册插件 fence 时，echarts / mermaid / svg /
html / 普通代码块的渲染结果与接入前完全一致。
"""

import json

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.message_card import (
    _fence_assets_for_skeleton,
    _get_plugin_fence_renderer,
    _plugin_fence_placeholder,
    _render_plugin_fence,
    _wrap_code_blocks_with_copy_button_web,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    yield
    reg.reset()


def _md_fence(lang: str, body: str) -> str:
    """生成与真实链路一致的 fence HTML（python-markdown + fenced_code 的产物形态）。"""
    return f'<pre><code class="language-{lang}">{body}</code></pre>'


class TestFenceRegistry:
    """registry 层：注册 / 查询 / 优先级 / 校验 / 卸载清理。"""

    def test_register_and_get(self):
        reg = UIPluginRegistry.get_instance()

        def render(code, ctx):
            return f"<b>{code}</b>"

        reg.register_fence_renderer("plug-a", "mychart", render)
        info = reg.get_fence_renderer("mychart")
        assert info is not None
        assert info.plugin_name == "plug-a"
        assert info.lang == "mychart"
        assert info.render_func is render

    def test_lang_normalized_to_lower(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer("plug-a", "MyChart", lambda c, ctx: c)
        assert reg.get_fence_renderer("mychart") is not None
        assert reg.get_fence_renderer("MYCHART") is not None

    def test_higher_priority_overrides(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer("plug-a", "mychart", lambda c, ctx: "A", priority=1)
        reg.register_fence_renderer("plug-b", "mychart", lambda c, ctx: "B", priority=5)
        assert reg.get_fence_renderer("mychart").plugin_name == "plug-b"
        # 低优先级不能覆盖高优先级
        reg.register_fence_renderer("plug-c", "mychart", lambda c, ctx: "C", priority=0)
        assert reg.get_fence_renderer("mychart").plugin_name == "plug-b"

    def test_invalid_lang_raises(self):
        reg = UIPluginRegistry.get_instance()
        with pytest.raises(ValueError):
            reg.register_fence_renderer("plug-a", "bad lang!", lambda c, ctx: c)
        with pytest.raises(ValueError):
            reg.register_fence_renderer("plug-a", "", lambda c, ctx: c)

    def test_unsafe_asset_path_raises(self):
        reg = UIPluginRegistry.get_instance()
        with pytest.raises(ValueError):
            reg.register_fence_renderer("plug-a", "mychart", lambda c, ctx: c, assets={"js": "../../evil.js"})
        with pytest.raises(ValueError):
            reg.register_fence_renderer("plug-a", "mychart", lambda c, ctx: c, assets={"exe": "a.exe"})

    def test_unknown_bridge_permission_raises(self):
        reg = UIPluginRegistry.get_instance()
        with pytest.raises(ValueError):
            reg.register_fence_renderer("plug-a", "mychart", lambda c, ctx: c, bridge_permissions=["root"])

    def test_valid_permissions_accepted(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer(
            "plug-a", "mychart", lambda c, ctx: c, bridge_permissions=["theme", "storage"]
        )
        assert reg.get_fence_renderer("mychart").bridge_permissions == ("theme", "storage")

    def test_unload_plugin_clears_registration(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer("plug-a", "mychart", lambda c, ctx: c)
        reg._loaded_plugins.add("plug-a")
        assert reg.get_fence_renderer("mychart") is not None

        reg.unload_plugin("plug-a")
        assert reg.get_fence_renderer("mychart") is None
        assert "plug-a" not in reg._loaded_plugins


class TestFenceDispatch:
    """宿主分发层：插件命中 / 内置不受影响 / 失败降级。"""

    def test_plugin_fence_rendered(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer("plug-a", "mychart", lambda code, ctx: f"<i>{code}</i>")
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("mychart", "A -> B"))
        assert 'data-fence-renderer="mychart"' in out
        assert 'data-plugin-name="plug-a"' in out
        assert "<i>A -> B</i>" in out

    def test_render_func_receives_ctx(self):
        reg = UIPluginRegistry.get_instance()
        seen = {}

        def render(code, ctx):
            seen.update(ctx)
            return "<hr/>"

        reg.register_fence_renderer("plug-a", "mychart", render)
        _wrap_code_blocks_with_copy_button_web(_md_fence("mychart", "x"))
        assert seen.get("lang") == "mychart"
        assert seen.get("plugin_name") == "plug-a"

    def test_builtin_echarts_unaffected(self):
        """回归红线：未注册插件时 echarts 行为与接入前一致。"""
        option = '{"xAxis":{"type":"category"},"series":[{"type":"bar","data":[1]}]}'
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("echarts", option))
        assert 'class="echarts-container"' in out
        assert "data-echarts-json" in out
        assert "plugin-fence" not in out

    def test_builtin_mermaid_unaffected(self):
        reg = UIPluginRegistry.get_instance()
        # 即便注册了别的 lang，mermaid 仍走内置链
        reg.register_fence_renderer("plug-a", "other", lambda c, ctx: "x")
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("mermaid", "graph TD;A-->B;"))
        assert 'class="mermaid-block"' in out
        assert "plugin-fence" not in out

    def test_plain_code_block_unaffected(self):
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("python", "print(1)"))
        assert "code-container" in out
        assert "plugin-fence" not in out

    def test_plugin_error_falls_back_to_code_block(self):
        reg = UIPluginRegistry.get_instance()

        def boom(code, ctx):
            raise RuntimeError("plugin blew up")

        reg.register_fence_renderer("plug-a", "mychart", boom)
        # 不能抛异常到渲染线程；产物应回落到普通代码块
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("mychart", "x"))
        assert "plugin-fence" not in out
        assert "code-container" in out

    def test_plugin_empty_output_falls_back(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer("plug-a", "mychart", lambda c, ctx: "   ")
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("mychart", "x"))
        assert "plugin-fence" not in out
        assert "code-container" in out


class TestFenceStreaming:
    """流式半截 fence 的占位。"""

    def test_builtin_streaming_skeleton(self):
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("mermaid-streaming", "graph TD"))
        assert "chart-skeleton" in out
        assert "chart-streaming" in out

    def test_plugin_streaming_custom_placeholder(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer(
            "plug-a", "mychart", lambda c, ctx: "<hr/>", streaming_placeholder="<div>loading</div>"
        )
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("mychart-streaming", "half"))
        assert "<div>loading</div>" in out

    def test_plugin_streaming_callable_placeholder(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer(
            "plug-a", "mychart", lambda c, ctx: "<hr/>", streaming_placeholder=lambda src: f"<p>{len(src)}</p>"
        )
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("mychart-streaming", "abcd"))
        assert "<p>4</p>" in out

    def test_plugin_streaming_default_skeleton(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer("plug-a", "mychart", lambda c, ctx: "<hr/>")
        out = _wrap_code_blocks_with_copy_button_web(_md_fence("mychart-streaming", "half"))
        assert "chart-skeleton" in out


class TestFenceHelpers:
    def test_get_renderer_returns_none_when_unregistered(self):
        assert _get_plugin_fence_renderer("nope") is None
        assert _get_plugin_fence_renderer("") is None

    def test_render_plugin_fence_escapes_nothing_extra(self):
        reg = UIPluginRegistry.get_instance()
        reg.register_fence_renderer("plug-a", "mychart", lambda c, ctx: "<b>" + c + "</b>")
        info = reg.get_fence_renderer("mychart")
        out = _render_plugin_fence(info, "hello")
        assert out == (
            '<div class="plugin-fence" data-fence-renderer="mychart" '
            'data-plugin-name="plug-a"><b>hello</b></div>'
        )

    def test_placeholder_exception_falls_back_to_skeleton(self):
        reg = UIPluginRegistry.get_instance()

        def bad(src):
            raise RuntimeError("nope")

        reg.register_fence_renderer("plug-a", "mychart", lambda c, ctx: "x", streaming_placeholder=bad)
        info = reg.get_fence_renderer("mychart")
        assert "chart-skeleton" in _plugin_fence_placeholder(info, "half")


class TestFenceAssets:
    """assets 路径解析与骨架映射表（架构档 2：按需注入 + 权限桥的前置）。"""

    def test_plugin_path_recorded_and_cleared(self):
        reg = UIPluginRegistry.get_instance()
        assert reg.get_plugin_path("plug-a") is None
        reg._plugin_paths["plug-a"] = "/tmp/plug-a"
        assert reg.get_plugin_path("plug-a") == "/tmp/plug-a"
        reg._plugin_paths.pop("plug-a", None)
        assert reg.get_plugin_path("plug-a") is None

    def test_resolve_fence_assets(self, tmp_path):
        reg = UIPluginRegistry.get_instance()
        plugin = tmp_path / "plug-a"
        (plugin / "ui" / "fence").mkdir(parents=True)
        js = plugin / "ui" / "fence" / "renderer.js"
        js.write_text("console.log(1)", encoding="utf-8")
        reg._plugin_paths["plug-a"] = str(plugin)

        out = reg.resolve_fence_assets("plug-a", {"js": "ui/fence/renderer.js"})
        assert out.get("js") == str(js)

    def test_resolve_fence_assets_blocks_traversal(self, tmp_path):
        reg = UIPluginRegistry.get_instance()
        plugin = tmp_path / "plug-a"
        plugin.mkdir()
        (tmp_path / "evil.js").write_text("x", encoding="utf-8")
        reg._plugin_paths["plug-a"] = str(plugin)
        assert reg.resolve_fence_assets("plug-a", {"js": "../evil.js"}) == {}

    def test_resolve_fence_assets_skips_missing(self, tmp_path):
        reg = UIPluginRegistry.get_instance()
        reg._plugin_paths["plug-a"] = str(tmp_path)
        assert reg.resolve_fence_assets("plug-a", {"js": "nope.js"}) == {}

    def test_resolve_fence_assets_unknown_plugin(self):
        reg = UIPluginRegistry.get_instance()
        assert reg.resolve_fence_assets("never-loaded", {"js": "a.js"}) == {}

    def test_assets_for_skeleton_empty_when_no_renderer(self):
        assets_js, perms_js, sig = _fence_assets_for_skeleton()
        assert assets_js == "{}"
        # 内置 fence（```widget）无插件替它声明权限，由宿主兜底合入 ——
        # 即便一个插件渲染器都没注册，perms 也必须含 widget 条目，
        # 否则 widget 的桥会全空。
        assert json.loads(perms_js) == {"widget": ["theme", "sendPrompt", "storage"]}
        assert sig == ()

    def test_assets_for_skeleton_collects_registered(self, tmp_path):
        reg = UIPluginRegistry.get_instance()
        plugin = tmp_path / "plug-a"
        (plugin / "a").mkdir(parents=True)
        (plugin / "a" / "r.js").write_text("console.log(1)", encoding="utf-8")
        reg._plugin_paths["plug-a"] = str(plugin)
        reg.register_fence_renderer(
            "plug-a",
            "mychart",
            lambda c, ctx: "x",
            assets={"js": "a/r.js"},
            bridge_permissions=["theme"],
        )
        assets_js, perms_js, sig = _fence_assets_for_skeleton()
        assert "mychart" in assets_js
        assert "file:///" in assets_js
        assert '"theme"' in perms_js
        assert len(sig) == 1


# ============================================================
# 内置 ```widget 围栏（沙箱 iframe 通道）
# ============================================================
class TestBuiltinWidgetFence:
    """` ```widget ` 保留脚本并装进沙箱；` ```html ` 仍然剥离脚本。"""

    @staticmethod
    def _code(lang, code):
        import html as _h

        return '<pre><code class="language-%s">%s</code></pre>' % (lang, _h.escape(code))

    def test_widget_fence_emits_sandboxed_container(self):
        import base64
        import re

        src = '<div id="a">hi</div><script>document.getElementById("a").textContent="ran"</script>'
        out = _wrap_code_blocks_with_copy_button_web(self._code("widget", src))
        m = re.search(r'<div id="([^"]+)" class="drifox-widget"[^>]*data-widget-src="([^"]+)"', out)
        assert m is not None
        assert m.group(1).startswith("wgt-")
        # 脚本必须原样保留（交给沙箱 iframe 执行），不得被净化吃掉
        assert base64.b64decode(m.group(2)).decode("utf-8") == src

    def test_widget_fence_falls_back_to_code_block_when_not_markup(self):
        out = _wrap_code_blocks_with_copy_button_web(self._code("widget", "print(1)"))
        assert "drifox-widget" not in out

    def test_html_fence_still_strips_script(self):
        out = _wrap_code_blocks_with_copy_button_web(self._code("html", "<div>hi</div><script>x()</script>"))
        assert "html-widget" in out
        assert "<script>" not in out

    def test_widget_is_reserved_for_plugins(self):
        reg = UIPluginRegistry.get_instance()
        with pytest.raises(ValueError, match="内置保留名"):
            reg.register_fence_renderer("plug-x", "widget", lambda c, ctx: c)
