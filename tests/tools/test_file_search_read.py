"""文件搜索与按行读取使用同一套行号，预览截断时仍统计全部命中文件。"""

from importlib import import_module

ft = import_module("plugins.system-core.tools.file_tools")


def test_read_uses_grep_line_numbers(tmp_path):
    (tmp_path / "example.py").write_bytes(b"first\nneedle\nlast\n")
    (tmp_path / "skip.txt").write_bytes(b"needle\n")
    ctx = {"workdir": str(tmp_path)}
    found = ft._grep_impl(ctx, pattern="needle", include="*.py")
    assert found.success and found.content.splitlines()[-1] == "2:needle"
    assert "skip.txt" not in found.content
    result = ft._read_impl(ctx, path="example.py", startline=2, endline=3, show_line_numbers=True)
    assert result.success and result.content.splitlines()[1:] == ["2:needle", "3:last"]


def test_grep_counts_files_after_preview_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "_MAX_GREP_CONTENT_LENGTH", 30)
    for name in ("one", "two", "three"):
        (tmp_path / name).write_bytes(b"needle\n")
    result = ft._grep_impl({"workdir": str(tmp_path)}, pattern="needle")
    assert result.success
    header, preview = result.content.split("\n", 1)
    assert header == "搜索到 3 个文件:"
    assert len(preview) == 30
