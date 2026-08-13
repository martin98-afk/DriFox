# -*- coding: utf-8 -*-
"""project-dashboard 数据采集 — git 统计 + 文件系统扫描

纯 stdlib，不依赖主程序模块，可独立测试。
"""
import os
import subprocess
import sys
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
              languages/file_types/generated_at/error
    """
    result = {
        "repo_name": "", "branch": "", "total_commits": 0,
        "daily_commits": [], "contributors": [], "languages": [],
        "file_types": [], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "error": None,
    }
    git_root = find_git_root(project_root)
    if not git_root:
        result["error"] = "当前目录不是 git 仓库"
        return result
    result["repo_name"] = Path(git_root).name
    result["branch"] = _run_git(git_root, "branch", "--show-current") or "unknown"

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
# HTML 生成
# ============================================================

_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # DriFox 项目根


def _vendor_script_tag() -> str:
    """探测 DriFox echarts vendor 的 file:// 绝对路径 script 标签

    PyInstaller 打包后资源在 sys._MEIPASS 下；开发环境在 _PROJECT_ROOT。
    """
    base_dirs = [_PROJECT_ROOT]
    if hasattr(sys, "_MEIPASS"):
        base_dirs.append(Path(sys._MEIPASS))
    for base in base_dirs:
        p = base / "app" / "resources" / "web" / "vendor" / "echarts.min.js"
        if p.exists():
            return f'<script src="file:///{p.as_posix()}"></script>'
    return ""  # vendor 缺失时图表不渲染，但页面结构仍可读


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


def _esc(s) -> str:
    """HTML 转义（数据进入 HTML 必须转义）"""
    import html as _html

    return _html.escape(str(s), quote=True)


def _commit_trend_option(daily: list, p: dict) -> dict:
    days = [d for d, _ in daily]
    vals = [c for _, c in daily]
    return {
        "backgroundColor": "transparent",
        "textStyle": {"color": p["text"]},
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 40, "right": 16, "top": 30, "bottom": 24},
        "title": {"text": "近 30 天 Commit 趋势", "left": 8, "top": 4,
                  "textStyle": {"fontSize": 13, "color": p["text"]}},
        "xAxis": {"type": "category", "data": days,
                  "axisLabel": {"color": p["muted"], "fontSize": 10}},
        "yAxis": {"type": "value", "minInterval": 1,
                  "axisLabel": {"color": p["muted"], "fontSize": 10},
                  "splitLine": {"lineStyle": {"color": p["grid"]}}},
        "series": [{"name": "Commits", "type": "bar", "data": vals,
                    "itemStyle": {"color": p["accent"], "borderRadius": [3, 3, 0, 0]}}],
    }


def _contributors_option(contributors: list, p: dict) -> dict:
    names = [n for n, _ in contributors]
    counts = [c for _, c in contributors]
    return {
        "backgroundColor": "transparent",
        "textStyle": {"color": p["text"]},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 100, "right": 24, "top": 30, "bottom": 20},
        "title": {"text": "贡献者 Top", "left": 8, "top": 4,
                  "textStyle": {"fontSize": 13, "color": p["text"]}},
        "xAxis": {"type": "value", "minInterval": 1,
                  "axisLabel": {"color": p["muted"], "fontSize": 10},
                  "splitLine": {"lineStyle": {"color": p["grid"]}}},
        "yAxis": {"type": "category", "data": names,
                  "axisLabel": {"color": p["text"], "fontSize": 11}},
        "series": [{"name": "Commits", "type": "bar", "data": counts,
                    "itemStyle": {"color": p["success"]}}],
    }


def _languages_option(languages: list, p: dict) -> dict:
    names = [n for n, _, _ in languages]
    files = [f for _, f, _ in languages]
    return {
        "backgroundColor": "transparent",
        "textStyle": {"color": p["text"]},
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} 文件 ({d}%)"},
        "title": {"text": "语言分布（按文件数）", "left": 8, "top": 4,
                  "textStyle": {"fontSize": 13, "color": p["text"]}},
        "series": [{
            "type": "pie", "radius": ["38%", "68%"], "center": ["50%", "55%"],
            "itemStyle": {"borderColor": p["card"], "borderWidth": 2},
            "label": {"color": p["text"], "fontSize": 11},
            "data": [{"name": n, "value": f} for n, f, _ in languages],
        }],
    }


def _file_types_option(file_types: list, p: dict) -> dict:
    exts = [e for e, _ in file_types]
    counts = [c for _, c in file_types]
    return {
        "backgroundColor": "transparent",
        "textStyle": {"color": p["text"]},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 60, "right": 24, "top": 30, "bottom": 20},
        "title": {"text": "文件类型 Top", "left": 8, "top": 4,
                  "textStyle": {"fontSize": 13, "color": p["text"]}},
        "xAxis": {"type": "value", "minInterval": 1,
                  "axisLabel": {"color": p["muted"], "fontSize": 10},
                  "splitLine": {"lineStyle": {"color": p["grid"]}}},
        "yAxis": {"type": "category", "data": exts,
                  "axisLabel": {"color": p["text"], "fontSize": 11}},
        "series": [{"name": "文件数", "type": "bar", "data": counts,
                    "itemStyle": {"color": p["warn"]}}],
    }


_chart_seq = [0]


def _chart_div(option: dict, height: int = 200) -> str:
    """单个 echarts 容器 div + 初始化 JS（id 用自增序号，避免 hash 碰撞）"""
    import json as _json

    _chart_seq[0] += 1
    cid = f"c{_chart_seq[0]}"
    opt_json = _json.dumps(option, ensure_ascii=False)
    return (
        f'<div class="chart" id="{cid}" style="height:{height}px;"></div>\n'
        f'<script>window._opts=window._opts||[];'
        f'window._opts.push({{el:"{cid}",opt:{opt_json}}});</script>'
    )


def generate_html(data: dict, is_dark: bool) -> str:
    """生成完整 HTML 文档（4 图 + 概要行），供 iframe 展示"""
    p = _palette(is_dark)
    vendor = _vendor_script_tag()
    err_block = ""
    if data.get("error"):
        err_block = f'<div class="err">⚠️ {_esc(data["error"])}</div>'
    repo = _esc(data.get("repo_name", ""))
    branch = _esc(data.get("branch", ""))
    generated = _esc(data.get("generated_at", ""))
    total = data.get("total_commits", 0)
    summary = f"<b>{repo}</b> · 分支 {branch} · 生成于 {generated} · 共 {total} commits"

    charts = []
    if data.get("daily_commits"):
        charts.append(_chart_div(_commit_trend_option(data["daily_commits"], p), 190))
    if data.get("contributors"):
        charts.append(_chart_div(_contributors_option(data["contributors"], p), 190))
    if data.get("languages"):
        charts.append(_chart_div(_languages_option(data["languages"], p), 200))
    if data.get("file_types"):
        charts.append(_chart_div(_file_types_option(data["file_types"], p), 180))

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{repo} 项目看板</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: {p["bg"]}; color: {p["text"]};
         font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
         padding: 12px 14px; }}
  .summary {{ font-size: 13px; margin-bottom: 10px; opacity: 0.9; }}
  .err {{ background: rgba(245,166,35,0.15); color: {p["warn"]};
          padding: 10px 12px; border-radius: 8px; margin-bottom: 10px; }}
  .chart {{ width: 100%; }}
</style>
</head>
<body>
  <div class="summary">{summary}</div>
  {err_block}
  {''.join(charts)}
  {vendor}
<script>
  window.addEventListener('load', function () {{
    if (typeof echarts === 'undefined') return;
    (window._opts || []).forEach(function (o) {{
      var el = document.getElementById(o.el);
      if (el) {{ var c = echarts.init(el); c.setOption(o.opt); }}
    }});
  }});
</script>
</body>
</html>
"""


def write_report(project_root: str, is_dark: bool) -> str:
    """生成报告文件到 <git-root>/.drifox/reports/project-dashboard.html

    Returns:
        文件绝对路径；失败返回空串
    """
    git_root = find_git_root(project_root)
    if not git_root:
        return ""
    data = collect_data(git_root)
    out_dir = Path(git_root) / ".drifox" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "project-dashboard.html"
    try:
        out_file.write_text(generate_html(data, is_dark), encoding="utf-8")
        return str(out_file)
    except OSError:
        return ""

