# -*- coding: utf-8 -*-
"""tests/ui 首批 4 用例（S3b）：池生命周期 / 缩略图闭包 / 切换内存回落 / 滚动净泄漏。

运行：``pytest tests/ui -m ui``（默认收集排除，显式 -m ui 才跑）。
硬约束：A 档出屏窗口 / 零 runJavaScript / ensure_rendered 显式前置 / 不读 _last_rendered_html。
"""

from __future__ import annotations

import gc

import pytest

pytestmark = [pytest.mark.ui]

_MD = "# 渲染验证\n\n```python\nprint('x')\n```\n\n正文段落。\n"


def _host(qtbot):
    """出屏宿主容器：卡片可见性门控（_is_effectively_visible）依赖顶层窗口可见。"""
    from tools.ui_driver import place_offscreen
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    host.setLayout(lay)
    place_offscreen(host)
    qtbot.addWidget(host)
    return host


def _make_assistant_card(host, qtbot, text: str):
    """建 assistant 卡 → 灌内容 → ensure_rendered → 等 JS ready + 高度回报。"""
    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant")
    host.layout().addWidget(card)
    card.resize(700, 400)
    card.update_content(text)
    card.ensure_rendered()
    qtbot.waitUntil(
        lambda: getattr(card, "viewer", None) is not None
        and getattr(card.viewer, "_is_js_ready", False),
        timeout=30000,
    )
    qtbot.wait(1200)  # 骨架渲染 + 高度回报落定
    assert card._is_effectively_visible()
    return card


# ── 用例 1 ──

# 独立探针源码：在子进程中构建 6 张卡并 cleanup，检验「cleanup 入池」。
# 用子进程的原因：现状下 cleanup 走 viewer 销毁路径，会触发 WebEngine 销毁
# 竞态崩溃（S2 POC 崩族①/②同源，进程级死亡）——pytest 的 xfail 只能表达
# 断言失败，无法表达进程崩溃；子进程隔离后崩溃=非零退出码，语义等价。
_POOL_PROBE_SRC = r'''
import os, sys
import tempfile

os.environ.setdefault("DRIFOX_DATA_DIR", tempfile.mkdtemp(prefix="drifox_pool_probe_"))
sys.path.insert(0, r"D:/work/DriFox")

from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget
from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401 — 先于 QApplication

app = QApplication([])
from app.widgets.message_card import MessageCard
from tools.ui_driver import place_offscreen

host = QWidget()
lay = QVBoxLayout(host)
host.setLayout(lay)
place_offscreen(host)

MD = "# 池生命周期\n\n```python\nprint(1)\n```\n"
cards = []
for _ in range(6):
    card = MessageCard(role="assistant")
    lay.addWidget(card)
    card.resize(700, 400)
    card.update_content(MD)
    card.ensure_rendered()
    cards.append(card)

deadline = __import__("time").monotonic() + 30
while __import__("time").monotonic() < deadline:
    app.processEvents()
    ready = all(
        getattr(c, "viewer", None) is not None and getattr(c.viewer, "_is_js_ready", False)
        for c in cards
    )
    if ready:
        break
    __import__("time").sleep(0.05)

for c in cards:
    c.cleanup()
for _ in range(30):
    app.processEvents()
    __import__("time").sleep(0.02)

from app.widgets.webview_pool import WebViewPool

pool = WebViewPool.get_instance()
size = pool.size(light=False)
pids = pool.pids()
print(f"POOL_SIZE={size}")
print(f"POOL_PIDS={len(pids)}")
ok = 1 <= size <= 4 and len(pids) >= 1
print("POOL_OK" if ok else "POOL_NOT_OK")
sys.exit(0 if ok else 1)
'''


@pytest.mark.xfail(strict=True, reason="等方案 4：cleanup 入池（当前 cleanup 销毁 viewer 不回池，且销毁竞态会崩子进程）")
def test_pool_lifecycle_after_cleanup(tmp_path):
    """cleanup() 后 viewer 应入池（池桶 size∈[1,4] 且 pids 非空）。

    子进程隔离执行：现状下 cleanup 销毁 viewer 触发 WebEngine 销毁竞态
    （崩族①/②同源）直接杀进程——用退出码表达「未达标」，xfer 语义不变；
    方案 4 落地后子进程打印 POOL_OK 且 returncode 0 → 本用例转绿。
    """
    import subprocess
    import sys as _sys

    script = tmp_path / "pool_probe.py"
    script.write_text(_POOL_PROBE_SRC, encoding="utf-8")
    r = subprocess.run(
        [_sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=r"D:/work/DriFox",
    )
    tail = (r.stdout or "").strip().splitlines()[-4:]
    assert r.returncode == 0, f"子进程未达标 rc={r.returncode} tail={tail}"
    assert "POOL_OK" in (r.stdout or ""), f"未入池：{tail}"


# ── 用例 3 ──


def test_session_switch_memory_rebounds(ui_app, ui_session, qtbot, mem_sampler, tmp_path):
    """双会话（其一含 5MB 级图片附件）切换 ×10：Private 中位数回落 ≤ 基线+30%。"""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QImage, QPainter

    mw = ui_session
    sm = mw.session_manager

    # 会话 B：文本 + 5MB 级 PNG 附件（约 5MB 文件 → 解码 ~33MB 位图）
    sess_b = sm.create_new_session()
    for i in range(20):
        sess_b.messages.append({"role": "user", "content": f"B 问题 {i}"})
        sess_b.messages.append({"role": "assistant", "content": "B 回答：" + "内容填充。" * 20})
    big = QImage(3840, 2160, QImage.Format_ARGB32)
    painter = QPainter(big)
    painter.fillRect(big.rect(), Qt.darkRed)
    painter.end()
    png_b = tmp_path / "b_big.png"
    assert big.save(str(png_b), "PNG")
    assert png_b.stat().st_size > 3 * 1024 * 1024, "测试前置：PNG 应 ≥3MB"
    sess_b.messages[0]["_image_attachments"] = [str(png_b)]

    def _switch(sess):
        for idx, s in enumerate(sm.sessions):
            if s is sess:
                sm.switch_to_session(idx)
                break
        mw._display_current_session()
        mw._release_inactive_session_messages()

    mem_sampler.sample("baseline")
    session_a = sm.get_current_session()
    for i in range(10):
        _switch(sess_b if i % 2 == 0 else session_a)
        if (i + 1) % 3 == 0:
            gc.collect()
        mem_sampler.sample(f"switch_{i}")
        qtbot.wait(400)
    _switch(session_a)
    gc.collect()
    qtbot.wait(5000)  # 终值窗口：切完 5s 内取最小
    tail = [mem_sampler.sample(f"tail_{j}")["main_private_mb"] for j in range(3)]

    base = mem_sampler.points[0]["main_private_mb"]
    import statistics

    mid = statistics.median(
        [p["main_private_mb"] for p in mem_sampler.points[1:] if isinstance(p.get("main_private_mb"), (int, float))]
    )
    tail_min = min(v for v in tail if v is not None)
    quota = mw._effective_max_rendered_cards()
    assert mw._rendered_card_count <= quota, f"渲染数 {_rendered(mw)} 超配额 {quota}"
    assert pool_size(mw) <= 8, f"池 size {pool_size(mw)} > 8"
    # 中位数口径（S1-r 建议⑤），终值取切完 5s 窗口最小值
    assert tail_min <= base * 1.30 or mid <= base * 1.30, (
        f"内存未回落：base={base:.0f}MB median={mid:.0f}MB tail_min={tail_min:.0f}MB"
    )


def _rendered(mw):
    return mw._rendered_card_count


def pool_size(mw):
    from app.widgets.webview_pool import WebViewPool

    return WebViewPool.get_instance().size(light=False)


# ── 用例 4 ──


def test_scroll_no_net_leak(ui_app, ui_session, qtbot, mem_sampler):
    """20 批会话滚底 → 上下往返 ×10：无净泄漏 + evict 链存活。"""
    from tools.ui_driver import wait_idle, wait_signal

    mw = ui_session
    assert len(mw._message_batch) >= 20, f"前置失败：批次数 {len(mw._message_batch)} < 20"
    sb = _find_bar(mw)
    assert sb is not None, "未找到滚动条"

    mem_sampler.sample("scroll_start")
    base = mem_sampler.points[-1]["main_private_mb"]

    sb.setValue(sb.maximum())
    wait_idle(600)
    for i in range(10):
        sb.setValue(0)
        wait_signal(sb.valueChanged, timeout_ms=3000)
        wait_idle(500)
        sb.setValue(sb.maximum())
        wait_signal(sb.valueChanged, timeout_ms=3000)
        wait_idle(500)
        # 懒队列排空（S1-r：_pending_lazy_cards 不留尾巴）
        qtbot.waitUntil(lambda: not mw._pending_lazy_cards, timeout=10000)
        mem_sampler.sample(f"round_{i}")

    quota = mw._effective_max_rendered_cards()
    from app.widgets.webview_pool import WebViewPool

    stats = WebViewPool.get_instance().stats
    final = mem_sampler.sample("final")["main_private_mb"]
    net = final - base
    assert mw._rendered_card_count <= quota, f"渲染数 {mw._rendered_card_count} 超配额 {quota}"
    assert len(mw._unloaded_pids) <= 32, f"_unloaded_pids 泄漏：{len(mw._unloaded_pids)}"
    assert stats["evicted"] > 0, f"evict 链未存活：stats={stats}"
    assert net < 50, f"滚动净增 {net:.1f}MB ≥ 50MB"


def _find_bar(mw):
    from PyQt5.QtWidgets import QScrollBar

    area = getattr(mw, "chat_scroll_area", None)
    if area is None:
        return None
    for child in area.findChildren(QScrollBar):
        if child.orientation() == Qt_ORIENTATION_VERTICAL:
            return child
    return None


from PyQt5.QtCore import Qt as Qt_ORIENTATION  # noqa: E402 — 供 _find_bar 使用
# ── 用例 2 ──


@pytest.mark.xfail(strict=True, reason="等方案 2：闭包改持小图（当前持全尺寸原始 pixmap）")
def test_thumb_closure_holds_scaled_only(ui_app, qtbot, tmp_path):
    """缩略图点击闭包只允许持有缩放后小图（≤800px 宽）。

    现状：_build_image_thumb 的 ``mousePressEvent = lambda e, pm=pixmap: ...``
    闭包 defaults 持有**全尺寸原始 pixmap**（3840×2160 ≈ 33MB RGBA/张）。
    强制 A 档：QPixmap 像素操作在 offscreen 下崩溃（S2）。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QImage, QPainter
    from PyQt5.QtWidgets import QLabel

    from app.widgets.message_card import MessageCard

    big = QImage(3840, 2160, QImage.Format_ARGB32)
    painter = QPainter(big)
    painter.fillRect(big.rect(), Qt.blue)
    painter.end()
    png = tmp_path / "big.png"
    assert big.save(str(png), "PNG")

    host = _host(qtbot)
    card = MessageCard(role="user")
    host.layout().addWidget(card)
    card.show()
    card.set_image_attachments([str(png)])
    qtbot.wait(300)

    thumbs = [x for x in card._image_strip.findChildren(QLabel) if x.pixmap() is not None and not x.pixmap().isNull()]
    assert thumbs, "缩略图条未构建"
    for t in thumbs:
        handler = getattr(t, "mousePressEvent", None)
        defaults = getattr(handler, "__defaults__", None) or ()
        for pm in defaults:
            if hasattr(pm, "width"):
                assert pm.width() <= 800, f"点击闭包持有全尺寸 pixmap（{pm.width()}px）"


