"""长回复、图片落盘与背景预读的内存预算。显式指定本文件运行。"""

import base64
import gc
import threading
import tracemalloc

import pytest


pytestmark = pytest.mark.perf
MIB = 1024 * 1024


def test_long_markdown_stream_retained_heap(qapp):
    from app.widgets import markdown_block_viewer as renderer

    renderer._plain_text_blocks("warm\n\nup")
    renderer._TEXT_BLOCK_CACHE.clear()
    renderer._HTML_PREFIX_CACHE.clear()
    gc.collect()
    tracemalloc.start()
    try:
        body = "长回复正文 " * 5000
        for i in range(180):
            body += f"\n\n第{i}段。"
            result = renderer._plain_text_blocks(body + "\n\n尾部文字")
            assert str(i) in result[0]["html"]
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
        print(f"长回复渲染持有 {retained / MIB:.2f} MiB，峰值 {peak / MIB:.2f} MiB")
        assert retained < 10 * MIB
        assert result == renderer._plain_text_blocks_uncached(body + "\n\n尾部文字")
    finally:
        tracemalloc.stop()
        renderer._TEXT_BLOCK_CACHE.clear()
        renderer._HTML_PREFIX_CACHE.clear()


def test_save_distinct_image_blocks_peak(tmp_path):
    from app.core.store.session_repository import _persist_vision_image_blocks_impl

    messages = []
    for index in range(24):
        encoded = base64.b64encode(bytes([index]) * MIB).decode("ascii")
        url = f"data:image/png;base64,{encoded}"
        messages.append({"content": [{"type": "image_url", "image_url": {"url": url}}]})

    tracemalloc.start()
    try:
        _persist_vision_image_blocks_impl(messages, "bench", tmp_path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(f"批量图片落盘额外峰值 {peak / MIB:.2f} MiB")
    assert peak < 6 * MIB
    for index, msg in enumerate(messages):
        ref = msg["content"][0]["image_ref"]
        path = tmp_path / "screenshots" / "persisted" / "bench" / ref["file"]
        assert path.read_bytes() == bytes([index]) * MIB


def test_4k_prefetch_queue(tmp_path, monkeypatch):
    from PyQt5.QtGui import QImage

    from app.widgets.anim_background import AnimationSource

    path = tmp_path / "4k.png"
    image = QImage(3840, 2160, QImage.Format_RGB32)
    image.fill(0xFF335599)
    assert image.save(str(path))
    del image

    # 等生产者明确进入满队列等待，再统计像素缓冲；慢机器也能区分一帧与三帧。
    full = threading.Event()
    start = AnimationSource._start_prefetch

    def observed_start(source):
        wait = source._prefetch_space.wait

        def wait_for_space():
            full.set()
            return wait()

        source._prefetch_space.wait = wait_for_space
        start(source)

    monkeypatch.setattr(AnimationSource, "_start_prefetch", observed_start)
    source = AnimationSource(str(path))
    try:
        assert full.wait(10)
        with source._prefetch_lock:
            count = len(source._prefetch)
            pixel_bytes = sum(frame.sizeInBytes() for frame, _, _ in source._prefetch)
        print(f"4K 预读 {count} 帧，像素缓冲 {pixel_bytes / MIB:.2f} MiB")
        assert count == 1
        assert source.advance(1_000_000)
    finally:
        source.stop_prefetch()
        source._prefetch_thread.join(3)
