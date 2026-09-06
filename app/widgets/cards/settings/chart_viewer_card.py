# -*- coding: utf-8 -*-
"""
ChartViewerCard — 内嵌图表查看卡片，覆盖右侧对话区域（card_id=chart_viewer）

与 DiffViewerCard 同构（BaseSettingsCard + QWebEngineView + proportional 高度），
复用 CardManager / TabManagerWindow 覆盖层栈机制（类似系统设置/差异对比）。

- echarts 模式：HTML 引用本地 vendor echarts.min.js（缺失降级 CDN），全幅重渲
  getOption 序列化产物，ResizeObserver 自适应。
- mermaid 模式：直接注入已渲染 SVG outerHTML（矢量无损、自带主题配色，零 JS 依赖），
  CSS max-width:100% 自适应。
- svg 模式：与 mermaid 同路径（payload=svg outerHTML b64 直注入 + panZoom + PNG 导出）。

导出：头部「导出 PNG」按钮 → runJavaScript 触发 window._exportChartPng(3) →
JS 生成 dataURL 后经 console.log('pywebview_action:save_chart_png:...') 回传 →
page 拦截发信号 → 弹保存框（与消息卡小图导出共用 ui_helpers.save_png_from_b64）。

HTML 加载走 diff_viewer._load_html_to_webview（临时文件 + setUrl），
规避 setHtml 对较大内容不可靠的问题（同 DiffViewerCard）。
"""

import base64
import binascii
import os
import sys
from typing import List

from loguru import logger
from PyQt5.QtCore import QBuffer, QIODevice, Qt, QUrl, pyqtSignal
from PyQt5.QtWebEngineWidgets import QWebEnginePage
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWidgets import QDialog, QVBoxLayout

from app.core.webengine_profile import create_transient_web_profile
from app.utils.diff_viewer import _cleanup_temp_files, _load_html_to_webview
from app.widgets.cards.settings.base_settings_card import BaseSettingsCard

# payload b64 上限（与消息卡 JS 侧拦截一致，防御异常大图打爆 console 通道）
_MAX_PAYLOAD_B64 = 8 * 1024 * 1024
# 大图背景/导出底色：跟随主题（深色 #1B1E24 / 浅色白底），与卡片内 echarts 观感一致
_CHART_BG_DARK = "#1B1E24"
_CHART_BG_LIGHT = "#FFFFFF"

_ECHARTS_LOCAL = "app/resources/web/vendor/echarts.min.js"
_ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"


def _echarts_script_tag() -> str:
    """本地 vendor 优先、缺失降级 CDN（与 message_card._get_vendor_script_tags 同逻辑）

    返回完整的 <script src=...>...</script> 对；闭合缺失会被 HTML 解析器把后续
    DOM/脚本全部吞进 script 文本，导致整页黑屏（曾踩坑）。
    """
    # 项目根从本文件上溯 5 级（app/widgets/cards/settings/xxx.py → 项目根）
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    )
    base_dirs = [project_root]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base_dirs.append(meipass)
    for base in base_dirs:
        candidate = os.path.join(base, _ECHARTS_LOCAL)
        if os.path.isfile(candidate):
            return '<script src="' + QUrl.fromLocalFile(candidate).toString() + '">' + _END_SCRIPT
    return '<script src="' + _ECHARTS_CDN + '">' + _END_SCRIPT


def decode_chart_payload(payload_b64: str) -> str:
    """b64 → UTF-8 文本（与卡片内 TextDecoder('utf-8') 等价）；非法输入返回空串"""
    try:
        return base64.b64decode(payload_b64).decode("utf-8")
    except binascii.Error, ValueError, UnicodeDecodeError:
        return ""


# 闭合标签拼接辅助（源码不直接出现 闭合标签，避免文档解析误吞）
def _close(tag):
    return "<" + "/" + tag + ">"


_END_SCRIPT = _close("script")
_END_BODY = _close("body")
_END_HTML = _close("html")
_END_STYLE = _close("style")
_END_HEAD = _close("head")
_SCRIPT_OPEN = "<" + "script" + ">"
_DIV_OPEN = "<" + "div" + ">"


_EXPORT_JS = """
function _b64EncodeUtf8(str) {
    return btoa(unescape(encodeURIComponent(str)));
}
function _emitPng(dataUrl) {
    var b64 = (dataUrl || '').split(',', 2)[1] || '';
    if (!b64 || b64.length > %(max_b64)d) { console.error('[chart] png too large or empty'); return; }
    console.log('pywebview_action:save_chart_png:' + _b64EncodeUtf8(window._chartName || 'chart') + ':' + b64);
}
function _exportEchartsPng(scale) {
    if (!chart) { console.error('[chart] echarts not ready'); return; }
    chart.resize();  // 强制按当前容器尺寸重算，防止实例内部宽度过期导致导出畸形
    _emitPng(chart.getDataURL({ type: 'png', pixelRatio: scale, backgroundColor: '%(bg)s' }));
}
function _exportMermaidPng(scale) {
    var svg = document.querySelector('.chart-body svg');
    if (!svg) return;
    var serialized = new XMLSerializer().serializeToString(svg);
    var vb = svg.viewBox && svg.viewBox.baseVal;
    var num = function (v) { var n = parseFloat(v); return (n && String(v).indexOf('%%') === -1) ? n : 0; };
    var w = (vb && vb.width) || num(svg.getAttribute('width')) || 800;
    var h = (vb && vb.height) || num(svg.getAttribute('height')) || 600;
    var img = new Image();
    img.onload = function () {
        var canvas = document.createElement('canvas');
        canvas.width = Math.round(w * scale);
        canvas.height = Math.round(h * scale);
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '%(bg)s';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        _emitPng(canvas.toDataURL('image/png'));
    };
    img.src = 'data:image/svg+xml;base64,' + _b64EncodeUtf8(serialized);
}
window._exportChartPng = function (scale) {
    if (window._chartType === 'echarts') _exportEchartsPng(scale);
    else _exportMermaidPng(scale);  // mermaid/svg
};
// 容器级平移缩放（mermaid SVG / 饼图等 dataZoom 不适用的图表）：
// Ctrl+滚轮以鼠标为中心缩放 · 拖拽平移 · 双击复位
function _enablePanZoom(container, content) {
    if (!container || !content || container._panZoom) return;
    container._panZoom = true;
    var scale = 1, tx = 0, ty = 0, dragging = false, lx = 0, ly = 0;
    function apply() {
        content.style.transformOrigin = '0 0';
        content.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
    }
    container.addEventListener('wheel', function (ev) {
        if (!ev.ctrlKey) return;
        ev.preventDefault();
        var factor = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
        var ns = Math.min(8, Math.max(0.4, scale * factor));
        var rect = container.getBoundingClientRect();
        var mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
        tx = mx - (mx - tx) * (ns / scale);
        ty = my - (my - ty) * (ns / scale);
        scale = ns;
        apply();
    }, { passive: false });
    container.addEventListener('mousedown', function (ev) {
        if (scale <= 1) return;
        dragging = true; lx = ev.clientX; ly = ev.clientY;
        container.style.cursor = 'grabbing';
    });
    window.addEventListener('mousemove', function (ev) {
        if (!dragging) return;
        tx += ev.clientX - lx; ty += ev.clientY - ly;
        lx = ev.clientX; ly = ev.clientY;
        apply();
    });
    window.addEventListener('mouseup', function () { dragging = false; container.style.cursor = ''; });
    container.addEventListener('dblclick', function () { scale = 1; tx = 0; ty = 0; apply(); });
    var tip = document.createElement('div');
    tip.textContent = 'Ctrl+滚轮缩放 · 拖拽平移 · 双击复位';
    tip.style.cssText = 'position:absolute;top:8px;left:12px;font-size:11px;opacity:.5;pointer-events:none;color:%(tip_color)s;z-index:5;';
    container.style.position = 'relative';
    container.appendChild(tip);
}
window._enablePanZoom = _enablePanZoom;
"""


def _svg_theme_css_vars(is_dark: bool) -> str:
    """按当前主题生成 :root CSS 变量块（与 message_card 骨架 :root 同源）。

    visualization 技能产出的内联 SVG / html widget 样式大量引用
    var(--text) / var(--accent-soft) 等变量，变量定义在聊天页骨架的 :root；
    预览页若不注入同名变量，var() 解析失败 → fill 回退 SVG 默认黑色（整块变黑）。
    mermaid 渲染产物与 echarts option 自带实色，注入变量对它们无影响（纯增益兜底）。
    """
    from app.utils.design_tokens import BorderRadius, current_theme

    theme = current_theme()
    is_light = not is_dark
    accent = theme["accent"]

    def _accent_rgba(v: str, alpha: float) -> str:
        h = (v or "").strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            try:
                return "rgba(%d, %d, %d, %s)" % (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
            except ValueError:
                pass
        return "rgba(100, 198, 255, %s)" % alpha  # 解析失败兜底：原 midnight 色

    pairs = [
        ("--bg", "transparent"),
        ("--panel", theme["card_bg_solid"]),
        ("--panel-elevated", theme["card_bg_solid"]),
        ("--panel-soft", theme["content_bg"]),
        ("--border", theme["border"]),
        ("--border-strong", theme["border_accent"]),
        ("--text", theme["text_primary"]),
        ("--text-secondary", theme["text_secondary"]),
        ("--text-muted", theme["text_muted"]),
        ("--accent", accent),
        ("--accent-warm", theme["accent_warm"]),
        ("--code-bg", "var(--panel-soft)" if is_light else "transparent"),
        ("--code-toolbar", "rgba(0,0,0,0.03)" if is_light else "rgba(255, 255, 255, 0.03)"),
        ("--code-border", "var(--border)" if is_light else "#2a3447"),
        ("--success", "#5fd18c"),
        ("--danger", "#ff7b7b"),
        # 语义派生层：与 message_card 骨架同源（浅/深主题通吃）
        ("--accent-text", accent),
        ("--accent-soft", theme["hover_bg"]),
        ("--accent-soft-strong", theme["selected_bg"]),
        ("--accent-border-weak", _accent_rgba(accent, 0.22)),
        ("--accent-glow", _accent_rgba(accent, 0.10)),
        ("--row-alt", "rgba(15, 23, 42, 0.03)" if is_light else "rgba(255, 255, 255, 0.02)"),
        ("--row-hover", "rgba(15, 23, 42, 0.05)" if is_light else "rgba(255, 255, 255, 0.05)"),
        ("--row-header", "rgba(15, 23, 42, 0.06)" if is_light else "rgba(255, 255, 255, 0.04)"),
    ]
    body = "; ".join(f"{k}: {v}" for k, v in pairs)
    return ":root { " + body + "; " + BorderRadius.CSS_VARS + " }"


def build_chart_viewer_html(chart_type: str, payload_b64: str, is_dark: bool = True) -> str:
    """构建图表大图查看 HTML

    Args:
        chart_type: "echarts" | "mermaid" | "svg"
        payload_b64: echarts option JSON / mermaid|svg 的 SVG outerHTML 的 UTF-8 b64
        is_dark: 深色主题（背景与 echarts init 主题跟随）

    Raises:
        ValueError: payload 超上限或类型未知
    """
    if chart_type not in ("echarts", "mermaid", "svg", "html"):
        raise ValueError("未知图表类型: " + chart_type)
    if len(payload_b64) > _MAX_PAYLOAD_B64:
        raise ValueError("图表数据超过 8MB 上限")

    bg = _CHART_BG_DARK if is_dark else _CHART_BG_LIGHT
    js_common = _EXPORT_JS % {"max_b64": _MAX_PAYLOAD_B64, "bg": bg, "tip_color": "#333" if bg == _CHART_BG_LIGHT else "#aaa"}

    if chart_type == "echarts":
        echarts_tag = _echarts_script_tag()
        pre_end = _close("pre")
        body_script = (
            echarts_tag
            + "\n"
            + '<div id="chart">'
            + "\n"
            + _SCRIPT_OPEN
            + "\n"
            + "var _PAYLOAD = '"
            + payload_b64
            + "';\n"
            + "var chart = null;\n"
            + "(function () {\n"
            + "    var el = document.getElementById('chart');\n"
            + "    try {\n"
            + "        var bytes = Uint8Array.from(atob(_PAYLOAD), function (c) { return c.charCodeAt(0); });\n"
            + "        var option = JSON.parse(new TextDecoder('utf-8').decode(bytes));\n"
            + "        chart = echarts.init(el, "
    + ("'dark'" if is_dark else "null")
    + ");\n"
            + "        chart.setOption(option);\n"
            + "        // 局部缩放：直角坐标系图表注入滚轮缩放+拖拽平移+底部滑条（源 option 已带 dataZoom 则尊重原配置；饼图/仪表盘不适用）\n"
            + "        try {\n"
            + "            var _dzTypes = ['line','bar','scatter','candlestick','effectScatter','boxplot','heatmap'];\n"
            + "            var _isCartesian = option.xAxis && option.series;\n"
            + "            if (_isCartesian) {\n"
            + "                _isCartesian = false;\n"
            + "                for (var i = 0; i < option.series.length; i++) {\n"
            + "                    if (_dzTypes.indexOf(option.series[i].type) >= 0) { _isCartesian = true; break; }\n"
            + "                }\n"
            + "            }\n"
            + "            if (_isCartesian && !option.dataZoom) {\n"
            + "                chart.setOption({ dataZoom: [\n"
            + "                    { type: 'inside', zoomOnMouseWheel: true, moveOnMouseMove: true },\n"
            + "                    { type: 'slider', height: 20, bottom: 8 }\n"
            + "                ]});\n"
            + "            } else if (!_isCartesian) {\n"
            + "                // 饱图/仪表盘等非直角系：dataZoom 不适用，走容器级平移缩放\n"
            + "                window._enablePanZoom(el, el.firstElementChild || el);\n"
            + "            }\n"
            + "        } catch (dzErr) { console.error('[chart] dataZoom init:', dzErr); }\n"
            + "    } catch (e) {\n"
            + "        el.innerHTML = '<pre style=\"color:#e06c75;padding:16px;\">图表渲染失败: ' + e + '" + pre_end + "';\n"
            + "        return;\n"
            + "    }\n"
            + "    window.addEventListener('resize', function () { chart && chart.resize(); });\n"
            + "})();\n"
            + "window._chartType = 'echarts';\n"
            + _END_SCRIPT
            + "\n"
        )
        style = (
            "html, body { margin: 0; padding: 0; width: 100%%; height: 100%%; background: %(bg)s; overflow: hidden; } "
            "#chart { width: 100%%; height: 100%%; }"
        )
    elif chart_type == "html":
        # html 不与 svg/mermaid 同路径：svg/mermaid 走 _enablePanZoom（矢量图
        # 平移缩放 transform），而 html widget 本身就是成型布局（指标卡/效果稿，
        # 自带 flex/grid/margin），套 transform 会把布局搅乱——表现为内容错位、
        # 不居中、大片空白。这里只做居中展示 + 可滚动。
        # payload 是消息侧已净化过的 .html-widget innerHTML（script/事件属性
        # 在 fence 阶段就摘掉了），这里再兜一道 script 检测防 payload 被伪造。
        pre_end = _close("pre")
        body_script = (
            '<div class="chart-body">'
            + "\n"
            + _SCRIPT_OPEN
            + "\n"
            + "var _PAYLOAD = '"
            + payload_b64
            + "';\n"
            + "(function () {\n"
            + "    var wrap = document.querySelector('.chart-body');\n"
            + "    try {\n"
            + "        var bytes = Uint8Array.from(atob(_PAYLOAD), function (c) { return c.charCodeAt(0); });\n"
            + "        var widgetHtml = new TextDecoder('utf-8').decode(bytes);\n"
            + "        if (/<script/i.test(widgetHtml)) throw new Error('html contains script');\n"
            + "        wrap.innerHTML = widgetHtml;\n"
            + "    } catch (e) {\n"
            + "        wrap.innerHTML = '<pre style=\"color:#e06c75;padding:16px;\">内容渲染失败: ' + e + '" + pre_end + "';\n"
            + "    }\n"
            + "})();\n"
            + "window._chartType = '"
            + chart_type
            + "';\n"
            + _END_SCRIPT
            + "\n"
        )
        # margin:auto 而非 align-items:center：flex 居中在内容高于视口时会把
        # 溢出端裁掉且滚不到；margin:auto 溢出后仍可滚动到全部内容
        style = (
            "html, body { margin: 0; padding: 0; width: 100%%; height: 100%%; background: %(bg)s; overflow: auto; }"
            " .chart-body { display: flex; min-height: 100%%; padding: 24px; box-sizing: border-box; }"
            " .chart-body > * { margin: auto; max-width: 100%%; }"
        )
    elif chart_type in ("mermaid", "svg"):
        # svg 与 mermaid 同路径：payload 均为 svg outerHTML b64，直注入 + panZoom。
        pre_end = _close("pre")
        body_script = (
            '<div class="chart-body">'
            + "\n"
            + _SCRIPT_OPEN
            + "\n"
            + "var _PAYLOAD = '"
            + payload_b64
            + "';\n"
            + "(function () {\n"
            + "    var wrap = document.querySelector('.chart-body');\n"
            + "    try {\n"
            + "        var bytes = Uint8Array.from(atob(_PAYLOAD), function (c) { return c.charCodeAt(0); });\n"
            + "        var svgHtml = new TextDecoder('utf-8').decode(bytes);\n"
            + "        if (/<script/i.test(svgHtml)) throw new Error('svg contains script');\n"
            + "        wrap.innerHTML = svgHtml;\n"
            + "        // 兜底：无 width/height 属性、只有 viewBox 的内联 SVG，在旧 Chromium\n"
            + "        // （Qt5 WebEngine）里 intrinsic size 按 0 算 → flex 下 0x0 整图不可见；\n"
            + "        // 按 viewBox 补显式像素属性，CSS max-width:100% + height:auto 继续管自适应\n"
            + "        var _el = wrap.firstElementChild;\n"
            + "        if (_el && _el.tagName && _el.tagName.toLowerCase() === 'svg') {\n"
            + "            var _vb = _el.viewBox && _el.viewBox.baseVal;\n"
            + "            if (_vb && _vb.width > 0 && _vb.height > 0 && !_el.getAttribute('width') && !_el.getAttribute('height')) {\n"
            + "                _el.setAttribute('width', _vb.width);\n"
            + "                _el.setAttribute('height', _vb.height);\n"
            + "            }\n"
            + "        }\n"
            + "        window._enablePanZoom(wrap, wrap.firstElementChild || wrap);\n"
            + "    } catch (e) {\n"
            + "        wrap.innerHTML = '<pre style=\"color:#e06c75;padding:16px;\">图表渲染失败: ' + e + '" + pre_end + "';\n"
            + "    }\n"
            + "})();\n"
            + "window._chartType = '"
            + chart_type
            + "';\n"
            + _END_SCRIPT
            + "\n"
        )
        style = (
            "html, body { margin: 0; padding: 0; width: 100%%; height: 100%%; background: %(bg)s; overflow: auto; }"
            " .chart-body { display: flex; align-items: center; justify-content: center; width: 100%%; height: 100%%; padding: 16px; box-sizing: border-box; }"
            " .chart-body svg { max-width: 100%%; height: auto; }"
        )

    # 内联 SVG / html widget 样式依赖骨架 :root 变量（var(--text) 等），
    # 缺变量时 fill 无效回退默认黑 → 黑块（曾踩坑，见 _svg_theme_css_vars）。
    # 拼在格式化之后：变量值不受 %%(bg)s 模板影响。
    style_full = (style % {"bg": bg}).replace("100%%", "100%") + " " + _svg_theme_css_vars(is_dark)
    head_open = "<" + "head" + ">"
    body_open = "<" + "body" + ">"
    style_open = "<" + "style" + ">"

    return (
        "<!DOCTYPE html>"
        + "\n"
        + head_open
        + style_open
        + style_full
        + _END_STYLE
        + '<meta charset="utf-8">'
        + _END_HEAD
        + body_open
        + _SCRIPT_OPEN
        + js_common  # 工具函数必须先于 body_script 定义：mermaid/echarts 的同步 IIFE 会调用 _enablePanZoom
        + _END_SCRIPT
        + body_script
        + _END_BODY
        + _END_HTML
    )


_HTML_OPEN = "<" + "html" + ">"
_BODY_OPEN = "<" + "body" + ">"


class _ChartViewerPage(QWebEnginePage):
    """图表查看页：拦截 pywebview_action:save_chart_png: 导出回传"""

    exportPngRequested = pyqtSignal(str, str)  # (name, png_b64)

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        msg = message.strip()
        if msg.startswith("pywebview_action:save_chart_png:"):
            try:
                rest = msg.split("pywebview_action:save_chart_png:", 1)[1]
                name_b64, png_b64 = rest.split(":", 1)
                if len(png_b64) > _MAX_PAYLOAD_B64:
                    logger.warning("[ChartViewer] 导出 PNG 超过大小上限，已拒绝")
                    return
                self.exportPngRequested.emit(decode_chart_payload(name_b64), png_b64)
            except Exception as e:
                logger.warning(f"[ChartViewer] 导出消息解析失败: {e}")


class ChartViewerCard(BaseSettingsCard):
    """内嵌图表查看卡片，用法类似文件差异对比面板"""

    def __init__(self, parent=None):
        super().__init__("图表查看", "📊", parent=parent)
        self.setMinimumHeight(200)
        self.set_height_mode("proportional")
        self._tmp_files: List[str] = []

        self._webview = QWebEngineView()
        self._profile = create_transient_web_profile(self)
        self._page = _ChartViewerPage(self._profile, self._webview)
        self._webview.setPage(self._page)
        self._page.exportPngRequested.connect(self._save_png)
        self.content_layout.addWidget(self._webview, 1)

        from qfluentwidgets import FluentIcon

        self._export_btn = self.add_header_button(FluentIcon.SAVE, "导出 PNG（3x 高清）", self._on_export_clicked)
        self._chart_type = ""

    def load_chart(self, chart_type: str, payload_b64: str, title: str = ""):
        """加载图表（echarts option b64 / mermaid、svg 的 outerHTML b64）"""
        self.set_title_text(title or {"svg": "SVG 查看", "html": "HTML 查看"}.get(chart_type, "图表查看"))
        self._chart_type = chart_type
        # html 走 Qt 抓图（视口物理像素），无 3x 矢量重画，tooltip 如实标注
        if chart_type == "html" and getattr(self, "_export_btn", None) is not None:
            self._export_btn.setToolTip("导出 PNG（当前视图）")
        _cleanup_temp_files(self._tmp_files)
        try:
            from app.utils.theme_manager import theme_manager

            is_dark = not theme_manager.is_light_theme()
        except Exception:
            is_dark = True
        try:
            html = build_chart_viewer_html(chart_type, payload_b64, is_dark=is_dark)
        except ValueError as e:
            logger.warning(f"[ChartViewer] {e}")
            err_html = (
                "<!DOCTYPE html>"
                + _HTML_OPEN
                + _BODY_OPEN
                + "<p style='color:#e06c75'>"
                + str(e)
                + _close("p")
                + _END_BODY
                + _END_HTML
            )
            html = err_html
        _load_html_to_webview(self._webview, html, self._tmp_files)

        # 导出依赖 window._chartType 就绪；页面 load 完成后再放行导出（防点击空跑）
        self._export_ready = False
        self._webview.loadFinished.connect(self._on_load_finished, Qt.ConnectionType.UniqueConnection)  # type: ignore[attr-defined]

    def _on_load_finished(self, ok: bool):
        try:
            self._webview.loadFinished.disconnect(self._on_load_finished)  # type: ignore[attr-defined]
        except TypeError, RuntimeError:
            pass
        self._export_ready = bool(ok)

    def _on_export_clicked(self):
        if not getattr(self, "_export_ready", False):
            return
        if getattr(self, "_chart_type", "") == "html":
            self._export_widget_png()
            return
        self._webview.page().runJavaScript("window._exportChartPng && window._exportChartPng(3);")

    def _export_widget_png(self):
        """html widget 导出 PNG：成型布局非 echarts 实例、无 html2canvas，
        JS 矢量通道（getDataURL / svg→canvas）均不适用 → Qt 侧抓整视图
        （含页面主题底色，物理像素保真）"""
        pixmap = self._webview.grab()
        if pixmap.isNull():
            logger.warning("[ChartViewer] html widget 截图失败")
            return
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pixmap.save(buf, "PNG"):
            logger.warning("[ChartViewer] html widget 截图编码失败")
            return
        self._save_png("HTML widget", base64.b64encode(bytes(buf.data())).decode("ascii"))

    def _save_png(self, name: str, png_b64: str):
        # 延迟导入：ui_helpers 顶部反向 import MessageCard，顶层导入会成环
        from app.widgets.ui_helpers import save_png_from_b64

        path = save_png_from_b64(self, png_b64, name or "图表")
        if path:
            logger.info(f"[ChartViewer] 图表已导出: {path}")

    def clear(self):
        _cleanup_temp_files(self._tmp_files)
        self._webview.setHtml("")


class ChartViewerWindow(QDialog):
    """弹窗回退（无全局卡片容器时使用，与 DiffViewerWindow 同定位）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("图表查看")
        self.resize(900, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._card = ChartViewerCard(self)
        layout.addWidget(self._card)

    def load_chart(self, chart_type: str, payload_b64: str):
        self._card.load_chart(chart_type, payload_b64)
