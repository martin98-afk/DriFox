# -*- coding: utf-8 -*-
"""回归测试：思考/工具「预览文字」打字机的接线与降级

背景（用户反馈）
----------------
思考内容与工具预览是**静默累积 → 一次性全量渲染**落地的（``append_reasoning`` 的
既定设计：避免每 chunk 全量重排把 think-streaming DOM 反复销毁重建），因此文字
出现在 DOM 的那一刻是整段瞬间出现，观感生硬，且常被感知成"要等工具执行完才输出"。

方案（app/widgets/message_card.py）
----------------------------------
* ``_PREVIEW_TYPEWRITER_JS``：只作用于带 ``[data-dfx-preview]`` 的**短文本预览行**
  （思考块 summary 右侧预览 / 工具块 header 预览）。全文存 ``data-dfx-text`` 属性，
  JS 只写首个文本节点，子元素（如工具的 ``(N字符)`` 计数 span）不受影响。
  - 首次落地：从空逐字打到完整预览文本（240ms 内补齐）
  - 后续更新：从"已显示文本"续打增量，不重放已显示部分
  - DOM 重建打断（流式每 150~500ms 一次）：写指针切到新节点首个文本节点续跑
* 开关：``_perform_update`` 按 ``_streaming / _streaming_finished`` 置
  ``window._pt.enabled``，历史会话加载不逐字（几十张卡片同时播放会抢帧）。

本测试是**源码级 + HTML 产物级**校验（不启动 Chromium）。

运行：
    python -m pytest tests/widgets/test_preview_typewriter.py -v
"""

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "app" / "widgets" / "message_card.py"

_ATTR_RE = re.compile(
    r'data-dfx-preview\s+data-dfx-key="(?P<key>[^"]*)"\s+data-dfx-text="(?P<text>[^"]*)"',
)


@pytest.fixture(scope="module")
def src_text() -> str:
    return _SRC.read_text(encoding="utf-8")


def _attr_of(html: str):
    m = _ATTR_RE.search(html)
    assert m is not None, "预览行缺少 data-dfx-preview / data-dfx-key / data-dfx-text 标记"
    return m.group("key"), m.group("text")


class TestPreviewMarkers:
    """预览元素必须带打字机标记，且 data-dfx-text 与可见文本一致"""

    def test_think_compact_preview_marked(self):
        from app.widgets.message_card import _render_think_block

        html = _render_think_block("先确认文件位置，再决定怎么改。", completed=True, compact=True)
        key, text = _attr_of(html)
        # key 与 block-key 同源（思考块内容哈希），保证重渲染时是同一个 key
        assert key in html.replace(f'data-dfx-key="{key}"', ""), "key 应取自块自身的 block-key"
        assert "先确认文件位置" in text

    def test_think_block_preview_marked(self):
        from app.widgets.message_card import _render_think_block

        html = _render_think_block("先确认文件位置，再决定怎么改。", completed=True, compact=False)
        key, text = _attr_of(html)
        assert key.startswith("think-")
        assert "先确认文件位置" in text

    def test_think_lightweight_preview_marked(self):
        from app.widgets.message_card import _render_think_block_lightweight

        html = _render_think_block_lightweight("超长思考的轻量渲染路径。", completed=True)
        key, text = _attr_of(html)
        assert key == "think-light"
        assert "超长思考" in text

    def test_tool_streaming_preview_marked(self):
        from app.widgets.message_card import _render_tool_streaming_block

        html = _render_tool_streaming_block(
            tool_call_id="call_abc",
            tool_name="read_file",
            preview='读取 "a.txt" 中',
            char_count=0,
            completed=False,
        )
        key, text = _attr_of(html)
        assert key == "tool-call_abc"
        # 引号被转义：属性值不能破损（否则 JS 读到的全文截断）
        assert "&quot;" in text
        assert '"' not in text

    def test_streaming_think_has_no_preview_marker(self):
        """流式态思考块只有 spinner，没有预览行 —— 不应被标记（避免打字机空转）"""
        from app.widgets.message_card import _render_think_block

        html = _render_think_block("思考中内容", completed=False)
        assert "data-dfx-preview" not in html


class TestJsWiring:
    """JS 资产定义、注入与调用点"""

    def test_asset_declared_and_injected(self, src_text: str):
        assert '_PREVIEW_TYPEWRITER_JS = """' in src_text
        assert "{_PREVIEW_TYPEWRITER_JS}" in src_text

    def test_ptplay_defined(self, src_text: str):
        body = src_text[src_text.find("_PREVIEW_TYPEWRITER_JS = ") :]
        body = body[: body.find('\n# ── FLIP')]
        assert "window._ptPlay = function" in body
        # DOM 重建续跑：写指针挂在 st.node 上，tick 每帧重新取节点
        assert "st.node[key] = node;" in body
        assert "if (n && n.nodeType === 3) n.nodeValue = full.slice(0, pos);" in body

    def test_called_from_render_paths(self, src_text: str):
        """updateContent / updateContentAppend / reorganizeContent 末尾都要播放"""
        count = src_text.count("if (typeof window._ptPlay === 'function') window._ptPlay();")
        assert count >= 5, f"预览打字机调用点过少（{count}）：渲染路径未全部接线"

    def test_history_disables_typewriter(self, src_text: str):
        """历史会话加载必须关闭逐字（避免几十张卡片同时起 rAF 抢帧）"""
        assert "_pt_enabled = \"true\" if (self._streaming" in src_text
        assert "window._pt.enabled = {_pt_enabled};" in src_text


class TestJsSyntax:
    """JS 资产语法校验（node --check，不可用时跳过）"""

    def test_preview_js_parses(self, src_text: str):
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            pytest.skip("未找到 node，跳过 JS 语法校验")

        start = src_text.find("_PREVIEW_TYPEWRITER_JS = \"\"\"")
        assert start != -1
        body = src_text[start + len('_PREVIEW_TYPEWRITER_JS = """') :]
        body = body[: body.find('"""')]
        # 骨架里用的是 Python str.format 风格占位（无 {..} 时可直接当 JS 跑）
        if "{" in body.replace("{{", "").replace("}}", ""):
            pytest.skip("骨架含 format 占位，跳过 node --check")

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(body)
            path = f.name
        try:
            subprocess.run([node, "--check", path], check=True, capture_output=True)
        finally:
            Path(path).unlink(missing_ok=True)
