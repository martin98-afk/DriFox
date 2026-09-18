# -*- coding: utf-8 -*-
"""验证：append_tool_result 的就地插位（_insertByOrder）能否把"沉底的完成块"拉回正确位置。

构造（绕开完整流式时序，直接摆 DOM）：
1. 简洁模式，注入 markdown：思考1 + 思考2 → 渲染后迁移进 #tool-content（od=0、2）
2. 手动向 #tool-content 末尾 appendChild 一个"沉底完成块"（tid=tool_x，od=1，
   class=cm-collapsible tool-block，非流式态）——模拟 restore appendChild 沉底后的形态
3. 调 card.append_tool_result(tool_x) → 命中分支 2（existing 非流式态）→ 原地更新 + 就地插位
4. 断言：tool_x 物理位置回到 [1]（think1 之后、think2 之前）

运行：python tests/debug/tool_insert_order_check.py
"""

import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

PROBE = """
(function(){
  var tc = document.getElementById('tool-content');
  if(!tc) return {err:'no tool-content'};
  var out = [];
  for (var i=0;i<tc.children.length;i++){
    var k = tc.children[i];
    out.push([k.getAttribute('data-tool-call-id'),
              (k.getAttribute('data-block-key')||'').slice(0,10),
              k.getAttribute('data-order')]);
  }
  return {order: out};
})()
"""


def main() -> None:
    app = QApplication(sys.argv)
    init_shared_web_profile()

    card = MessageCard(role="assistant")
    card.resize(700, 520)
    card.show()

    def wait_js() -> None:
        v = card.viewer
        if v is None or not getattr(v, "_is_js_ready", False):
            QTimer.singleShot(200, wait_js)
            return
        v.page().runJavaScript("window._toolCompactMode=true;")
        card.append_text("开始。\n\n")
        card.start_new_thinking_block()
        card.append_reasoning("思考一")
        card.start_new_thinking_block()
        card.append_reasoning("思考二")
        card.append_text("两段思考之间的正文。\n\n")
        QTimer.singleShot(600, seed_sunk_block)

    def seed_sunk_block() -> None:
        # 手动把"完成块"摆到 #tool-content 最底（模拟 restore appendChild 沉底态）
        v = card.viewer
        seed_js = """
        (function(){
          var tc = document.getElementById('tool-content');
          if (!tc) return 'no tc';
          var d = document.createElement('div');
          d.className = 'cm-collapsible tool-block';
          d.setAttribute('data-tool-call-id', 'tool_x');
          d.setAttribute('data-order', '1');
          d.innerHTML = '<details class="cm-collapsible" open><summary class="cm-collapsible__summary">✔ search</summary><div class="cm-collapsible__body">r</div></details>';
          tc.appendChild(d);
          return 'ok';
        })()
        """
        v.page().runJavaScript(seed_js, lambda r=None: (print(f"[seed] {r}"), QTimer.singleShot(300, probe_before)))

    def probe_before() -> None:
        probe("修复前", run_fix)

    def run_fix() -> None:
        # 单测 _insertByOrder 本身：patch 掉渲染（模拟"渲染没跑/差量吞掉"的拍差场景），
        # 只让 append_tool_result 的注入 JS 生效。锚点注册使数据层 od=1（思考一之后）。
        ref = card._content_data[1] if len(card._content_data) > 1 else None
        if ref is not None:
            card._tool_anchor_refs["tool_x"] = ref
            card._tool_call_order["tool_x"] = 0
        v = card.viewer
        v._schedule_render = lambda *a, **k: None  # 拍差模拟：渲染缺席
        card.append_tool_result(
            tool_name="search", result="结果内容", tool_call_id="tool_x"
        )
        QTimer.singleShot(800, lambda: probe("修复后", judge))

    def probe(label: str, done=None) -> None:
        v = card.viewer
        v.page().runJavaScript(PROBE, lambda r=None, l=label, d=done: (report(l, r), d() if d else None))

    def report(label: str, r) -> None:
        rows = (r or {}).get("order") if isinstance(r, dict) else None
        print(f"----- [{label}] -----")
        if rows is None:
            print("  probe fail:", r)
            return
        for i, row in enumerate(rows):
            print(f"  [{i}] tid={row[0]} bk={row[1]} od={row[2]}")

    def judge() -> None:
        v = card.viewer
        v.page().runJavaScript(PROBE, lambda r=None: (final(r), app.quit()))

    def final(r) -> None:
        rows = (r or {}).get("order") if isinstance(r, dict) else None
        print("===== 判定 =====")
        if not rows:
            print(">>> 无快照")
            app.quit()
            return
        pos = {row[0]: i for i, row in enumerate(rows)}
        od = {row[0]: row[2] for i, row in enumerate(rows)}
        print(f"  tool_x 位置={pos.get('tool_x')} od={od.get('tool_x')} 全序={rows}")
        # 期望：think(od=0) < tool_x(od=1) < think(od=2)，tool_x 物理位于两思考之间
        ok = (
            pos.get("tool_x") is not None
            and pos.get("tool_x") != max(pos.values())
            and len(rows) >= 3
        )
        print(">>> 通过：完成块被就地插位拉回" if ok else ">>> 失败：完成块仍在物理底部")
        app.quit()

    card.start_streaming_anim()
    card.ensure_rendered()
    QTimer.singleShot(300, wait_js)
    QTimer.singleShot(30000, app.quit)
    app.exec_()


if __name__ == "__main__":
    main()
