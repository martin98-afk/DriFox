# -*- coding: utf-8 -*-
"""Gateway 出站附件提取与原生发送调度单测。

覆盖：
- ``_extract_markdown_files``：本地文件链接识别、图片/远程/不存在文件不误吞
- ``GatewayService._deliver_attachments``：原生发送成功不回落、失败回退 Gitee 链接
"""
import asyncio

import pytest

from system_im_core.attachments import _extract_markdown_files, _extract_markdown_images


# ==================== 文件提取 ====================

def test_extract_local_file_link_removed(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4")
    cleaned, paths = _extract_markdown_files(f"报告已生成 📎 [report.pdf]({f})")
    assert paths == [str(f)]
    assert "report.pdf]" not in cleaned
    assert "报告已生成" in cleaned


def test_extract_skips_missing_file(tmp_path):
    missing = tmp_path / "nope.pdf"
    text = f"📎 [nope.pdf]({missing})"
    cleaned, paths = _extract_markdown_files(text)
    assert paths == []
    assert cleaned == text  # 原样保留，交给平台当裸文本显示


def test_extract_skips_remote_url():
    text = "📎 [report.pdf](https://example.com/report.pdf)"
    cleaned, paths = _extract_markdown_files(text)
    assert paths == []
    assert cleaned == text


def test_extract_skips_images(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    cleaned, paths = _extract_markdown_files(f"![图]({img})")
    assert paths == []
    assert cleaned == f"![图]({img})"


def test_extract_multiple_files_in_order(tmp_path):
    a = tmp_path / "a.csv"
    b = tmp_path / "b.zip"
    a.write_text("x")
    b.write_bytes(b"PK")
    _cleaned, paths = _extract_markdown_files(f"📎 [a.csv]({a}) 和 📎 [b.zip]({b})")
    assert paths == [str(a), str(b)]


def test_image_and_file_extraction_split_by_kind(tmp_path):
    """图片与文件各归各的通道，互不吞并。"""
    img = tmp_path / "x.jpg"
    doc = tmp_path / "y.docx"
    img.write_bytes(b"\xff\xd8\xff")
    doc.write_bytes(b"PK")
    content = f"![截图]({img})\n📎 [合同]({doc})"
    cleaned, images = _extract_markdown_images(content)
    assert images == [str(img)]
    cleaned, files = _extract_markdown_files(cleaned)
    assert files == [str(doc)]
    assert "https://" not in cleaned


# ==================== 发送调度 ====================

class _FakeAdapter:
    """记录调用的假适配器；send_file/send_image 结果可配。"""

    def __init__(self, image_ok=True, file_ok=True, supports=True):
        self.image_calls = []
        self.file_calls = []
        self.text_calls = []
        self._image_ok = image_ok
        self._file_ok = file_ok
        self._supports = supports

    async def send_image(self, chat_id, path, **kwargs):
        self.image_calls.append(path)
        return _result(self._image_ok)

    async def send_file(self, chat_id, path, **kwargs):
        self.file_calls.append(path)
        return _result(self._file_ok)

    async def send(self, chat_id, content, **kwargs):
        self.text_calls.append(content)
        return _result(True)


def _result(ok):
    from app.gateway.base import SendResult

    return SendResult(success=ok, error=None if ok else "not supported")


class _FakeManager:
    def __init__(self, adapter):
        self._adapter = adapter

    def get_adapter(self, platform):
        return self._adapter


def _service_with(adapter):
    """构造最小 GatewayService 桩：只保留 _manager 与待测方法。"""
    import system_im_service as mod

    svc = mod.GatewayService.__new__(mod.GatewayService)
    svc._manager = _FakeManager(adapter)
    return svc


def test_deliver_uses_native_send(tmp_path, monkeypatch):
    img = tmp_path / "a.png"
    doc = tmp_path / "b.pdf"
    img.write_bytes(b"\x89PNG")
    doc.write_bytes(b"PDF")
    adapter = _FakeAdapter()
    svc = _service_with(adapter)
    asyncio.run(svc._deliver_attachments("feishu", "chat1", [str(img)], [str(doc)]))
    assert adapter.image_calls == [str(img)]
    assert adapter.file_calls == [str(doc)]
    assert adapter.text_calls == []  # 原生成功就不发链接


def test_deliver_falls_back_to_link_when_unsupported(tmp_path, monkeypatch):
    doc = tmp_path / "b.pdf"
    doc.write_bytes(b"PDF")
    adapter = _FakeAdapter(file_ok=False)
    svc = _service_with(adapter)

    # 回退上传走 Gitee；测试中替换为固定 URL，避免真实网络
    class _FakeUploader:
        @staticmethod
        def get_instance():
            return _FakeUploader()

        @staticmethod
        def is_configured():
            return True

        @staticmethod
        async def upload_file_async(path):
            return "https://example.com/x.pdf", None

    import app.gateway.utils.gitee_uploader as gitee_mod

    monkeypatch.setattr(gitee_mod, "GiteeUploader", _FakeUploader)
    asyncio.run(svc._deliver_attachments("qq", "chat1", [], [str(doc)]))
    assert adapter.file_calls == [str(doc)]
    assert adapter.text_calls and "https://example.com/x.pdf" in adapter.text_calls[0]


def test_deliver_falls_back_to_local_path_when_upload_unconfigured(tmp_path, monkeypatch):
    doc = tmp_path / "b.pdf"
    doc.write_bytes(b"PDF")
    adapter = _FakeAdapter(file_ok=False)
    svc = _service_with(adapter)

    class _FakeUploader:
        @staticmethod
        def get_instance():
            return _FakeUploader()

        @staticmethod
        def is_configured():
            return False

    import app.gateway.utils.gitee_uploader as gitee_mod

    monkeypatch.setattr(gitee_mod, "GiteeUploader", _FakeUploader)
    asyncio.run(svc._deliver_attachments("qq", "chat1", [], [str(doc)]))
    # 未配置 Gitee：仍发一条通知，至少让用户知道文件在哪
    assert adapter.text_calls and str(doc) in adapter.text_calls[0]
