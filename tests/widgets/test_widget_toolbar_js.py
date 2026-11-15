# -*- coding: utf-8 -*-
"""renderWidgetToolbars 的 DOM 层级覆盖验证（node 真跑骨架 JS）。

回归背景：v2 block 级渲染通道（DRIFOX_RENDER_V2=1 默认开启）把每段内容包进
``<div data-block-wrapper>``，svg / .html-widget 沉到 wrapper 内层。扫描器原先
只遍历 roots 的直接子节点 → v2 卡片右上角放大/保存按钮全部漏挂。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_JS_HARNESS = r"""
// 极简 DOM 桩：只实现 renderWidgetToolbars / _attachChartToolbar 用到的 API
class El {
  constructor(tag, cls = '') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.classList = {
      _s: new Set(cls.split(' ').filter(Boolean)),
      contains(c) { return this._s.has(c); },
      add(c) { this._s.add(c); },
    };
    this._attrs = {};
    this.clientWidth = 600;
    this.clientHeight = 300;
    this._toolbarAttached = false;
    this._widgetToolbar = false;
    this.style = {};
  }
  setAttribute(k, v) { this._attrs[k] = String(v); }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  querySelector(sel) {
    if (sel === 'svg') return this.children.find(c => c.tagName === 'SVG') || null;
    if (sel.indexOf('.mermaid-block') >= 0) return null;  // 测试桩无图表内部结构
    return null;
  }
  closest() { return null; }
  replaceChild(nw, old) {
    const i = this.children.indexOf(old);
    if (i >= 0) { this.children[i] = nw; nw.parentNode = this; old.parentNode = null; }
    return old;
  }
}
const _cp = new El('div');
global.document = {
  getElementById(id) { return id === 'content-placeholder' ? _cp : null; },
  createElement(tag) { return new El(tag); },
};
global.getComputedStyle = () => ({ position: 'static' });
global.window = {};
global.__attached = [];
global.window._attachChartToolbar = function (el, type) {
  if (!el || el._toolbarAttached) return;
  el._toolbarAttached = true;
  el.appendChild(new El('div', 'chart-toolbar'));
  global.__attached.push(type);
};

__SCAN_JS__

function runCase(name, build, rootsFn) {
  _cp.children.length = 0;
  global.__attached = [];
  build(_cp);
  window.renderWidgetToolbars(rootsFn ? rootsFn() : undefined);
  return { name, attached: global.__attached.slice() };
}

const out = [];
// 1 v1 全量路径：svg / html-widget 是 content-placeholder 直接子节点
out.push(runCase('v1_top_level', cp => {
  cp.appendChild(new El('svg'));
  cp.appendChild(new El('div', 'html-widget'));
}, undefined));
// 2 v2 patchBlocks：made 是 data-block-wrapper，内容沉一层（显式 roots）
out.push(runCase('v2_wrapper_roots', cp => {
  const w = new El('div', 'data-block-wrapper');
  w.setAttribute('data-block-wrapper', '1');
  w.appendChild(new El('svg'));
  w.appendChild(new El('div', 'html-widget'));
  cp.appendChild(w);
}, () => [ _cp.children[0] ]));
// 3 v2 全量重扫（无 roots）：同样必须覆盖 wrapper 内层
out.push(runCase('v2_wrapper_full', cp => {
  const w = new El('div', 'data-block-wrapper');
  w.setAttribute('data-block-wrapper', '1');
  w.appendChild(new El('svg'));
  w.appendChild(new El('div', 'html-widget'));
  cp.appendChild(w);
}, undefined));
// 4 已挂过工具栏的容器不得重复挂载
out.push(runCase('no_duplicate', cp => {
  const w = new El('div', 'data-block-wrapper');
  w.setAttribute('data-block-wrapper', '1');
  const svg = new El('svg');
  svg._widgetToolbar = true;
  const html = new El('div', 'html-widget');
  html._widgetToolbar = true;
  w.appendChild(svg); w.appendChild(html);
  cp.appendChild(w);
}, () => [ _cp.children[0] ]));
console.log(JSON.stringify(out));
"""

def _extract_scan_js() -> str:
    src = (_ROOT / "app/widgets/card_viewers.py").read_text(encoding="utf-8")
    marker = "window.renderWidgetToolbars = function(roots) {"
    i = src.find(marker)
    assert i >= 0, "renderWidgetToolbars 未找到"
    j = src.find("window.__fenceLoaded", i)
    assert j > i
    seg = src[i:j]
    seg = seg[: seg.rfind("};") + 2]
    return seg.replace("{{", "{").replace("}}", "}")

def _node_available() -> str | None:
    for cand in ("node", "node.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=5)
            return cand
        except Exception:
            continue
    return None

@pytest.mark.skipif(_node_available() is None, reason="node 不可用")
def test_render_widget_toolbars_pierces_block_wrapper(tmp_path):
    js = _JS_HARNESS.replace("__SCAN_JS__", _extract_scan_js())
    f = tmp_path / "widget_toolbar_harness.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([_node_available(), str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[:800]
    cases = {row["name"]: row["attached"] for row in json.loads(r.stdout.strip().splitlines()[-1])}

    assert cases["v1_top_level"] == ["svg", "html"]
    assert cases["v2_wrapper_roots"] == ["svg", "html"], "v2 wrapper 下工具栏全部漏挂"
    assert cases["v2_wrapper_full"] == ["svg", "html"], "v2 全量重扫同样漏挂"
    assert cases["no_duplicate"] == [], "已挂载节点被重复挂工具栏"
