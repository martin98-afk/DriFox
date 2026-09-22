# -*- coding: utf-8 -*-
"""回归测试：工具调用参数预览（ToolPayloadHtmlGenerator）的 CSS 与 HTML 类名必须对齐。

根因
----
06-16 那轮「CSS 缩写压缩」重构只改了 `<style>` 里的选择器（`.field-row` →
`.fr`、`.field-key` → `.fk`、`.field-badge` → `.fb`、`.field-preview` → `.fp`、
`.field-empty` → `.fe`、`.field-top` → `.ft`），但 `_field_summary()` 生成的
HTML 仍用完整类名 `class="field-row"` 等 → 两侧从未匹配，左侧字段区**全部
样式失效**（无卡片边框/圆角/内边距、徽标无底色、「path重点」文字粘连、
等宽字体未生效）。

存活 3 个月未被发现的原因：`ToolPayloadHtmlGenerator` 当时零测试覆盖，
且失效是「静默降级」（用户截图肉眼才能看出），无任何报错。

修复
----
CSS 侧选择器改回与 HTML 一致的完整类名（同文件 `tool-name` / `call-id` /
`cbtn` / `ctt` 等本就是长名，长名 53 个 vs 短名 22 个，长名是主流）。

测试说明
-------
本测试**不依赖 WebEngine 渲染**（本机实例化该控件会 AV 崩溃，环境级问题），
而是对生成的 HTML 做类名双向比对：HTML 用到的每个字段类必须在 CSS 中有
定义，CSS 定义的字段类也必须被 HTML 使用。这比「断言某个色值」更能锁死
该类撕裂（任何一侧改名都会立刻失败）。
"""

import re
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

# 必须在导入 diff_viewer（其导入 QWebEngineWidgets）之前设置
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
QApplication.instance() or QApplication(sys.argv)

from app.utils.diff_viewer import ToolPayloadHtmlGenerator  # noqa: E402

# 字段区（左侧栏）相关的类名前缀：断言范围只覆盖本次事故区域，
# 不牵扯 diff 视图（file-block / du-row 等）的类名体系
_FIELD_PREFIX = "field-"

_ARGS = {
    "path": "write_tool_test.md",
    "content": "# write 工具测试\n\n- 状态：ok\n",
    "description": "写入测试文件验证 write 工具可用",
}


def _split_html(html: str) -> tuple:
    """拆出 (CSS 定义区, HTML body 区)"""
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    assert style, "生成的 HTML 必须含 <style> 块"
    body = html.split("</style>", 1)[1]
    return style.group(1), body


def _css_classes(css: str) -> set:
    return set(re.findall(r"\.([a-zA-Z][\w-]*)\s*[,{:]?", css))


def _normalize(decl: str) -> str:
    """去掉所有空白，便于对声明做与排版无关的断言"""
    return re.sub(r"\s+", "", decl)


def _used_classes(body: str) -> set:
    used = set()
    for m in re.finditer(r'class="([^"]+)"', body):
        used.update(m.group(1).split())
    return used


def test_field_css_selectors_match_html_classes():
    """字段区 CSS 选择器与 HTML 类名必须一一对齐（回归锁定）

    断言方向 1：HTML 用到的每个 field-* 类都在 CSS 里有定义
    → 否则该元素「无样式」（本次事故形态：字段区全部裸奔）
    """
    html = ToolPayloadHtmlGenerator.generate_html_report("write", "call_x", _ARGS, light=True)
    css, body = _split_html(html)
    css_classes = _css_classes(css)
    used = {c for c in _used_classes(body) if c.startswith(_FIELD_PREFIX)}

    assert used, "HTML 必须使用 field-* 类名（字段区渲染丢失）"
    missing = sorted(c for c in used if c not in css_classes)
    assert not missing, (
        f"以下类名在 HTML 中使用但 CSS 未定义 → 这些元素将无样式: {missing}\n"
        f"（本次事故根因：CSS 侧被压缩成 .fr/.fk/.fb/.fp/.fe/.ft 短名，"
        f"HTML 侧仍是完整类名）"
    )


def test_field_css_selectors_all_used():
    """断言方向 2：CSS 里的 field-* 选择器必须都被 HTML 用到

    → 否则该 CSS 规则是死代码（改 HTML 类名后遗留的孤儿选择器）

    `.field-empty` 例外：它只在**无参数**时渲染，故并入两个场景的并集判定。
    """
    html = ToolPayloadHtmlGenerator.generate_html_report("write", "call_x", _ARGS, light=True)
    empty_html = ToolPayloadHtmlGenerator.generate_html_report("write", "call_x", {}, light=True)
    css, body = _split_html(html)
    _, empty_body = _split_html(empty_html)
    css_fields = {c for c in _css_classes(css) if c.startswith(_FIELD_PREFIX)}
    used = _used_classes(body) | _used_classes(empty_body)

    assert css_fields, "CSS 必须定义 field-* 选择器"
    orphans = sorted(c for c in css_fields if c not in used)
    assert not orphans, f"以下 CSS 选择器无对应 HTML 类名（死规则）: {orphans}"


def test_field_row_card_style_present():
    """字段卡片必须有边框/圆角/内边距（事故时三者全为 0）"""
    html = ToolPayloadHtmlGenerator.generate_html_report("write", "call_x", _ARGS, light=True)
    css, _ = _split_html(html)
    block = re.search(r"\.field-row\s*\{(.*?)\}", css, re.S)
    assert block, ".field-row 必须存在（字段卡片容器）"
    decl = _normalize(block.group(1))
    assert "border:1pxsolid" in decl, ".field-row 缺边框"
    assert "border-radius:6px" in decl, ".field-row 缺圆角"
    assert "padding:10px" in decl, ".field-row 缺内边距"


def test_field_badge_has_pill_style():
    """「重点」徽标必须是带底色的小胶囊（事故时无底色无圆角）"""
    html = ToolPayloadHtmlGenerator.generate_html_report("write", "call_x", _ARGS, light=True)
    css, _ = _split_html(html)
    badge = re.search(r"\.field-badge\s*\{(.*?)\}", css, re.S)
    assert badge, ".field-badge 必须存在"
    decl = _normalize(badge.group(1))
    assert "border-radius:10px" in decl, ".field-badge 缺胶囊圆角"
    assert "padding:1px6px" in decl, ".field-badge 缺内边距"
    # 重点徽标底色（必须有一条 .field-badge.risk 规则给出背景色）
    risk = re.search(r"\.field-badge\.risk\s*\{(.*?)\}", css, re.S)
    assert risk, ".field-badge.risk 必须存在（重点徽标配色）"
    assert "background:var(--yel-bg)" in _normalize(risk.group(1)), "重点徽标缺底色"


def test_field_preview_multiline_clamp():
    """参数值预览：保留换行 + 限 3 行（事故时 white-space 为 normal，换行被吃掉）"""
    html = ToolPayloadHtmlGenerator.generate_html_report("write", "call_x", _ARGS, light=True)
    css, _ = _split_html(html)
    block = re.search(r"\.field-preview\s*\{(.*?)\}", css, re.S)
    assert block, ".field-preview 必须存在"
    decl = _normalize(block.group(1))
    assert "white-space:pre-wrap" in decl, ".field-preview 必须保留换行"
    assert "-webkit-line-clamp:3" in decl, ".field-preview 必须限 3 行"


def test_field_key_uses_mono_font():
    """参数名用等宽字体（事故时 fallback 到 sans）"""
    html = ToolPayloadHtmlGenerator.generate_html_report("write", "call_x", _ARGS, light=True)
    css, _ = _split_html(html)
    block = re.search(r"\.field-key\s*\{(.*?)\}", css, re.S)
    assert block, ".field-key 必须存在"
    assert "font-family:var(--mono)" in _normalize(block.group(1)), ".field-key 必须用等宽字体"


def test_empty_arguments_uses_styled_placeholder():
    """无参数时占位文案用 .field-empty（同样必须有样式）"""
    html = ToolPayloadHtmlGenerator.generate_html_report("write", "call_x", {}, light=True)
    css, body = _split_html(html)
    assert 'class="field-empty"' in body, "无参数应渲染 field-empty 占位"
    assert ".field-empty" in css, ".field-empty 必须有样式定义"


def test_light_and_dark_both_keep_field_classes():
    """浅色/深色两套主题下类名一致（配色走 CSS 变量，类名不随主题变）"""
    for light in (True, False):
        html = ToolPayloadHtmlGenerator.generate_html_report("write", "call_x", _ARGS, light=light)
        css, body = _split_html(html)
        for cls in ("field-row", "field-top", "field-key", "field-badge", "field-preview"):
            assert f".{cls}" in css, f"light={light} 时 CSS 缺 .{cls}"
        assert 'class="field-row"' in body, f"light={light} 时 HTML 缺 field-row"
