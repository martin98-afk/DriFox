# -*- coding: utf-8 -*-
"""强停（中途打断）双症状复现探针 v2：对齐真实 app 的 v2 渲染通道。

与 v1 探针的差异：
- 先流式正文（v2 接管），再注入工具运行框，再 append_tool_result 完成
  （对齐用户「工具完成框」场景 + `_will_collapse_tool_section()==True`
  → 终渲染走 280ms defer 路径）
- 工具完成后继续流式几段正文，然后强停

运行：python tests/debug/_probe_stop_loss_and_tool_body.py
"""

import json
import os
import re
import sys

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_app = QApplication.instance() or QApplication(sys.argv)

FEED_INTERVAL_MS = int(os.environ.get("PROBE_INTERVAL_MS", "30"))
CHUNK_CHARS = int(os.environ.get("PROBE_CHUNK_CHARS", "14"))

PARA_TMPL = "这是第{idx:03d}段流式测试内容，用于验证强停后正文完整性与工具框展开。标记[{tok}]。\n\n"
TOOL_NAME = "bash"
TOOL_ID = "probe-tool-0001"

_READ_JS = r"""
(function () {
    var c = document.getElementById('content-placeholder');
    var tc = document.getElementById('tool-content');
    var ts = document.getElementById('tool-section');
    return JSON.stringify({
        cp: c ? (c.textContent || '') : '',
        tc: tc ? (tc.textContent || '') : '',
        collapsed: ts ? ts.getAttribute('data-collapsed') : null,
        streamingActive: !!window._streamingActive,
        streamingBlocks: document.querySelectorAll('#tool-content [data-streaming="true"]').length,
        lazyBodies: document.querySelectorAll('.tool-block[data-lazy-body="1"]').length
    });
})()
"""


def _tokens_in(text):
    return set(re.findall(r"T\d{4}", text or ""))


def main():
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()

    card = MessageCard("assistant")
    card.resize(900, 760)
    card.show()
    card.ensure_rendered()

    for _ in range(80):
        QTest.qWait(150)
        v = card.viewer
        if v is not None and getattr(v, "_is_js_ready", False):
            break
    viewer = card.viewer
    if viewer is None or not getattr(viewer, "_is_js_ready", False):
        print("[FAIL] viewer 未就绪", flush=True)
        os._exit(1)

    card.start_streaming_anim()
    QTest.qWait(150)

    all_text = "".join(PARA_TMPL.format(idx=i, tok=f"T{i:04d}") for i in range(40))
    para_len = len(PARA_TMPL.format(idx=0, tok="T0000"))
    pos = 0

    def feed_until(target_paras):
        """流式喂到第 target_paras 段。"""
        nonlocal pos
        limit = target_paras * para_len
        while pos < min(limit, len(all_text)):
            card.append_text(all_text[pos : pos + CHUNK_CHARS])
            pos += CHUNK_CHARS
            QTest.qWait(FEED_INTERVAL_MS)
        QTest.qWait(400)

    # 1. 正文 5 段（v2 接管）
    feed_until(5)
    print(f"[phase1] 正文 5 段，_render_v2={viewer._render_v2}，_content_data 类型={type(card._content_data).__name__}", flush=True)

    # 2. 工具运行框
    card.update_tool_streaming(TOOL_ID, TOOL_NAME, {"command": "echo hi"})
    QTest.qWait(300)

    # 3. 工具完成（完成框，_restore_finished_ids 非空）
    card.append_tool_result(
        tool_name=TOOL_NAME,
        arguments={"command": "echo hi"},
        result="hi\n",
        success=True,
        tool_call_id=TOOL_ID,
    )
    QTest.qWait(400)
    print(f"[phase3] 工具已完成，_restore_finished_ids={viewer._restore_finished_ids}", flush=True)

    # 4. 继续正文到第 10 段
    feed_until(10)

    before = json.loads(viewer._run_js_sync(_READ_JS, timeout_ms=5000) or "{}")
    expect = _tokens_in(all_text[:pos])
    before_have = _tokens_in(before.get("cp", ""))
    print("\n===== 强停前（流式态） =====", flush=True)
    print(f"DOM 正文 token: {len(before_have)}/{len(expect)}，缺失 {len(sorted(expect - before_have))}", flush=True)

    # ===== 强停（对齐 _on_stop_clicked）=====
    card.stop_streaming_anim()
    card.finish_streaming(force_dock_off=True)
    print("\n[stop] finish_streaming(force_dock_off=True) 已调", flush=True)
    print(f"[stop] _will_collapse={viewer._will_collapse_tool_section()} defer_timer_active={viewer._finish_defer_timer.isActive()}", flush=True)
    print(f"[stop] _markdown_text len={len(viewer._markdown_text or '')} lazy_cb={viewer._lazy_markdown_cb is not None}", flush=True)
    print(f"[stop] _incremental_finalize={getattr(viewer, '_incremental_finalize', None)} _render_v2={viewer._render_v2}", flush=True)
    print(f"[stop] _stable_md_len={viewer._stable_md_len}", flush=True)

    # T+50ms：defer 窗口内（终渲染未落地）
    QTest.qWait(50)
    t50 = json.loads(viewer._run_js_sync(_READ_JS, timeout_ms=5000) or "{}")
    t50_have = _tokens_in(t50.get("cp", ""))
    print(f"\n[T+50ms] 正文 token: {len(t50_have)}/{len(expect)} 缺失 {len(sorted(expect - t50_have))}", flush=True)
    try:
        from app.widgets.card_render_core import find_tool_segment_by_call_id

        seg = find_tool_segment_by_call_id(viewer._markdown_text or "", TOOL_ID)
    except Exception as e:
        seg = f"ERR {e}"
    cache_hit = (getattr(viewer, "_tool_md_cache", {}) or {}).get(TOOL_ID)
    print(f"[T+50ms] tool-body 双源: md反查={'None' if seg is None else 'HIT'} cache={'HIT' if cache_hit else 'None'}", flush=True)

    # 等终渲染
    QTest.qWait(2500)
    after = json.loads(viewer._run_js_sync(_READ_JS, timeout_ms=5000) or "{}")
    after_have = _tokens_in(after.get("cp", ""))
    missing = sorted(expect - after_have)
    print("\n===== T+2.5s（终渲染后） =====", flush=True)
    print(f"DOM 正文 token: {len(after_have)}/{len(expect)}，缺失 {len(missing)}", flush=True)
    if missing:
        print(f"!!! 正文丢失复现：缺失 {missing[:20]}", flush=True)
    print(
        f"工具区: collapsed={after.get('collapsed')} streamingActive={after.get('streamingActive')} "
        f"streamingBlocks={after.get('streamingBlocks')} lazyBody块={after.get('lazyBodies')}",
        flush=True,
    )
    print(f"[post] _markdown_text len={len(viewer._markdown_text or '')} lazy_cb={viewer._lazy_markdown_cb is not None}", flush=True)
    print(f"[post] _incremental_finalize={getattr(viewer, '_incremental_finalize', None)}", flush=True)

    # 症状 2：点击完成框（懒 body 通道）
    print("\n===== 模拟点击工具完成框 =====", flush=True)
    viewer._serve_tool_body(TOOL_ID)
    QTest.qWait(800)
    click = json.loads(
        viewer._run_js_sync(
            r"""
(function () {
    var blocks = document.querySelectorAll('.tool-block[data-tool-call-id]');
    var out = {blocks: blocks.length};
    for (var i = 0; i < blocks.length; i++) {
        var b = blocks[i];
        if (b.dataset.toolCallId === 'probe-tool-0001') {
            out.cls = b.className;
            out.lazyBody = b.dataset.lazyBody || null;
            out.lazyLoading = b.dataset.lazyLoading || null;
            out.expanded = b.dataset.expanded || null;
            var body = b.querySelector('.cm-collapsible__body');
            out.bodyLen = body ? (body.innerHTML || '').length : -1;
        }
    }
    return JSON.stringify(out);
})()
""",
            timeout_ms=5000,
        )
        or "{}"
    )
    print(f"[click-result] {click}", flush=True)

    print("\ndone", flush=True)
    sys.stdout.flush()
    os._exit(0)


main()
