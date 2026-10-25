# -*- coding: utf-8 -*-
"""复现探针 3：多工具循环 + 长正文（卡内滚动）下 save/restore 全量渲染频率。

探针 2 盲区：_restore_finished_ids 非空后全量走 _build_save_and_restore_js
包装（摘工具区→innerHTML 替换→放回），不调 window.updateContent，JS hook
看不见。本探针直接 hook Python 侧：
  - _build_save_and_restore_js → save_restore 计数（每次 = 一次整页替换闪烁）
  - _apply_render_result → apply_ok
  - _rdiag 各分支（tail_inline / tool_active_inline_only / full_submit / diff_applied）

场景模拟真实流：think → 正文 → [工具运行中 + 正文持续] × 3 轮循环。
检验假设 H1：工具完成后闭合 <tool> 滞留 tail + active dom 恒真 → 全量兜底
频率是「每工具一次」（设计）还是「每拍一次」（基线推进失效 bug）。
"""

import os
import sys
import time
import json

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

init_shared_web_profile()

_PARA = (
    "这是一段用于复现超长卡片流式问题的中文正文内容，模拟大语言模型的长回复形态，"
    "每个段落包含若干长句，用于触发差量渲染路径与卡内滚动模式。"
)
BODY_CHUNK = ("\n\n" + _PARA) * 200
FEED_INTERVAL_MS = 30
FEED_CHARS = 60

TOOL_SEQ = [
    ("read_file", {"path": "src/main.py"}, "import sys\n\n" + ("content line\n" * 30)),
    ("grep", {"pattern": "def render", "path": "app/widgets"}, "card_viewers.py:100:def render():"),
    ("bash", {"command": "pytest tests/widgets -x"}, ".......................... passed"),
]


def main():
    card = MessageCard("assistant")
    card.resize(900, 800)
    card.show()
    card.ensure_rendered()

    for _ in range(60):
        QTest.qWait(200)
        v = card.viewer
        if v is not None and getattr(v, "_is_js_ready", False):
            break
    viewer = card.viewer
    if viewer is None or not getattr(viewer, "_is_js_ready", False):
        print("[FAIL] viewer 未就绪", flush=True)
        return
    print("[init] js_ready OK", flush=True)

    # Python 侧 hook：save/restore 全量计数 + 调用栈
    import traceback

    counts = {"save_restore": 0, "apply_ok": 0, "full_submit": 0, "events": []}
    orig_sr = viewer._build_save_and_restore_js

    def sr_wrap(html, finished_ids):
        counts["save_restore"] += 1
        stack = "".join(traceback.format_stack(limit=6)[:-1])
        # 只保留 app/ 内的帧摘要
        frames = [
            ln.strip().replace("app/widgets/", "").replace("app/", "")
            for ln in stack.splitlines()
            if "app/" in ln and "File" in ln
        ]
        counts["events"].append(
            (round((time.monotonic() % 1000) * 1000) % 1000000, "sr", len(html), " <- ".join(frames[-3:]))
        )
        return orig_sr(html, finished_ids)

    viewer._build_save_and_restore_js = sr_wrap

    orig_apply = viewer._apply_render_result

    def apply_wrap(seq, html):
        if html is not None and seq == viewer._render_seq:
            counts["apply_ok"] += 1
            counts["events"].append((round((time.monotonic() % 1000) * 1000) % 1000000, "apply", len(html or "")))
        return orig_apply(seq, html)

    viewer._apply_render_result = apply_wrap

    orig_pf = viewer._perform_update

    def pf_wrap():
        before_fs = counts["full_submit"]
        orig_pf()
        if counts["full_submit"] != before_fs:
            counts["events"].append((round((time.monotonic() % 1000) * 1000) % 1000000, "full_submit", len(viewer._markdown_text)))

    # full_submit 通过 _rdiag_hit 计数，wrap _rdiag_hit 抓 full_submit 事件
    orig_hit = viewer._rdiag_hit

    def hit_wrap(key):
        if key == "full_submit":
            counts["full_submit"] += 1
            tail = (viewer._markdown_text or "")[viewer._stable_md_len :]
            tpos = {t: tail.find(t) for t in ("<think>", "</think>", "<tool>", "</tool>") if t in tail}
            counts["events"].append(
                (
                    round((time.monotonic() % 1000) * 1000) % 1000000,
                    "fs",
                    len(viewer._markdown_text or ""),
                    f"stable={viewer._stable_md_len} tail_len={len(tail)} tag_pos={tpos} "
                    f"tail_head={tail[:60]!r} tail_tail={tail[-60:]!r}",
                )
            )
        return orig_hit(key)

    viewer._rdiag_hit = hit_wrap
    viewer._perform_update = pf_wrap  # noqa: F841（保持原调用语义）

    card.start_streaming_anim()
    QTest.qWait(150)

    # think 块
    card.append_text("<think>\n")
    for i in range(6):
        card.append_text(f"思考段 {i}：分析当前任务，规划工具调用与正文结构。" * 3)
        QTest.qWait(40)
    card.append_text("\n</think>\n\n")
    QTest.qWait(400)

    # 正文 + 工具循环：每轮 [运行中工具 + 持续正文 2s] → 工具完成 → 续正文
    sent = 0
    total = len(BODY_CHUNK)

    def feed_piece(n=FEED_CHARS):
        nonlocal sent
        if sent < total:
            piece = BODY_CHUNK[sent : sent + n]
            sent += len(piece)
            card.append_text(piece)

    # 先喂 3000 字符让卡片长起来
    for _ in range(50):
        feed_piece()
        QTest.qWait(FEED_INTERVAL_MS)

    for round_i, (tname, targs, tresult) in enumerate(TOOL_SEQ):
        tid = f"call_p3_{round_i}"
        print(f"\n[round {round_i}] 工具运行中: {tname}", flush=True)
        card.update_tool_streaming(tid, tname, dict(targs))
        # 运行中：持续喂正文 2s（模拟工具执行期间 LLM 继续出字）
        t_end = time.monotonic() + 2.0
        while time.monotonic() < t_end:
            feed_piece()
            QTest.qWait(FEED_INTERVAL_MS)
        print(f"[round {round_i}] 工具完成", flush=True)
        card.append_tool_result(tool_name=tname, arguments=dict(targs), result=tresult, success=True, tool_call_id=tid)
        # 完成后续喂 1s
        t_end = time.monotonic() + 1.0
        while time.monotonic() < t_end:
            feed_piece()
            QTest.qWait(FEED_INTERVAL_MS)

    # 收尾：剩余正文
    while sent < total:
        feed_piece()
        QTest.qWait(FEED_INTERVAL_MS)
    QTest.qWait(3000)

    # ── 分析 ──
    print("\n===== 计数 =====", flush=True)
    print(f"save_restore(整页替换) = {counts['save_restore']}", flush=True)
    print(f"apply_ok(全量落地)     = {counts['apply_ok']}", flush=True)
    print(f"full_submit(全量提交)  = {counts['full_submit']}", flush=True)
    print(f"_rdiag: {dict(viewer._rdiag) if viewer._rdiag else {}}", flush=True)
    md = len(viewer._markdown_text)
    print(f"md 长度 = {md}  stable_md_len = {viewer._stable_md_len}", flush=True)
    tail = viewer._markdown_text[viewer._stable_md_len :]
    has_tag = any(t in tail for t in ("<think>", "</think>", "<tool>", "</tool>"))
    print(f"tail 长度 = {len(tail)}  tail 含协议标签 = {has_tag}", flush=True)

    print("\n===== save/restore 事件时间线 =====", flush=True)
    sr_events = [e for e in counts["events"] if e[1] == "sr"]
    for e in sr_events[:40]:
        print(f"t={e[0]} html_len={e[2]}", flush=True)
    if len(sr_events) >= 2:
        gaps = [b[0] - a[0] for a, b in zip(sr_events, sr_events[1:])]
        print(f"save/restore 间隔(ms): {gaps[:30]}", flush=True)

    print("\n===== full_submit 时 tail 状态 =====", flush=True)
    for e in counts["events"]:
        if e[1] == "fs":
            print(f"t={e[0]} md={e[2]} {e[3]}", flush=True)

    # JS 侧最终一致性
    try:
        raw = viewer._run_js_sync(
            "JSON.stringify({cp:(document.getElementById('content-placeholder')||{textContent:''}).textContent.length,"
            "tc:(document.getElementById('tool-content')||{textContent:''}).textContent.length,"
            "sh:document.body.scrollHeight, ch:document.body.clientHeight})",
            timeout_ms=2000,
        )
        print(f"\nJS 状态: {raw}", flush=True)
    except Exception as e:
        print(f"JS 采样失败: {e}", flush=True)
    print("done", flush=True)


main()
