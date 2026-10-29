# -*- coding: utf-8 -*-
"""真链路探针：历史会话 assistant 卡片（含真实工具结果）的折叠态。

与 _probe_history_fold.py 的区别：不再猜「哪条 assistant 消息含工具」，
而是用生产同款 ``group_messages_for_display`` 组 batch（assistant + tool 交替），
再走 ``render_batch_to_assistant_card``（append_text / append_tool_result /
finish_streaming(history=True)）建卡，最后 ensure_rendered 让 viewer 起来。

输出：#tool-section[data-collapsed] / display、#tool-content 各块身份、
正文是否残留 .tool-block / [data-tool-call-id]（home 判错的直接证据）。

用法：uv run python tests/debug/_probe_history_batch_fold.py [batch 条数]
"""

import json
import os
import sqlite3
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

DB = _ROOT / ".drifox" / "sessions.db"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def spin(seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec_()


def pick_batches():
    from app.core.conversation.message_content import group_messages_for_display
    from app.core.store.serde import deserialize

    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute(
        "select session_id, messages from sessions where messages is not null"
        " order by rowid desc limit 400"
    )
    out = []
    for sid, raw in cur.fetchall():
        try:
            msgs = deserialize(raw)
        except Exception:
            continue
        try:
            batches = group_messages_for_display(msgs)
        except Exception:
            continue
        for batch in batches:
            if not batch or batch[0].get("role") != "assistant":
                continue
            tools = [m for m in batch if m.get("role") == "tool" and m.get("content")]
            if not tools:
                continue
            out.append((sid, batch))
            break
        if len(out) >= N:
            break
    con.close()
    return out


JS_SNAPSHOT = """
(function () {
  function list(sel, root) {
    var out = [];
    var els = (root || document).querySelectorAll(sel);
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      out.push([
        el.tagName, el.className || '',
        el.getAttribute('data-streaming') || '-',
        el.getAttribute('data-tool-call-id') || '-',
        el.getAttribute('data-expanded') || '-',
        String(el.textContent || '').slice(0, 24)
      ].join('~'));
    }
    return out;
  }
  var ts = document.getElementById('tool-section');
  var tc = document.getElementById('tool-content');
  var cp = document.getElementById('content-placeholder');
  return JSON.stringify({
    collapsed: ts ? (ts.getAttribute('data-collapsed') || 'no-attr') : 'no-ts',
    display: ts ? ts.style.display : '-',
    aria: (document.getElementById('tool-separator') || {}).ariaExpanded || '-',
    tc_count: tc ? tc.children.length : -1,
    tc_blocks: list('#tool-content .tool-block, #tool-content .think-block, #tool-content .think-compact', tc),
    cp_blocks: list('.tool-block, .think-block, .think-compact, [data-tool-call-id]', cp),
    streaming_nodes: document.querySelectorAll('[data-streaming="true"]').length
  }, null, 1);
})()
"""


def probe_one(idx: int, sid: str, batch) -> None:
    print("=" * 78)
    roles = [m.get("role") for m in batch]
    print(f"[{idx}] session={sid[:12]} batch={roles}")
    from app.widgets import ui_helpers
    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant", timestamp=batch[0].get("timestamp"))
    card.resize(720, 420)
    # 真实路径：卡片先建、内容通过 append_* 灌入（immediate_render=False）
    ui_helpers.render_batch_to_assistant_card(card, batch, immediate_render=False)
    print(f"  pre-render: streaming={card._streaming} finished={card._streaming_finished}"
          f" content_blocks={len(card._content_data) if isinstance(card._content_data, list) else 'str'}"
          f" pending={'yes' if card._pending_content is not None else 'no'}")
    card.show()
    card.ensure_rendered()
    spin(0.3)
    viewer = getattr(card, "viewer", None)
    if viewer is None or not hasattr(viewer, "_render_v2"):
        print(f"  viewer={type(viewer).__name__ if viewer else None}，跳过")
        card.deleteLater()
        return
    print(f"  viewer: is_history={getattr(viewer, '_is_history', '?')} streaming={viewer._streaming}"
          f" compact={card.viewer._tool_compact_mode} v2={viewer._render_v2}")
    spin(9.0)
    got = []
    if viewer.page():
        viewer.page().runJavaScript(JS_SNAPSHOT, lambda r: got.append(r))
        spin(1.2)
    print(f"  viewer_after: streaming={viewer._streaming} is_history={getattr(viewer, '_is_history', '?')}")
    print(f"  dom: {got[0] if got else 'no-callback'}")

    # ── 池化复用路径对照：归还 → 建第二张同内容历史卡（复刻虚拟滚动回收重建）──
    try:
        from app.widgets.webview_pool import WebViewPool

        pool = WebViewPool.get_instance()
        released = pool.release(viewer, light=False)
        card.viewer = None
        print(f"  [pool] released={released} idle={pool.size(light=False)}")

        card2 = MessageCard(role="assistant", timestamp=batch[0].get("timestamp"))
        card2.resize(720, 420)
        ui_helpers.render_batch_to_assistant_card(card2, batch, immediate_render=False)
        card2.show()
        card2.ensure_rendered()
        spin(0.3)
        v2 = getattr(card2, "viewer", None)
        reused = v2 is viewer
        print(f"  [pool] card2 viewer={'池实例' if reused else '新建'} "
              f"is_history={getattr(v2, '_is_history', '?')} js_ready={getattr(v2, '_is_js_ready', '?')}"
              f" applied={getattr(v2, '_history_collapse_applied', '?')}")
        if v2 is not None:
            spin(7.0)
            got2 = []
            if v2.page():
                v2.page().runJavaScript(JS_SNAPSHOT, lambda r: got2.append(r))
                spin(1.2)
            s = json.loads(got2[0]) if got2 and got2[0] else {}
            print(f"  [pool] card2 dom collapsed={s.get('collapsed')} display={s.get('display')}"
                  f" tc_count={s.get('tc_count')}")
        card2.deleteLater()
    except Exception:
        import traceback

        traceback.print_exc()

    card.deleteLater()


def main() -> None:
    batches = pick_batches()
    print(f"picked {len(batches)} assistant batches with tool results (v2={os.environ.get('DRIFOX_RENDER_V2', 'default-on')})")
    for i, (sid, batch) in enumerate(batches):
        try:
            probe_one(i, sid, batch)
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"  PROBE-ERR: {e}")
    spin(0.5)


if __name__ == "__main__":
    main()
