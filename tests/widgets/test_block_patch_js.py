# -*- coding: utf-8 -*-
"""patchBlocks 的 splice 语义验证（纯 Python 模型 + node 真跑）。

两段：

1. **Python 模型**：实现与骨架 JS 逐条对应的 splice 规则，验证
   「删除 → 插入 → 重编号」在流式典型场景下的结果。
2. **node 真跑**（有 node 时）：把骨架里的 `patchBlocks` 抽出来，用极简
   DOM 桩（children / insertBefore / removeChild）执行同一批用例，
   确认 JS 实现与 Python 模型给出相同结果。

单独建桩而不是用 jsdom：项目没有 jsdom 依赖，且这里只需验证 splice 逻辑，
不需要真实 HTML 解析（后处理函数用空桩即可）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


# ── Python 模型（与骨架 JS 的规则一一对应）──────────────────────────


class Node:
    def __init__(self, key: str, kind: str = "text", home: str = "content"):
        self.key = key
        self.kind = kind
        self.home = home
        self.index = 0

    def __repr__(self) -> str:
        return f"{self.kind}:{self.key}@{self.index}"


class Container:
    def __init__(self, nodes=None):
        # 允许混入非 block 节点（骨架 / 增量文本），它们不参与 splice
        self.children = list(nodes or [])
        self._renumber()

    def block_nodes(self):
        return [n for n in self.children if isinstance(n, Node)]

    def _renumber(self):
        for i, n in enumerate(self.block_nodes()):
            n.index = i

    def patch(self, base: int, delete: int, items):
        nodes = self.block_nodes()
        for i in range(delete):
            victim = nodes[base + i] if base + i < len(nodes) else None
            if victim is not None and victim in self.children:
                self.children.remove(victim)
        anchor = nodes[base + delete] if base + delete < len(nodes) else None
        made = [Node(it[1], it[0]) for it in items]
        if anchor is not None and anchor in self.children:
            pos = self.children.index(anchor)
            self.children[pos:pos] = made
        else:
            self.children.extend(made)
        self._renumber()
        return made


def test_append_only():
    c = Container([Node("a"), Node("b")])
    c.patch(2, 0, [("text", "c")])
    assert [n.key for n in c.block_nodes()] == ["a", "b", "c"]
    assert [n.index for n in c.block_nodes()] == [0, 1, 2]


def test_replace_active_block_in_place():
    """流式典型场景：只有活动块被替换，历史块不动。"""
    c = Container([Node("a"), Node("active-v1")])
    c.patch(1, 1, [("text", "active-v2")])
    assert [n.key for n in c.block_nodes()] == ["a", "active-v2"]


def test_insert_middle():
    c = Container([Node("a"), Node("c")])
    c.patch(1, 0, [("tool", "b")])
    assert [n.key for n in c.block_nodes()] == ["a", "b", "c"]
    assert [n.index for n in c.block_nodes()] == [0, 1, 2]


def test_non_block_nodes_ignored():
    """骨架 / 增量文本节点混在容器里时，splice 位置必须按 block 节点计。"""
    c = Container([Node("a"), "skeleton", Node("b"), "incremental"])
    c.patch(1, 1, [("text", "b2")])
    assert [n.key for n in c.block_nodes()] == ["a", "b2"]
    # 非 block 节点仍在原位（不参与删除）
    assert "skeleton" in c.children and "incremental" in c.children


def test_base_beyond_length_appends():
    """base 越界（旧渲染结果比补丁期望的短）时退化为追加，不抛错。"""
    c = Container([Node("a")])
    c.patch(5, 1, [("text", "z")])
    assert [n.key for n in c.block_nodes()] == ["a", "z"]


def test_delete_tail_then_insert():
    c = Container([Node("a"), Node("b"), Node("c")])
    c.patch(1, 2, [("tool", "x"), ("text", "y")])
    assert [n.key for n in c.block_nodes()] == ["a", "x", "y"]


# ── node 真跑：验证骨架 JS 与 Python 模型一致 ────────────────────────

_JS_HARNESS = r"""
// 极简 DOM 桩：只实现 patchBlocks 用到的 API
function makeNode(key, kind) {
  return {
    _key: key, _kind: kind, _attrs: {}, _kids: [],
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return this._attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k); },
    get children() { return this._kids; },
    get parentNode() { return this._parent || null; },
    set parentNode(p) { this._parent = p; },
    appendChild(n) {
      if (n && n._isFrag) { n._kids.forEach(c => this.appendChild(c)); n._kids = []; return n; }
      n.parentNode = this; this._kids.push(n); return n;
    },
    insertBefore(n, ref) {
      if (n && n._isFrag) { n._kids.forEach(c => this.insertBefore(c, ref)); n._kids = []; return n; }
      const i = ref ? this._kids.indexOf(ref) : -1;
      n.parentNode = this;
      if (i < 0) this._kids.push(n); else this._kids.splice(i, 0, n);
      return n;
    },
    removeChild(n) {
      const i = this._kids.indexOf(n);
      if (i >= 0) this._kids.splice(i, 1);
      n.parentNode = null;
      return n;
    },
  };
}
function makeFragment() {
  return { _isFrag: true, _kids: [], appendChild(n) { this._kids.push(n); return n; } };
}
const _containers = {};
global.document = {
  getElementById(id) { return _containers[id] || null; },
  createElement() { return makeNode('new', 'text'); },
  createDocumentFragment() { return makeFragment(); },
};
global.window = {};
global.reportHeightDebounced = function () {};

__PATCH_JS__

function build(id, keys) {
  const root = makeNode('root', '');
  _containers[id] = root;
  keys.forEach(k => {
    const el = makeNode(k, k.split(':')[0]);
    el.setAttribute('data-block-key', k);
    root.appendChild(el);
  });
  return root;
}
function snapshot(id) {
  return window._blockNodes(_containers[id]).map(n => ({
    key: n.getAttribute('data-block-key'),
    idx: n.getAttribute('data-block-index'),
  }));
}

const out = {};
// 1 追加
build('a', ['text:a', 'text:b']);
window.patchBlocks('a', {base: 2, delete: 0, items: [{key: 'text:c', kind: 'text', html: 'C'}]});
out.append = snapshot('a');
// 2 原地替换活动块
build('b', ['text:a', 'text:active-v1']);
window.patchBlocks('b', {base: 1, delete: 1, items: [{key: 'text:active-v2', kind: 'text', html: 'V2'}]});
out.replace = snapshot('b');
// 3 中间插入
build('c', ['text:a', 'text:c']);
window.patchBlocks('c', {base: 1, delete: 0, items: [{key: 'tool:b', kind: 'tool', html: 'B'}]});
out.insert = snapshot('c');
// 4 非 block 节点混入
build('d', ['text:a', 'text:b']);
(function () {
  const root = _containers['d'];
  const sk = makeNode('skeleton', '');
  root.appendChild(sk);
})();
window.patchBlocks('d', {base: 1, delete: 1, items: [{key: 'text:b2', kind: 'text', html: 'B2'}]});
out.mixed = snapshot('d');
out.mixedHasSkeleton = _containers['d'].children.some(c => c._key === 'skeleton');
// 5 base 越界
build('e', ['text:a']);
window.patchBlocks('e', {base: 5, delete: 1, items: [{key: 'text:z', kind: 'text', html: 'Z'}]});
out.overflow = snapshot('e');
console.log(JSON.stringify(out));
"""


def _extract_patch_js() -> str:
    src = (_ROOT / "app/widgets/card_viewers.py").read_text(encoding="utf-8")
    i = src.find("function _applyBlockMeta")
    j = src.find("// ===== B1 差量渲染：未闭合尾部行内渲染")
    seg = src[i:j]
    # f-string 双花括号还原成 JS 单花括号
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
def test_js_patchblocks_matches_model(tmp_path):
    import json

    js = _JS_HARNESS.replace("__PATCH_JS__", _extract_patch_js())
    f = tmp_path / "harness.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([_node_available(), str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[:800]
    out = json.loads(r.stdout.strip().splitlines()[-1])

    def keys(name):
        return [row["key"] for row in out[name]]

    def idx(name):
        return [row["idx"] for row in out[name]]

    assert keys("append") == ["text:a", "text:b", "text:c"]
    assert idx("append") == ["0", "1", "2"]
    assert keys("replace") == ["text:a", "text:active-v2"]
    assert keys("insert") == ["text:a", "tool:b", "text:c"]
    assert idx("insert") == ["0", "1", "2"]
    assert keys("mixed") == ["text:a", "text:b2"]
    assert out["mixedHasSkeleton"] is True
    assert keys("overflow") == ["text:a", "text:z"]
