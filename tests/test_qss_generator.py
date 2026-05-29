# -*- coding: utf-8 -*-
"""QSSGenerator 单元测试"""
import pytest
from app.utils.qss_generator import (
    Resolver, Renderer, COMPONENT_MAP, qss_manager
)


class TestResolver:
    """测试引用解析"""

    def test_simple_replace(self):
        colors = {"accent": "#57d29a", "border_default": "#334155"}
        result = Resolver.resolve("{colors.accent}", colors)
        assert result == "#57d29a"

    def test_no_reference(self):
        colors = {}
        result = Resolver.resolve("12px", colors)
        assert result == "12px"

    def test_missing_key_keeps_original(self):
        colors = {}
        result = Resolver.resolve("{colors.nonexistent}", colors)
        assert result == "{colors.nonexistent}"

    def test_inline_reference_in_shadow(self):
        colors = {"input_focus_border": "#57d29a"}
        result = Resolver.resolve("0 0 6px {colors.input_focus_border}", colors)
        assert result == "0 0 6px #57d29a"

    def test_multiple_references(self):
        colors = {"a": "#111", "b": "#222"}
        result = Resolver.resolve("a:{colors.a} b:{colors.b}", colors)
        assert result == "a:#111 b:#222"

    def test_resolve_dict(self):
        colors = {"accent": "#57d29a"}
        config = {"bg": "{colors.accent}", "border_radius": "12px"}
        resolved = Resolver.resolve_dict(config, colors)
        assert resolved == {"bg": "#57d29a", "border_radius": "12px"}

    def test_resolve_dict_nested(self):
        colors = {"accent": "#57d29a"}
        config = {"states": {"focus": {"border_color": "{colors.accent}"}}}
        resolved = Resolver.resolve_dict(config, colors)
        assert resolved["states"]["focus"]["border_color"] == "#57d29a"


class TestRenderer:
    """测试 QSS 输出"""

    def test_empty_config(self):
        qss = Renderer.render("nonexistent", {})
        assert qss == ""

    def test_basic_properties(self):
        config = {"bg": "#000", "text": "#fff", "border_radius": "8px"}
        qss = Renderer.render("nonexistent", config)
        assert qss == ""

    def test_render_known_component(self):
        COMPONENT_MAP["test_btn"] = {
            "selector": "#testButton",
            "props": {"bg": "background", "text": "color", "border_radius": "border-radius"},
            "states": {"hover": ":hover"},
        }
        try:
            config = {
                "bg": "#333",
                "text": "#fff",
                "border_radius": "8px",
                "states": {"hover": {"bg": "#444"}},
            }
            qss = Renderer.render("test_btn", config)
            assert "#testButton {" in qss
            assert "background: #333;" in qss
            assert "color: #fff;" in qss
            assert "border-radius: 8px;" in qss
            assert "#testButton:hover {" in qss
            assert "background: #444;" in qss
        finally:
            del COMPONENT_MAP["test_btn"]

    def test_border_merge(self):
        COMPONENT_MAP["test_input"] = {
            "selector": "#inputField",
            "props": {"bg": "background", "border_width": "border-width", "border_color": "border-color"},
            "states": {},
        }
        try:
            config = {"bg": "#1a1a1a", "border_width": "1px", "border_color": "#555"}
            qss = Renderer.render("test_input", config)
            assert "border: 1px solid #555;" in qss
        finally:
            del COMPONENT_MAP["test_input"]

    def test_inherit_property_skipped(self):
        COMPONENT_MAP["test_inherit"] = {
            "selector": "#inheritWidget",
            "props": {"bg": "background", "border_radius": "border-radius"},
            "states": {},
        }
        try:
            config = {"bg": "#111", "border_radius": "inherit"}
            qss = Renderer.render("test_inherit", config)
            assert "border-radius" not in qss
            assert "background: #111;" in qss
        finally:
            del COMPONENT_MAP["test_inherit"]


class TestQSSManager:
    """测试管理器集成"""

    def test_build_and_cache(self):
        colors = {"accent": "#57d29a"}
        components = {
            "input_area": {
                "bg": "{colors.accent}",
                "border_radius": "8px",
            }
        }
        COMPONENT_MAP["input_area"] = {
            "selector": "#inputEdit",
            "props": {"bg": "background", "border_radius": "border-radius"},
            "states": {},
        }
        try:
            result = qss_manager.build_all("test_theme", components, colors)
            assert "input_area" in result
            qss = result["input_area"]
            assert "#57d29a" in qss
            assert "border-radius: 8px;" in qss
        finally:
            del COMPONENT_MAP["input_area"]
            qss_manager.invalidate("test_theme")

    def test_get_missing_returns_empty(self):
        qss = qss_manager.get("nonexistent", "missing")
        assert qss == ""
