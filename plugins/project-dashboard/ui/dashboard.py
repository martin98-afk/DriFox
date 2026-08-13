# -*- coding: utf-8 -*-
"""project-dashboard 数据采集 + echarts option 生成

纯 stdlib（不依赖 PyQt / 主程序），可独立测试：
- collect_data(): git 统计 + 文件系统扫描
- build_options(): 生成 2 个 echarts option（双 grid 左右布局，控制高度）
"""

import os
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

# 跳过目录：噪音目录不统计
_SKIP_DIRS = {
    ".git", ".drifox", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".vscode", "target", "vendor",
}
# 扩展名 → 语言名（覆盖常见项目；未知扩展名归入原扩展名）
_EXT_LANG = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".jsx": "JavaScript", ".java": "Java", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".kt": "Kotlin", ".scala": "Scala", ".md": "Markdown", ".rst": "Markdown",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".json": "JSON",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".xml": "XML",
    ".sql": "SQL", ".sh": "Shell", ".bat": "Batch", ".ps1": "PowerShell",
    ".dockerfile": "Dockerfile", ".txt": "Text",
}


def _run_git(cwd: str, *args: str, timeout: int = 8) -> str:
    """执行 git 命令，失败/超时返回空串（不抛异常）"""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def find_git_root(start: str) -> Optional[str]:
    """从 start 向上查找 git 根目录；非 git 仓库返回 None"""
    out = _run_git(start, "rev-parse", "--show-toplevel")
    return out or None


def _scan_files(project_root: str):
    """遍历项目文件（跳过噪音目录），返回 (ext_counter, lang_files, lang_lines)"""
    ext_counter: Counter = Counter()
    lang_files: Counter = Counter()
    lang_lines: Counter = Counter()
    for dirpath, dirnames, filenames in os.walk(project_root):
        # 原地过滤跳过目录（os.walk 生效）
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            ext = Path(fn).suffix.lower()
            if not ext:
                continue
            ext_counter[ext] += 1
            lang = _EXT_LANG.get(ext, ext.lstrip(".").capitalize())
            lang_files[lang] += 1
            try:
                p = Path(dirpath) / fn
                if p.stat().st_size > 512 * 1024:  # 大文件跳过行数统计
                    continue
                with open(p, encoding="utf-8", errors="ignore") as f:
                    lang_lines[lang] += sum(1 for _ in f)
            except OSError:
                pass
    return ext_counter, lang_files, lang_lines


def collect_data(project_root: str) -> dict:
    """采集项目看板数据（git + 文件系统）

    Returns:
        dict: repo_name/branch/total_commits/daily_commits/contributors/
              languages/file_types/generated_at/head/error
    """
    result = {
        "repo_name": "", "branch": "", "total_commits": 0,
        "daily_commits": [], "contributors": [], "languages": [],
        "file_types": [], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "head": "", "error": None,
    }
    git_root = find_git_root(project_root)
    if not git_root:
        result["error"] = "当前目录不是 git 仓库"
        return result
    result["repo_name"] = Path(git_root).name
    result["branch"] = _run_git(git_root, "branch", "--show-current") or "unknown"
    result["head"] = _run_git(git_root, "rev-parse", "--short", "HEAD")

    # commit 趋势：近 30 天
    out = _run_git(
        git_root, "log", "--since=30 days ago", "--pretty=format:%ad",
        "--date=short",
    )
    daily: Counter = Counter()
    for line in out.splitlines():
        if line:
            daily[line] += 1
    result["daily_commits"] = sorted(daily.items())
    result["total_commits"] = sum(daily.values())

    # 贡献者 Top
    out = _run_git(git_root, "shortlog", "-sne", "--no-merges", "HEAD")
    for line in out.splitlines()[:8]:
        parts = line.strip().split("\t")
        if len(parts) == 2:
            try:
                # 取姓名（去掉 <email>），空名回退完整行
                name = parts[1].strip()
                if "<" in name:
                    name = name.split("<")[0].strip()
                result["contributors"].append((name or parts[1].strip(), int(parts[0])))
            except ValueError:
                pass

    # 文件统计
    ext_counter, lang_files, lang_lines = _scan_files(git_root)
    result["file_types"] = ext_counter.most_common(10)
    result["languages"] = sorted(
        ((lang, lang_files[lang], lang_lines[lang]) for lang in lang_files),
        key=lambda x: -x[1],
    )[:10]
    return result


# ============================================================
# echarts option 生成（双 grid 合并，控制高度）
# ============================================================


def _palette(is_dark: bool) -> dict:
    """明暗色板（与主程序欢迎卡片风格对齐）"""
    if is_dark:
        return {
            "bg": "#1a1f2e", "card": "#232838", "text": "#e6e6e6",
            "muted": "#8a8a8a", "grid": "rgba(255,255,255,0.08)",
            "accent": "#62a0ea", "success": "#50e3c2", "warn": "#f5a623",
        }
    return {
        "bg": "#f7f8fa", "card": "#ffffff", "text": "#333333",
        "muted": "#999999", "grid": "rgba(0,0,0,0.06)",
        "accent": "#2878dc", "success": "#00a888", "warn": "#e08e0b",
    }


def _base_style(p: dict) -> dict:
    return {
        "backgroundColor": "transparent",
        "textStyle": {"color": p["text"]},
    }


def _grid_layout() -> list:
    """双 grid 左右布局（各占 50%）"""
    return [
        {"left": 8, "right": "52%", "top": 34, "bottom": 20, "containLabel": True},
        {"left": "52%", "right": 8, "top": 34, "bottom": 20, "containLabel": True},
    ]


def _commit_trend_option(daily: list, p: dict) -> dict:
    """左 grid：近 30 天 commit 柱状图"""
    days = [d for d, _ in daily]
    vals = [c for _, c in daily]
    return {
        "type": "bar",
        "name": "Commits",
        "data": vals,
        "barMaxWidth": 18,
        "itemStyle": {"color": p["accent"], "borderRadius": [3, 3, 0, 0]},
        "xAxisIndex": 0,
        "yAxisIndex": 0,
    }, {
        "type": "category", "gridIndex": 0, "data": days,
        "axisLabel": {"color": p["muted"], "fontSize": 9, "interval": 4},
        "axisLine": {"lineStyle": {"color": p["grid"]}},
        "axisTick": {"show": False},
    }, {
        "type": "value", "gridIndex": 0, "minInterval": 1,
        "axisLabel": {"color": p["muted"], "fontSize": 9},
        "splitLine": {"lineStyle": {"color": p["grid"]}},
    }


def _contributors_option(contributors: list, p: dict) -> dict:
    """右 grid：贡献者 Top 横向条形图"""
    names = [n for n, _ in contributors][::-1]
    counts = [c for _, c in contributors][::-1]
    return {
        "type": "bar",
        "name": "Commits",
        "data": counts,
        "barMaxWidth": 12,
        "itemStyle": {"color": p["success"]},
        "xAxisIndex": 1,
        "yAxisIndex": 1,
    }, {
        "type": "value", "gridIndex": 1, "minInterval": 1,
        "axisLabel": {"color": p["muted"], "fontSize": 9},
        "splitLine": {"lineStyle": {"color": p["grid"]}},
    }, {
        "type": "category", "gridIndex": 1, "data": names,
        "axisLabel": {"color": p["text"], "fontSize": 9},
        "axisLine": {"lineStyle": {"color": p["grid"]}},
        "axisTick": {"show": False},
    }


def _languages_option(languages: list, p: dict) -> dict:
    """左 grid：语言分布环形图"""
    names = [n for n, _, _ in languages]
    files = [f for _, f, _ in languages]
    return {
        "type": "pie",
        "name": "语言分布",
        "radius": ["32%", "55%"],
        "center": ["25%", "55%"],
        "itemStyle": {"borderColor": p["card"], "borderWidth": 2},
        "label": {"color": p["text"], "fontSize": 9},
        "data": [{"name": n, "value": f} for n, f, _ in languages],
    }


def _file_types_option(file_types: list, p: dict) -> dict:
    """右 grid：文件类型 Top 横向条形图"""
    exts = [e for e, _ in file_types][::-1]
    counts = [c for _, c in file_types][::-1]
    return {
        "type": "bar",
        "name": "文件数",
        "data": counts,
        "barMaxWidth": 12,
        "itemStyle": {"color": p["warn"]},
        "xAxisIndex": 1,
        "yAxisIndex": 1,
    }, {
        "type": "value", "gridIndex": 1, "minInterval": 1,
        "axisLabel": {"color": p["muted"], "fontSize": 9},
        "splitLine": {"lineStyle": {"color": p["grid"]}},
    }, {
        "type": "category", "gridIndex": 1, "data": exts,
        "axisLabel": {"color": p["text"], "fontSize": 9},
        "axisLine": {"lineStyle": {"color": p["grid"]}},
        "axisTick": {"show": False},
    }


def build_options(data: dict, is_dark: bool) -> list:
    """生成 2 个 echarts option（每个 400px 内双 grid 左右布局）

    option[0]: commit 趋势(左柱) + 贡献者 Top(右横条)
    option[1]: 语言分布(左环形) + 文件类型 Top(右横条)

    高度控制：2 实例 × 400px = 800px，避免 4 图各自 400px 过高。
    """
    p = _palette(is_dark)
    options = []

    # ── 实例 1：commit 趋势 + 贡献者 ──
    series, axes = [], []
    x_axes, y_axes = [], []
    if data.get("daily_commits"):
        s, x, y = _commit_trend_option(data["daily_commits"], p)
        series.append(s)
        x_axes.append(x)
        y_axes.append(y)
        title_left = {"text": "近 30 天 Commit", "left": 8, "top": 6,
                      "textStyle": {"fontSize": 11, "color": p["text"]}}
    else:
        title_left = {}
    if data.get("contributors"):
        s, x, y = _contributors_option(data["contributors"], p)
        series.append(s)
        x_axes.append(x)
        y_axes.append(y)
        title_right = {"text": "贡献者 Top", "left": "52%", "top": 6,
                       "textStyle": {"fontSize": 11, "color": p["text"]}}
    else:
        title_right = {}
    if series:
        options.append({
            **_base_style(p),
            "grid": _grid_layout(),
            "title": [t for t in (title_left, title_right) if t],
            "xAxis": x_axes,
            "yAxis": y_axes,
            "series": series,
        })

    # ── 实例 2：语言分布 + 文件类型 ──
    series, x_axes, y_axes = [], [], []
    if data.get("languages"):
        series.append(_languages_option(data["languages"], p))
        title_left = {"text": "语言分布", "left": 8, "top": 6,
                      "textStyle": {"fontSize": 11, "color": p["text"]}}
    else:
        title_left = {}
    if data.get("file_types"):
        s, x, y = _file_types_option(data["file_types"], p)
        series.append(s)
        x_axes.append(x)
        y_axes.append(y)
        title_right = {"text": "文件类型", "left": "52%", "top": 6,
                       "textStyle": {"fontSize": 11, "color": p["text"]}}
    else:
        title_right = {}
    if series:
        options.append({
            **_base_style(p),
            "grid": _grid_layout(),
            "title": [t for t in (title_left, title_right) if t],
            "xAxis": x_axes,
            "yAxis": y_axes,
            "series": series,
        })

    return options
