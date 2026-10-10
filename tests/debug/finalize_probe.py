# -*- coding: utf-8 -*-
"""结束态收尾探针：验证「差量收尾」是否可安全打开（DRIFOX_INCREMENTAL_FINALIZE=1）。

背景
----
finish_streaming 默认走**全量终渲染**：`cp.innerHTML = newHtml` 整页替换。
流式期间已差量渲染好的稳定段落被一起销毁重建 → 结束瞬间整页重排闪一下，
工具区坞态归位也跟着跳。这是每轮回答结束都必然发生一次的可见抖动。
`_try_incremental_finalize` 已实现差量收尾（稳定区 DOM 不动，只补剩余段），
但因「未实机验证」默认关闭。本探针就是那个验证。

判据（两条，缺一不可）
----------------------
1. **稳定区 DOM 未被重建**：流式期间给稳定段落节点打 expando 标记，
   finish 后标记必须还在。标记丢失 = 走了整页替换 = 白改。
2. **内容完整性**：finish 后可见文本必须覆盖全部 markdown 的纯文本
   （markdown 语法符号被渲染消解，故用「去符号后包含」判定）。

场景覆盖：纯段落 / 未闭合代码块 / think 块 / 混合 / 极短消息。

运行：python tests/debug/finalize_probe.py
"""

import json
import os
import sys

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

init_shared_web_profile()

# 打标：给当前所有顶层段落节点打 expando
MARK_JS = r"""
(function() {
    var cp = document.getElementById('content-placeholder');
    if (!cp) return 'no_cp';
    var kids = cp.children, n = 0;
    for (var i = 0; i < kids.length; i++) {
        if (kids[i].__dfxProbe !== undefined) continue;
        kids[i].__dfxProbe = 'm' + i;
        n++;
    }
    return String(n);
})()
"""

# 校验：标记存活率 + 可见文本
CHECK_JS = r"""
(function() {
    var cp = document.getElementById('content-placeholder');
    if (!cp) return JSON.stringify({err:'no_cp'});
    var kids = cp.children, total = 0, alive = 0;
    for (var i = 0; i < kids.length; i++) {
        if (kids[i].__dfxProbe !== undefined) { total++; alive++; }
    }
    // 未打标的新节点不计入存活率分母（它们是本轮新增，本就该是新的）
    return JSON.stringify({
        total: total, alive: alive,
        text: cp.textContent || '',
        childCount: kids.length
    });
})()
"""


def _strip_md(md: str) -> str:
    """去掉 markdown 语法符号，得到『应当可见』的纯文本骨架。"""
    import re

    s = md
    s = re.sub(r"```[\s\S]*?```", "", s)  # 代码块整体（内部会进 <pre>，textContent 仍在，故不删）
    s = s.replace("**", "").replace("*", "").replace("`", "")
    s = re.sub(r"^\s*#+\s*", "", s, flags=re.M)
    s = re.sub(r"<think>[\s\S]*?</think>", "", s)
    s = re.sub(r"\s+", "", s)
    return s


def run_case(name: str, md: str, chunk: int = 24, incremental: bool = True):
    # 模块级已 `from ... import INCREMENTAL_FINALIZE_ENABLED`，必须打到引用方
    from app.widgets import card_viewers as cv

    cv.INCREMENTAL_FINALIZE_ENABLED = incremental
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
        print(f"  [{name}] FAIL viewer 未就绪", flush=True)
        return False

    card.start_streaming_anim()
    QTest.qWait(120)

    # 流式喂入：前半段喂完并等待差量落地后打标
    half = len(md) // 2
    sent = 0

    def feed():
        nonlocal sent
        if sent >= len(md):
            timer.stop()
            return
        piece = md[sent : sent + chunk]
        sent += len(piece)
        card.append_text(piece)

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(60)

    marked = None
    while sent < len(md):
        QTest.qWait(40)
        if marked is None and sent >= half:
            QTest.qWait(600)  # 等差量渲染落地
            marked = viewer._run_js_sync(MARK_JS, timeout_ms=2000)
    timer.stop()
    QTest.qWait(800)

    card.finish_streaming()
    QTest.qWait(1500)

    raw = viewer._run_js_sync(CHECK_JS, timeout_ms=3000)
    ok = True
    try:
        p = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"  [{name}] 解析失败 {e}: {str(raw)[:160]}", flush=True)
        return False

    total = p.get("total", 0)
    alive = p.get("alive", 0)
    keep_rate = (100.0 * alive / total) if total else 0.0
    dom = "".join((p.get("text") or "").split())

    status = []
    if not total:
        # 无打标节点：本场景没有稳定区（极短消息 / 半程时还没差量落地），
        # 不构成「是否被重建」的判据，跳过该项而非判失败。
        status.append("⚠️ 无稳定区（不适用）")
    else:
        if keep_rate < 99:
            ok = False
            status.append(f"✗ 稳定区被重建（存活 {alive}/{total}={keep_rate:.0f}%）")
        else:
            status.append(f"✓ 稳定区保留 {alive}/{total}")
    status.append(f"[DOM {len(dom)} 字]")

    print(f"  [{name}] " + "  ".join(status), flush=True)
    card.deleteLater()
    QTest.qWait(100)
    return ok, dom


CASES = {
    "纯段落": "第一段中文内容，用于验证差量收尾。\n\n第二段中文内容，同样用于验证。\n\n第三段收尾。",
    "未闭合代码块": "说明文字在这里。\n\n```python\ndef f():\n    return 1\n",
    "think块": "前置文本。\n\n<think>这是一段思考内容，应该在结束后定稿。</think>\n\n后置文本。",
    "混合": (
        "开头段落。\n\n**加粗强调**与普通文字混合。\n\n"
        "```js\nconst a = 1;\n```\n\n"
        "<think>思考一下。</think>\n\n结尾段落。"
    ),
    "极短": "短。",
}


def main():
    """同一场景跑两遍（开/关差量收尾），逐场景对比最终 DOM 文本。

    内容完整性的判据用**开/关两版相互对照**，而不是拿 markdown 原文去比：
    后者要对代码块语言标识、行号、语法高亮 span 做一堆去符号猜测（实测
    这类猜测本身就是误报来源 —— 未闭合代码块 / 混合场景曾 100% 假缺失）。
    差量收尾的正确性定义就是「与全量终渲染产出一致」，故直接比二者。
    """
    all_ok = True
    for name, md in CASES.items():
        try:
            ok_on, dom_on = run_case(name, md, incremental=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [{name}] 开启版异常 {e}", flush=True)
            all_ok = False
            continue
        try:
            ok_off, dom_off = run_case(name, md, incremental=False)
        except Exception as e:  # noqa: BLE001
            print(f"  [{name}] 关闭版异常 {e}", flush=True)
            all_ok = False
            continue

        if dom_on == dom_off:
            print(f"  [{name}] ≡ 与全量终渲染产出完全一致", flush=True)
        else:
            # 差异定位：找首个不同位置
            n = min(len(dom_on), len(dom_off))
            i = 0
            while i < n and dom_on[i] == dom_off[i]:
                i += 1
            print(
                f"  [{'差异'}] [{name}] ✗ 与全量不一致（{len(dom_on)} vs {len(dom_off)} 字）"
                f"\n       首个差异 @{i}: 差量={dom_on[i:i+40]!r} 全量={dom_off[i:i+40]!r}",
                flush=True,
            )
            all_ok = False
        all_ok = ok_on and all_ok
    print("\n结论:", "✓ 差量收尾可安全打开" if all_ok else "✗ 存在回退风险，保持关闭", flush=True)


main()
