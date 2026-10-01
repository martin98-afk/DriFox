# -*- coding: utf-8 -*-
"""回归测试：无 tool_call_id 的工具块在「工具与思考」区累积残留 / 出现两份。

现象（用户报告）
----------------
工具完成折叠框残留，偶尔出现两份，其中一份永远沉底不更新。

根因
----
`<tool>` 协议块解析不到 `tool_call_id` 时（模型正文复述协议格式、输出被截断 /
停止、旧历史数据缺该字段），渲染产物是 `data-tool-call-id=""`（空串）：

  * `_render_tool_streaming_block(tool_call_id="")` → 无 data-block-key、无 data-order
  * `render_tool_block(tool_call_id=None)`         → 有 data-block-key，但空 id

`reorganizeContent` 全线用 `if (_tid)` / `_etid && ...` 判定，**空串为假**：
  * 不登记 `_currentToolIds` → 过期清理分支短路 → **永不清理**
  * 排序 `getPos` 无 data-order 时返回 `1e9` → 流式占位框**恒沉底**
内容一变 `block_key` 就变（tool_name+args+result 的 sha1）→ 新块迁入工具区，
旧块留在原地。渲染几次就几份，全部 data-order 相同、不再重排。

修复
----
1. `_render_tool_streaming_block`：无 id 时按 tool_name+preview 合成
   `data-block-key="syn-<hash>"`，让空 id 块也有稳定身份。
2. `reorganizeContent`：新增 `_currentToolBlockKeys` 集合，过期清理分支
   同时认 data-block-key（有 id 用 id，无 id 用 bk）；排序 getPos 已有
   data-block-key 分支，无需改动。
3. `_render_tool_streaming_block` 无 id 块补 data-order 兜底由调用方
   （_inject_tool_blocks 未闭合分支）之外路径负责，此处只保证有序可比。

测试分两层（与 tests 既有风格一致）：
  A. Python 产物断言：空 id 块必须带可区分的 data-block-key
  B. JS 源码契约断言：reorganizeContent 的过期清理必须覆盖 data-block-key
真实 DOM 行为另由 tests/debug/unclosed_tool_completed_linger.py 验证（已复现
「每改一次 result 多一份」，修复后应为恒 1 份）。
"""

import re
import textwrap
from pathlib import Path

from app.widgets.message_card import (
    _inject_tool_blocks,
    _render_tool_streaming_block,
    render_tool_block,
)

_SRC_PATH = Path(__file__).resolve().parents[2] / "app" / "widgets" / "message_card.py"
_SRC = _SRC_PATH.read_text(encoding="utf-8")

# 未闭合 <tool>（无 </tool>），无 tool_call_id 字段
_UNCLOSED_NO_ID = '<tool>\nname: read_file\nargs: {"path": "app/main.py"}\nresult: 文件内容\nsuccess: true\n'
_UNCLOSED_WITH_ID = _UNCLOSED_NO_ID + "tool_call_id: call_real_1\n"


def _streaming_blocks(html: str) -> list[str]:
    """提取 HTML 中所有 tool-streaming-block 的开标签。"""
    return re.findall(r'<div class="[^"]*tool-streaming-block[^"]*"[^>]*>', html)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'{name}="([^"]*)"', tag)
    return m.group(1) if m else None


class TestStreamingBlockHasIdentity:
    """A 层：流式占位块必须带稳定可区分的身份（block_key 或 tool_call_id）。"""

    def test_no_id_block_gets_synthetic_block_key(self):
        """无 tool_call_id 时必须合成 data-block-key（否则清理/去重判据全失效）。"""
        html = _render_tool_streaming_block(tool_call_id="", tool_name="read_file", preview="读取中", completed=False)
        assert 'data-block-key="syn-' in html, (
            "无 tool_call_id 的流式块必须带合成 data-block-key，"
            "否则 reorganizeContent 的 `if (_tid)` 判据全线失效 → 永久累积"
        )
        assert _attr(html, "data-block-key"), "data-block-key 不得为空"

    def test_synthetic_key_differs_by_content(self):
        """内容不同 → 合成 key 不同（否则新旧块互相误判为同一块而被误删）。"""
        h1 = _render_tool_streaming_block("", "read_file", "版本一", completed=False)
        h2 = _render_tool_streaming_block("", "read_file", "版本二", completed=False)
        assert _attr(h1, "data-block-key") != _attr(h2, "data-block-key"), (
            "合成 key 必须包含内容维度，否则不同内容块会撞 key"
        )

    def test_synthetic_key_stable_for_same_content(self):
        """同内容重复渲染 → 合成 key 稳定（否则每轮都换身份，一样累积）。"""
        h1 = _render_tool_streaming_block("", "read_file", "同一内容", completed=False)
        h2 = _render_tool_streaming_block("", "read_file", "同一内容", completed=False)
        assert _attr(h1, "data-block-key") == _attr(h2, "data-block-key")

    def test_with_id_block_keeps_id_and_no_conflict(self):
        """有 tool_call_id 时保持原样：不合成 key、id 原样保留。"""
        html = _render_tool_streaming_block("call_x", "read_file", "读取中", completed=False)
        assert _attr(html, "data-tool-call-id") == "call_x"
        assert _attr(html, "data-block-key") is None, (
            "有 id 的块不应额外合成 block_key（避免与完成态 markdown 块身份重复）"
        )

    def test_unclosed_injection_produces_identifiable_blocks(self):
        """_inject_tool_blocks 未闭合分支产出的占位块必须可被清理判据识别。"""
        out = _inject_tool_blocks(_UNCLOSED_NO_ID, completed=False, compact=True)
        tags = _streaming_blocks(out)
        assert len(tags) == 1, f"应产出 1 个占位块，实际 {len(tags)}"
        tag = tags[0]
        has_identity = bool(_attr(tag, "data-tool-call-id")) or bool(_attr(tag, "data-block-key"))
        assert has_identity, "占位块必须有 id 或 block_key 之一，否则 reorganizeContent 清理判据失效"

    def test_completed_unclosed_block_has_block_key(self):
        """完成态空 id 块（render_tool_block）必须带 block_key 供清理。"""
        html = render_tool_block("read_file", {"path": "a.py"}, "结果", True, tool_call_id=None)
        assert _attr(html, "data-block-key"), "完成态块必须带 block_key"
        assert _attr(html, "data-tool-call-id") == "", "无 id 时该属性为空串"


class TestReorganizeCleanupCoversBlockKey:
    """B 层：reorganizeContent 的过期清理必须同时认 data-block-key。"""

    def _func_src(self, name: str) -> str:
        m = re.search(
            rf"^\s*function {re.escape(name)}\(.*?\n(.*?)\n\s*\}}\n",
            _SRC,
            re.MULTILINE | re.DOTALL,
        )
        assert m, f"未找到 JS 函数 {name}"
        return m.group(0)

    def test_cleanup_uses_block_key_set(self):
        """过期清理分支必须维护并使用 block_key 集合。"""
        src = self._func_src("reorganizeContent")
        assert "_currentToolBlockKeys" in src, (
            "reorganizeContent 必须维护 _currentToolBlockKeys —— 空 data-tool-call-id 的块"
            "在 `if (_etid)` 判据下永不清理，是「残留/两份/沉底」的根因"
        )
        assert re.search(r"_eel\.getAttribute\('data-block-key'\)", src), (
            "清理分支必须读取 data-block-key 以覆盖空 id 块"
        )
        assert "_currentToolBlockKeys.has(" in src, "清理分支必须用 block_key 集合判定过期"

    def test_cleanup_branch_not_id_only(self):
        """清理条件不得只依赖 tool_call-id（旧写法：`_etid && ...`）。"""
        src = self._func_src("reorganizeContent")
        bad = re.search(
            r"if\s*\(\s*\n?\s*_etid\s*\n?\s*&&\s*!_currentToolIds\.has",
            src,
        )
        assert not bad, "过期清理不得只认 tool_call-id —— 空串 id 会让整个条件短路，块永不清理"

    def test_block_key_registered_in_scan(self):
        """posMap/清理集合必须与单次扫描同步登记（PERF v2 的单遍结构不被破坏）。"""
        src = self._func_src("reorganizeContent")
        assert re.search(r"_currentToolBlockKeys\s*=\s*new Set\(\)", src), (
            "必须在单次扫描处初始化 _currentToolBlockKeys（保持 O(n) 单遍结构）"
        )
        assert re.search(r"_currentToolBlockKeys\.add\(", src), "扫描时必须把 block_key 加入集合"


class TestRenderPipelineSourceGuards:
    """源码级守卫：修复点不得被后续改动悄悄回退。"""

    def test_streaming_renderer_builds_synthetic_key(self):
        """_render_tool_streaming_block 必须含合成 block_key 的实现。"""
        assert "syn-" in _SRC, "message_card 必须包含合成 block_key 前缀 syn-"

    def test_sanitize_reason_comment_present(self):
        """修复点必须有注释说明根因，避免后续被当冗余删掉。"""
        idx = _SRC.find("syn-")
        assert idx > 0
        window = _SRC[max(0, idx - 1500) : idx + 500]
        assert "data-block-key" in window, "合成 key 处必须有 data-block-key 说明"
