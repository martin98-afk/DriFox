# -*- coding: utf-8 -*-
"""真链路复现：v2 下历史会话 assistant 卡正文空白。

从真实 sessions.db（mode=ro）取 assistant 消息，走完整 MessageCard.set_content
→ ensure_rendered → CodeWebViewer v2 渲染链（offscreen WebEngine），随后回读：
  - viewer 的 v2 状态（_render_v2 / _v2_last / _needs_full_render）
  - JS 侧 typeof patchBlocks / _v2AfterPatch / 两容器 innerHTML 长度
  - page._dom_text_len（sentinel 用的同一指标）

用法：uv run python tests/debug/_probe_history_v2_blank.py [消息条数]
"""

import os
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DRIFOX_DATA_DIR", str(_ROOT / ".drifox-test"))
# 用真 windows 平台：offscreen 下 QWebEngineProfile 初始化直接 0xC0000005
os.environ.pop("QT_QPA_PLATFORM", None)
# 注意：不设 DRIFOX_QT_RENDERER（那会强制纯 Qt 通道绕开 v2，与真机路径不符）
# v2 默认开，无需显式设；对照时传 DRIFOX_RENDER_V2=0

from PyQt5.QtCore import QCoreApplication, QEventLoop, Qt, QTimer  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

# 渲染配置 → 环境变量（与 main.py 同款，QT_OPENGL 等在 QApplication 前生效）
from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

apply_render_env(default_config_path())

app = QApplication.instance() or QApplication(sys.argv)

# CodeWebViewer 依赖共享 Profile（main.py 延迟启动里同款调用）
from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile(parent=app)

DB = _ROOT / ".drifox" / "sessions.db"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def spin(seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec_()


def pick_messages():
    from app.core.store.serde import deserialize

    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute(
        "select session_id, messages from sessions where messages is not null"
        " order by rowid desc limit 40"
    )
    out = []
    for sid, raw in cur.fetchall():
        try:
            msgs = deserialize(raw)
        except Exception:
            continue
        # 历史加载的真实形态：content 是序列化后的 str（含协议标签）。
        # 取长消息（>1500 字符），最接近用户截图的空白卡。
        for m in reversed(msgs):
            if not (isinstance(m, dict) and m.get("role") == "assistant" and m.get("content")):
                continue
            c = m.get("content")
            size = len(c) if isinstance(c, str) else sum(len(str(b)) for b in c) if isinstance(c, list) else 0
            if size > 1500:
                out.append((sid, m))
                break
        if len(out) >= N:
            break
    con.close()
    return out


JS_SNAPSHOT = """
(function () {
  function info(id) {
    var el = document.getElementById(id);
    if (!el) return 'missing';
    return String(el.innerHTML.length) + '/' + String(el.children.length);
  }
  var ts = document.getElementById('tool-section');
  var tcEl = document.getElementById('tool-content');
  var firstCard = tcEl && tcEl.firstElementChild ? String(tcEl.firstElementChild.outerHTML).slice(0, 150) : 'empty';
  return [
    typeof window.patchBlocks,
    typeof window._v2AfterPatch,
    info('content-placeholder'),
    info('tool-content'),
    String(document.body ? document.body.children.length : -1),
    String(document.documentElement ? document.documentElement.outerHTML.length : -1),
    typeof window.updateContent,
    typeof window.reorganizeContent,
    ts ? (ts.getAttribute('data-collapsed') || 'no-attr') + '/' + ts.style.display : 'no-ts',
    String(document.querySelectorAll('#tool-content [data-streaming="true"]').length),
    firstCard
  ].join('|');
})()
"""


def probe_one(idx: int, sid: str, msg: dict) -> None:
    from app.core.conversation.message_content import content_to_markdown, ensure_content_blocks

    blocks = ensure_content_blocks(msg.get("content"))
    md = content_to_markdown(blocks)
    print("=" * 78)
    print(f"[{idx}] session={sid[:12]} blocks={len(blocks)} md_len={len(md)}")

    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant", timestamp=msg.get("timestamp"))
    card._is_history = True
    card.resize(700, 400)
    card.set_content(msg.get("content"))
    card.show()
    card.ensure_rendered()
    spin(0.2)

    viewer = getattr(card, "viewer", None)
    if viewer is None:
        print("  viewer=None（懒渲染未建）")
        return
    print(f"  viewer={type(viewer).__name__}")
    if not hasattr(viewer, "_render_v2"):
        print("  非 CodeWebViewer（纯 Qt 通道），跳过")
        return
    spin(8.0)  # 等骨架 setHtml + _on_js_ready + 渲染链

    snap_cb = []
    if viewer.page():
        viewer.page().runJavaScript(JS_SNAPSHOT, lambda r: snap_cb.append(r))
        spin(0.8)

    js = snap_cb[0] if snap_cb else "no-callback"
    parts = str(js).split("|")
    print(f"  v2_state: _render_v2={viewer._render_v2} _v2_last={'set' if viewer._v2_last else 'None'}"
          f" needs_full={viewer._needs_full_render} js_ready={viewer._is_js_ready}"
          f" streaming={viewer._streaming} deferred={viewer._render_deferred}"
          f" is_history={getattr(viewer, '_is_history', '?')}")
    print(f"  md={len(viewer._markdown_text or '')} last_r={len(viewer._last_rendered_markdown or '')}")
    names = ["patchBlocks", "_v2AfterPatch", "content", "tool", "body_kids", "html_size",
             "updateContent", "reorganize", "collapsed/display", "streaming_nodes", "tc_first"]
    for nm, val in zip(names, parts):
        print(f"    {nm}={val}")
    dom_len = getattr(viewer.page(), "_dom_text_len", "n/a") if viewer.page() else "no-page"
    print(f"  page._dom_text_len={dom_len}")

    # ── 手动触发一轮真 v2 渲染（复刻 _perform_update_v2 自身），看能否落地 ──
    try:
        blocks_v2 = viewer._v2_blocks()
        ctx_real = viewer._v2_render_ctx()
        from app.widgets.card_viewers import _HISTORY_TOOL_CAPS

        print(f"  pre-manual: js_ready={viewer._is_js_ready} visible={viewer.isVisible()}"
              f" blocks={len(blocks_v2) if blocks_v2 else None}"
              f" cache_size={len(viewer._block_cache)}")
        print(f"  real ctx: compact={ctx_real.compact} lazy_body={ctx_real.lazy_body}"
              f" heavy_caps={ctx_real.heavy_caps} fingerprints={len(ctx_real.tool_fingerprints)}"
              f" _HISTORY_TOOL_CAPS={_HISTORY_TOOL_CAPS}")
        # 掏缓存：命中那条的产物是什么
        from app.widgets.render.block_render import block_key

        for b in blocks_v2:
            k = block_key(b, ctx_real, True)
            hit = viewer._block_cache._items.get(k[1])
            print(f"  cache[key={k[1][:12]}] hit={'None' if hit is None else f'html_len={len(hit.html)} kind={hit.kind} home={hit.home}'}")
        # 带 cache 重渲一次（真机路径），对比无 cache 产物
        from app.widgets.render import render_message as _rm

        mr_cache = _rm(blocks_v2, ctx_real, cache=viewer._block_cache)
        mr_nocache = _rm(blocks_v2, ctx_real)
        print(f"  with_cache: {[len(x.html) for x in mr_cache.content]}"
              f" nocache: {[len(x.html) for x in mr_nocache.content]}")
    except Exception:
        import traceback

        traceback.print_exc()

    # ── 纯函数层对照：真机同款 ctx 下 render_message / diff_message 产物 ──
    try:
        from app.widgets.render import BlockCache, RenderCtx, diff_message, render_message

        ctx = RenderCtx(
            streaming=False,
            compact=getattr(viewer, "_tool_compact_mode", False),
            lazy_body=getattr(viewer, "_tool_lazy_body", False),
            heavy_caps=None,
            tool_fingerprints=frozenset(getattr(viewer, "_finished_tool_fingerprints", ()) or ()),
        )
        mr = render_message(blocks, ctx)
        ops_c, ops_t = diff_message(None, mr)
        print(f"  pure: content={len(mr.content)} tool={len(mr.tool)}"
              f" html_sizes={[len(b.html) for b in (*mr.content, *mr.tool)]}")
        print(f"  pure ops: c_empty={ops_c.is_empty} c_base={ops_c.base} c_del={ops_c.delete}"
              f" c_items={len(ops_c.items)} | t_empty={ops_t.is_empty} t_items={len(ops_t.items)}")
        ctx_h = RenderCtx(
            streaming=False,
            compact=getattr(viewer, "_tool_compact_mode", False),
            lazy_body=getattr(viewer, "_tool_lazy_body", False),
            heavy_caps=(1200, 4000, 20000),
            tool_fingerprints=frozenset(getattr(viewer, "_finished_tool_fingerprints", ()) or ()),
        )
        mr_h = render_message(blocks, ctx_h)
        ops_ch, ops_th = diff_message(None, mr_h)
        print(f"  pure(heavy): content={len(mr_h.content)} tool={len(mr_h.tool)}"
              f" html_sizes={[len(b.html) for b in (*mr_h.content, *mr_h.tool)]}"
              f" c_empty={ops_ch.is_empty} c_items={len(ops_ch.items)}")
    except Exception as e:
        import traceback

        traceback.print_exc()

    # ── 折叠链路诊断：手动执行 _on_js_ready 同款折叠 JS，看属性是否吃得住 ──
    try:
        results = []
        fold_js = (
            "(function(){var _ts=document.getElementById('tool-section');"
            "var _sep=document.getElementById('tool-separator');"
            "var _pre=_ts?_ts.getAttribute('data-collapsed'):'no-ts';"
            "if(_ts){if(typeof _beginToolSectionTransition==='function')"
            "_beginToolSectionTransition();_ts.setAttribute('data-collapsed','true');}"
            "if(_sep)_sep.setAttribute('aria-expanded','false');"
            "return _pre + '->' + (_ts?_ts.getAttribute('data-collapsed'):'no-ts');})()"
        )
        if viewer.page() is not None:
            viewer.page().runJavaScript(fold_js, lambda r: results.append(r))
            spin(0.8)
        print(f"  manual fold -> {results}")
    except Exception:
        import traceback

        traceback.print_exc()
    card.deleteLater()


def main() -> None:
    msgs = pick_messages()
    print(f"picked {len(msgs)} assistant messages (v2={os.environ.get('DRIFOX_RENDER_V2', 'default-on')})")
    for i, (sid, m) in enumerate(msgs):
        try:
            probe_one(i, sid, m)
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"  PROBE-ERR: {e}")
    spin(0.5)


if __name__ == "__main__":
    main()
