# -*- coding: utf-8 -*-
"""project-dashboard echarts option 生成测试"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "project-dashboard" / "ui"))
from dashboard import build_options  # noqa: E402


def _sample_data():
    return {
        "repo_name": "demo", "branch": "main", "total_commits": 10,
        "daily_commits": [("2026-08-01", 3), ("2026-08-02", 7)],
        "contributors": [("Alice", 6), ("Bob", 4)],
        "languages": [("Python", 5, 120), ("Markdown", 2, 30)],
        "file_types": [[".py", 5], [".md", 2]],
        "generated_at": "2026-08-13 10:00", "error": None,
    }


def test_build_options_structure():
    options = build_options(_sample_data(), is_dark=True)
    assert len(options) == 2  # 2 个 echarts 实例（双 grid 合并）
    for opt in options:
        assert isinstance(opt, dict)
        # 可 JSON 序列化（echarts 代码块走 JSON.parse）
        json.dumps(opt, ensure_ascii=False)
        assert len(opt["series"]) == 2  # 每实例 2 图


def test_build_options_json_serializable():
    """echarts JSON 不能携带 JS 函数：序列化后必须能被 JSON.parse 还原"""
    options = build_options(_sample_data(), is_dark=True)
    for opt in options:
        text = json.dumps(opt, ensure_ascii=False)
        assert "function" not in text  # 无 JS 函数表达式
        parsed = json.loads(text)      # JSON.parse 等价
        assert parsed["series"] == opt["series"]


def test_build_options_dark_palette():
    dark = build_options(_sample_data(), is_dark=True)
    light = build_options(_sample_data(), is_dark=False)
    assert dark != light
    # 暗色文本色
    assert dark[0]["textStyle"]["color"] == "#e6e6e6"
    assert light[0]["textStyle"]["color"] == "#333333"


def test_build_options_empty_data():
    """无数据：返回空列表（不输出空 echarts 块）"""
    data = _sample_data()
    data.update({
        "daily_commits": [], "contributors": [], "languages": [], "file_types": [],
    })
    assert build_options(data, is_dark=True) == []
