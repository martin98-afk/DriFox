# -*- coding: utf-8 -*-
"""收尾渲染成本探针：流式 → finish_streaming，测 DOM 完整性与耗时。

背景（2026-10-31）：线上日志收尾渲染 ``[finish-render] js_land+layout=450~490ms``，
原因是 v2 的 ``force_full`` 会清空两个容器，Chromium 重排整卡。改为「终渲染走
增量 diff」后，需要验证：
  1. 收尾后 DOM 正文完整（段落 token 不丢）—— 增量路径最大的风险；
  2. ``js_land+layout`` 耗时不高于改动前。

运行：python tests/debug/_probe_finish_render_cost.py
"""

import json
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DRIFOX_DATA_DIR", str(_ROOT / ".drifox-test"))
os.environ.pop("QT_QPA_PLATFORM", None)

from PyQt5.QtCore import QCoreApplication, QEventLoop, Qt, QTimer  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

apply_render_env(default_config_path())

app = QApplication.instance() or QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile(parent=app)

PARA = (
    "这是第{idx:03d}段流式测试内容，用于验证收尾渲染完整性与耗时。标记[T{idx:04d}]。"
    + "填充正文内容，包含 `code` 与 **加粗** 以及一段中文描述，用来把单段体量拉到接近真实回复。" * 6
    + "\n\n"
)

READ_JS = """
(function () {
  var c = document.getElementById('content-placeholder');
  var t = document.getElementById('tool-content');
  return JSON.stringify({
    cp: c ? (c.textContent || '') : '',
    tc: t ? (t.textContent || '') : '',
    cpWrappers: c ? c.querySelectorAll('[data-block-wrapper]').length : -1
  });
})()
"""


def spin(seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec_()


def main() -> None:
    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant")
    card.resize(900, 700)
    # 真实形态：多块（思考 + 工具）+ 末尾活动正文，全量收尾需要重建全部块，
    # 增量只替换活动块 —— 差异在多块场景才显现。
    base_blocks = []
    for _i in range(6):
        base_blocks.append({"type": "reasoning", "content": f"第{_i}段思考内容，用来撑起思考卡。" * 20})
        base_blocks.append(
            {
                "type": "tool_result",
                "tool_call_id": f"t{_i}",
                "name": "bash",
                "arguments": {"command": "ls"},
                "content": "输出内容" * 80,
                "success": True,
            }
        )
    base_blocks.append({"type": "text", "text": ""})
    card._content_data = base_blocks
    card._streaming = True
    card.show()
    card.ensure_rendered()
    spin(0.6)

    viewer = card.viewer
    if viewer is None or not getattr(viewer, "_is_js_ready", False):
        print("[FAIL] viewer 未就绪")
        return
    viewer._streaming = True
    viewer._dock_sync_requested = True
    card.start_streaming_anim()
    spin(0.2)

    N = int(os.environ.get("PROBE_PARAS", "60"))
    expect_tokens = set()
    for i in range(N):
        card.append_text(PARA.format(idx=i))
        expect_tokens.add(f"T{i:04d}")
        spin(0.06)
    spin(0.6)

    def js(code):
        box = []
        viewer.page().runJavaScript(code, lambda r: box.append(r))
        spin(0.3)
        return box[0] if box else None

    before = json.loads(js(READ_JS) or "{}")
    before_tokens = set(re.findall(r"T\d{4}", before.get("cp", "")))

    print("\n===== 收尾前 =====")
    print(f"正文 token: {len(before_tokens)}/{len(expect_tokens)}，块 wrapper={before.get('cpWrappers')}")

    t0 = __import__("time").perf_counter()
    if os.environ.get("PROBE_FORCE_FULL") == "1":
        # 反向验证：抹掉落地基线 → finish_streaming 的增量标记不置位 → 走 force_full
        viewer._v2_last = None
        viewer._needs_full_render = True
    card.finish_streaming()

    # 采样：DOM 首次达到完整 token 的时刻（含 JS 执行 + 布局）
    ready_ms = -1.0
    for _ in range(120):
        box = []
        viewer.page().runJavaScript(READ_JS, lambda r: box.append(r))
        spin(0.03)
        if box:
            try:
                d = json.loads(box[0])
            except Exception:
                d = {}
            if len(set(re.findall(r"T\d{4}", d.get("cp", "")))) >= len(expect_tokens):
                ready_ms = (__import__("time").perf_counter() - t0) * 1000
                break
    spin(1.0)
    wall = (__import__("time").perf_counter() - t0) * 1000

    after = json.loads(js(READ_JS) or "{}")
    after_tokens = set(re.findall(r"T\d{4}", after.get("cp", "")))
    missing = sorted(expect_tokens - after_tokens)

    print("\n===== 收尾后 =====")
    print(f"收尾墙钟: {wall:.0f}ms")
    print(f"DOM 达到完整 token 用时: {ready_ms:.0f}ms")
    print(f"正文 token: {len(after_tokens)}/{len(expect_tokens)}，缺失 {len(missing)} {missing[:8]}")
    print(f"块 wrapper={after.get('cpWrappers')}")
    print(f"_incremental_finalize={getattr(viewer, '_incremental_finalize', None)}")
    print(f"_v2_finalize_incremental={viewer.__dict__.get('_v2_finalize_incremental')}")
    print("\n（耗时见上方 [finish-render] 日志行；DOM token 无缺失即增量收尾安全）")


if __name__ == "__main__":
    main()
