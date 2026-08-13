# -*- coding: utf-8 -*-
"""project-dashboard HTML 生成层测试"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "project-dashboard" / "ui"))
from dashboard import _vendor_script_tag, generate_html  # noqa: E402


def _sample_data():
    return {
        "repo_name": "demo", "branch": "main", "total_commits": 10,
        "daily_commits": [("2026-08-01", 3), ("2026-08-02", 7)],
        "contributors": [("Alice", 6), ("Bob", 4)],
        "languages": [("Python", 5, 120), ("Markdown", 2, 30)],
        "file_types": [[".py", 5], [".md", 2]],
        "generated_at": "2026-08-13 10:00", "error": None,
    }


def test_generate_html_structure():
    html = generate_html(_sample_data(), is_dark=True)
    assert "<!DOCTYPE html>" in html
    assert "demo" in html            # 仓库名
    assert "main" in html            # 分支
    assert "10" in html              # commit 总数
    assert "echarts.min.js" in html  # vendor script
    assert "echarts.init" in html    # 初始化代码


def test_vendor_script_tag():
    tag = _vendor_script_tag()
    assert tag.startswith("<script src=\"file:///")
    assert tag.endswith("echarts.min.js\"></script>")
    assert "app/resources/web/vendor" in tag


def test_generate_html_dark_palette():
    dark = generate_html(_sample_data(), is_dark=True)
    light = generate_html(_sample_data(), is_dark=False)
    assert dark != light
    assert "background: #1a1f2e" in dark    # 暗色背景
    assert "background: #f7f8fa" in light   # 亮色背景
