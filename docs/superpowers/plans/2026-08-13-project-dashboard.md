# project-dashboard 插件实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `project-dashboard` 插件：`/project-dashboard` function 命令在项目 git 根 `.drifox/reports/` 生成项目图表 HTML，welcome tab「📊 项目看板」通过 iframe 展示，tab 内追问按钮触发重新生成。

**Architecture:** 插件含 commands 组件（`.md` 命令定义，PluginManager 自动加载）+ ui 组件（`register_ui` 注册 welcome tab + FunctionCommandHandlers handler）。handler 用 subprocess 调 git + 遍历文件系统生成独立 HTML（内联 echarts vendor 的 file:// 绝对路径），生成后调 registry `_refresh_welcome_cards()` 刷新显示。整条链路零主程序改动。

**Tech Stack:** Python 3.14、stdlib（subprocess/sqlite3/json/pathlib）、echarts 5（复用 DriFox `app/resources/web/vendor/echarts.min.js`）、PyQt5 欢迎卡片。

## Global Constraints

- Python 3.14：禁止 Python 2 语法 `except X, e:`，一律 `except X as e:`
- ruff 格式：行宽 120、双引号；导入 标准→三方→本地
- 插件不导入 `app.core` / `app.widgets` 内部模块之外的主程序代码（仅允许 `app.core.ui_plugin_registry` / `app.core.command_manager` / `app.core.builtin_commands` / `app.utils.theme_manager`）
- 不加第三方依赖（纯 stdlib + 现有 vendor echarts）
- 提交规范：`feat|fix|docs: scope - summary`

---

### Task 1: 插件骨架（plugin.json + 命令定义）

**Files:**
- Create: `plugins/project-dashboard/.drifox-plugin/plugin.json`
- Create: `plugins/project-dashboard/commands/project-dashboard.md`
- Create: `plugins/project-dashboard/icon.svg`
- Create: `plugins/project-dashboard/icon_dark.svg`

**Interfaces:**
- Consumes: 无
- Produces: 插件目录结构（PluginManager 自动发现 `commands/` → 命令 `/project-dashboard` 注册为 FUNCTION 类型）；icon 供插件市场显示

- [ ] **Step 1: 创建 plugin.json**

`plugins/project-dashboard/.drifox-plugin/plugin.json`：

```json
{
    "icon": {
        "light": "icon.svg",
        "dark": "icon_dark.svg"
    },
    "name": "project-dashboard",
    "description": "项目信息看板 — 生成 commit 趋势/语言分布/贡献者/文件统计图表，欢迎卡片展示",
    "version": "0.1.0",
    "author": {
        "name": "DriFox Contributors"
    },
    "homepage": "https://github.com/martin98-afk/DriFox",
    "license": "MIT",
    "type": "system",
    "components": {
        "ui": true,
        "commands": true
    }
}
```

- [ ] **Step 2: 创建命令定义**

`plugins/project-dashboard/commands/project-dashboard.md`：

```markdown
---
description: 生成项目信息看板 HTML（commit 趋势/语言分布/贡献者/文件统计），输出到 .drifox/reports/project-dashboard.html
type: function
argument-hint: ""
---
```

- [ ] **Step 3: 创建图标（复用 system-cleaner 的图标占位）**

复制 `plugins/system-cleaner/icon.svg` 和 `icon_dark.svg` 到 `plugins/project-dashboard/`（命令行 copy）。

- [ ] **Step 4: 验证命令注册**

Run: `python -c "from app.core.plugin_manager import PluginManager; pm=PluginManager.get_instance(); print([f.path.name for f in pm.get_command_files()])"`
Expected: 输出包含 `project-dashboard`（需先 `git pull` 后启动环境，若 PluginManager 未初始化则跳过此步，改由 Task 4 集成验证）

- [ ] **Step 5: Commit**

```bash
git add plugins/project-dashboard/
git commit -m "feat: project-dashboard - 插件骨架与命令定义"
```

---

### Task 2: 数据采集层（dashboard.py 前半：git + 文件扫描）

**Files:**
- Create: `plugins/project-dashboard/ui/dashboard.py`
- Test: `tests/test_project_dashboard_data.py`

**Interfaces:**
- Consumes: 无（纯函数，不依赖主程序）
- Produces: 供 Task 3 使用：
  - `collect_data(project_root: str) -> dict` — 返回 `{"repo_name", "branch", "total_commits", "daily_commits": [(date_str, count)], "contributors": [(name, count)], "languages": [(name, files, lines)], "file_types": [(ext, count)], "generated_at": str, "error": str|None}`
  - `find_git_root(start: str) -> str|None`
  - `_run_git(cwd, *args, timeout=8) -> str`（抛 `subprocess.CalledProcessError` 时返回 ""）

- [ ] **Step 1: 写失败测试**

`tests/test_project_dashboard_data.py`：

```python
# -*- coding: utf-8 -*-
"""project-dashboard 数据采集层测试（临时 git 仓库）"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "project-dashboard" / "ui"))
from dashboard import collect_data, find_git_root  # noqa: E402


@pytest.fixture
def git_repo(tmp_path):
    """建一个含 2 个 commit、2 个 .py 文件、1 个 .md 文件的临时 git 仓库"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)
    (repo / "a.py").write_text("x = 1\n" * 10, encoding="utf-8")
    (repo / "b.py").write_text("y = 2\n" * 5, encoding="utf-8")
    (repo / "readme.md").write_text("# hi", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "first"], check=True)
    (repo / "c.py").write_text("z = 3\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "second"], check=True)
    return repo


def test_find_git_root(git_repo):
    sub = git_repo / "sub" / "dir"
    sub.mkdir(parents=True)
    assert find_git_root(str(sub)) == str(git_repo)


def test_collect_data_basic(git_repo):
    data = collect_data(str(git_repo))
    assert data["error"] is None
    assert data["total_commits"] == 2
    assert data["branch"]  # 非空（master/main）
    assert sum(c for _, c in data["daily_commits"]) == 2
    assert sum(c for _, c in data["contributors"]) == 2
    # 语言统计：py 文件 2 个、md 1 个（c.py 也在）
    by_name = {name: (files, lines) for name, files, lines in data["languages"]}
    assert by_name["Python"][0] == 3
    assert by_name["Markdown"][0] == 1
    # 文件类型统计
    exts = dict(data["file_types"])
    assert exts[".py"] == 3
    assert exts[".md"] == 1


def test_collect_data_non_git(tmp_path):
    data = collect_data(str(tmp_path))
    assert data["error"] is not None
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_project_dashboard_data.py -v`
Expected: FAIL（`ModuleNotFoundError: dashboard`）

- [ ] **Step 3: 写最小实现**

`plugins/project-dashboard/ui/dashboard.py`（仅数据采集部分，HTML 生成留 Task 3）：

```python
# -*- coding: utf-8 -*-
"""project-dashboard 数据采集 — git 统计 + 文件系统扫描

纯 stdlib，不依赖主程序模块，可独立测试。
"""
import os
import subprocess
from collections import Counter, defaultdict
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
                result["contributors"].append((parts[1].strip(), int(parts[0])))
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
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_project_dashboard_data.py -v`
Expected: PASS（3 个测试全过）

- [ ] **Step 5: Commit**

```bash
git add plugins/project-dashboard/ui/dashboard.py tests/test_project_dashboard_data.py
git commit -m "feat: project-dashboard - 数据采集层（git 统计 + 文件扫描）"
```

---

### Task 3: HTML 生成层（dashboard.py 后半）

**Files:**
- Modify: `plugins/project-dashboard/ui/dashboard.py`（追加 HTML 生成函数）
- Test: `tests/test_project_dashboard_html.py`

**Interfaces:**
- Consumes: Task 2 的 `collect_data()`、`_run_git()`、`find_git_root()`
- Produces: 供 Task 4 使用：
  - `generate_html(data: dict, is_dark: bool) -> str` — 完整 HTML 文档字符串（含 echarts vendor script 标签）
  - `write_report(project_root: str, is_dark: bool) -> str` — 生成文件到 `.drifox/reports/project-dashboard.html`，返回文件绝对路径；失败返回空串
  - `_vendor_script_tag() -> str` — 探测 DriFox echarts vendor 的 file:// 绝对路径 script 标签（`_PROJECT_ROOT` / `sys._MEIPASS` 兜底）

- [ ] **Step 1: 写失败测试**

`tests/test_project_dashboard_html.py`：

```python
# -*- coding: utf-8 -*-
"""project-dashboard HTML 生成层测试"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "project-dashboard" / "ui"))
from dashboard import generate_html, _vendor_script_tag  # noqa: E402


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
    assert "--bg" in dark and "--bg" in light
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_project_dashboard_html.py -v`
Expected: FAIL（`ImportError: cannot import name 'generate_html'`）

- [ ] **Step 3: 追加 HTML 生成实现**

在 `dashboard.py` 末尾追加：

```python
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


def _chart_div(option: dict, height: int = 200) -> str:
    """单个 echarts 容器 div + 初始化 JS"""
    import json as _json

    opt_json = _json.dumps(option, ensure_ascii=False)
    return (
        f'<div class="chart" id="c{abs(hash(opt_json)) % 100000}" '
        f'style="height:{height}px;"></div>\n'
        f'<script>window._opts=window._opts||[];'
        f'window._opts.push({{el:"c{abs(hash(opt_json)) % 100000}",'
        f'opt:{opt_json}}});</script>'
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
    summary = f"**{repo}** · `{branch}` · 生成于 {generated} · 共 {total} commits"

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
```

注意：`dashboard.py` 顶部需补 `import sys`（`_vendor_script_tag` 用）。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_project_dashboard_html.py -v`
Expected: PASS（3 个测试全过）

- [ ] **Step 5: 手动验证 HTML 可打开**

Run: `python -c "import sys; sys.path.insert(0,'plugins/project-dashboard/ui'); from dashboard import collect_data, generate_html, write_report; d=collect_data('.'); print(d['error']); print(write_report('.', True))"`
Expected: 输出无 error，打印 HTML 文件绝对路径，浏览器打开可见 4 图

- [ ] **Step 6: Commit**

```bash
git add plugins/project-dashboard/ui/dashboard.py tests/test_project_dashboard_html.py
git commit -m "feat: project-dashboard - HTML 生成层（4 图 echarts 报告）"
```

---

### Task 4: UI 注册（welcome tab + function handler）

**Files:**
- Create: `plugins/project-dashboard/ui/__init__.py`

**Interfaces:**
- Consumes: Task 3 的 `write_report()`、Task 2 的 `collect_data()`；主程序 `UIPluginRegistry.register_welcome_tab` / `CommandManager.register` / `FunctionCommandHandlers.register`
- Produces: 
  - `register_ui(registry)` — 注册 welcome tab（mode_key=`project-dashboard`，label=`📊 项目看板`）+ function 命令 handler
  - `render_welcome_tab(ctx) -> str` — welcome tab HTML 片段（概要行 + iframe + 追问按钮）

- [ ] **Step 1: 写 ui/__init__.py**

`plugins/project-dashboard/ui/__init__.py`：

```python
# -*- coding: utf-8 -*-
"""project-dashboard UI 组件入口 — 欢迎卡片「📊 项目看板」tab + function 命令

- register_welcome_tab：欢迎卡片新增 tab，iframe 展示 .drifox/reports/project-dashboard.html
- FunctionCommandHandlers：/project-dashboard 生成 HTML，生成后刷新欢迎卡片
"""

import os
import sys
from pathlib import Path

from loguru import logger


def _get_project_root() -> str:
    """获取当前项目 git 根：优先 registry 活跃窗口 provider，兜底 os.getcwd()"""
    try:
        from app.core.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        provider = reg._resolve_active_window_provider()
        if provider is not None:
            ctx = provider()
            root = ctx.get("project_root")
            if root:
                return root
    except Exception:
        pass
    return os.getcwd()


def _report_path() -> str:
    """报告文件绝对路径（不存在返回空串）"""
    root = _get_project_root()
    try:
        from dashboard import find_git_root

        git_root = find_git_root(root)
        if not git_root:
            return ""
        p = Path(git_root) / ".drifox" / "reports" / "project-dashboard.html"
        return str(p) if p.exists() else ""
    except Exception:
        return ""


def _render_welcome_tab(ctx: dict = None) -> str:
    """welcome tab render_func：概要行 + iframe + 追问按钮

    iframe src 带 ?t=<mtime> 时间戳，生成后强制重新加载最新文件。
    """
    is_dark = ctx.get("is_dark") if isinstance(ctx, dict) else None
    light = "--text: #333; --muted: #999;"
    dark = "--text: #e6e6e6; --muted: #8a8a8a;"
    if is_dark is not None:
        root_css = f":root {{ {dark if is_dark else light} }}"
    else:
        root_css = (
            f":root {{ {light} }}"
            f"@media (prefers-color-scheme: dark) {{ :root {{ {dark} }} }}"
        )

    rp = _report_path()
    if not rp:
        body = '<div class="pd-empty">📊 尚未生成项目看板，点击下方按钮生成</div>'
    else:
        try:
            mtime = int(Path(rp).stat().st_mtime)
        except OSError:
            mtime = 0
        body = (
            '<iframe class="pd-frame" '
            f'src="file:///{Path(rp).as_posix()}?t={mtime}" '
            'loading="lazy"></iframe>'
        )

    return f"""<div class="pd-wrap">
  {body}
  <span class="context-tag pd-refresh" data-action="ask"
        data-content="/project-dashboard">🔄 重新生成</span>
</div>
<style>
.pd-wrap {{ max-width: 640px; margin: 0 auto; }}
.pd-frame {{ width: 100%; height: 460px; border: 1px solid var(--text-muted, rgba(128,128,128,0.2));
             border-radius: 10px; background: transparent; }}
.pd-empty {{ padding: 18px 4px; color: var(--text); }}
.pd-refresh {{ display: inline-block; margin-top: 10px; cursor: pointer;
               color: var(--text, #333); }}
{root_css}
</style>
"""


def _handler(args: str) -> None:
    """/project-dashboard 命令 handler：生成 HTML + 刷新欢迎卡片"""
    root = _get_project_root()
    try:
        from dashboard import write_report

        # is_dark：跟随 Qt 主题（生成时状态）
        is_dark = True
        try:
            from app.utils.theme_manager import theme_manager

            is_dark = not theme_manager.is_light_theme()
        except Exception:
            pass
        path = write_report(root, is_dark)
        if path:
            logger.info(f"[project-dashboard] report generated: {path}")
        else:
            logger.warning("[project-dashboard] report generation failed (not a git repo?)")
    except Exception as e:
        logger.error(f"[project-dashboard] generate failed: {e}")
    # 刷新欢迎卡片（重新渲染 iframe，加载最新文件）
    try:
        from app.core.ui_plugin_registry import UIPluginRegistry

        UIPluginRegistry.get_instance()._refresh_welcome_cards()
    except Exception:
        pass


def register_ui(registry):
    """注册 project-dashboard 的 UI 组件"""
    # 清理旧子模块缓存（热重载兼容）
    prefix = "ui_plugin_project_dashboard."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    # 确保 ui 目录在 sys.path（相对导入 dashboard）
    ui_dir = str(Path(__file__).resolve().parent)
    if ui_dir not in sys.path:
        sys.path.insert(0, ui_dir)

    registry.register_welcome_tab(
        plugin_name="project-dashboard",
        mode_key="project-dashboard",
        label="📊 项目看板",
        render_func=_render_welcome_tab,
        priority=0,
    )

    # 注册 function 命令（命令定义已由 commands/*.md 提供）
    try:
        from app.core.builtin_commands import FunctionCommandHandlers
        from app.core.command_manager import CommandManager, CommandType

        cmd_mgr = CommandManager.get_instance()
        if not cmd_mgr.has_command("project-dashboard"):
            cmd_mgr.register(
                name="project-dashboard",
                command_type=CommandType.FUNCTION,
                description="生成项目信息看板 HTML（.drifox/reports/）",
            )
        FunctionCommandHandlers.register("project-dashboard", _handler)
    except Exception as e:
        logger.error(f"[project-dashboard] command register failed: {e}")

    logger.info("[project-dashboard] UI components registered")
```

- [ ] **Step 2: 静态检查**

Run: `python -m ruff check plugins/project-dashboard/`
Expected: 无错误（若 ruff 未装则跳过）

- [ ] **Step 3: 验证 register_ui 可导入执行（无 Qt 环境）**

Run: `python -c "import sys; sys.path.insert(0,'plugins/project-dashboard/ui'); import __init__ as m; print(hasattr(m,'register_ui')); print(m._render_welcome_tab({'is_dark':True})[:80])"`
Expected: `True` + HTML 片段开头

- [ ] **Step 4: Commit**

```bash
git add plugins/project-dashboard/ui/__init__.py
git commit -m "feat: project-dashboard - welcome tab 与 function 命令注册"
```

---

### Task 5: 集成验证

**Files:** 无（验证 + 修复）

**Interfaces:**
- Consumes: Task 1-4 全部产物

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/test_project_dashboard_data.py tests/test_project_dashboard_html.py -v`
Expected: 全 PASS

- [ ] **Step 2: ruff 检查全部改动**

Run: `python -m ruff check plugins/project-dashboard/ tests/test_project_dashboard_data.py tests/test_project_dashboard_html.py`
Expected: 无错误

- [ ] **Step 3: 启动程序手工验证（checklist.md §8.6 适配）**

```
启动 DriFox → 欢迎卡片出现「📊 项目看板」tab
1. tab 标签文字显示正确？            → 「📊 项目看板」
2. 首用（无 HTML）显示「尚未生成」+ 刷新按钮？
3. 点追问按钮 → 命令执行 → HTML 生成 → tab 自动刷新显示 iframe？
4. iframe 内 4 图渲染正确？          → echarts 正常（无 JS 报错）
5. 明暗主题切换颜色跟随？            → is_dark 生效
6. 高度合适（iframe 460px，无多余滚动条）？
7. 概要行精简（1 行）？
8. 再次点击 → 重新生成（时间戳/时间更新）？
```

- [ ] **Step 4: 修复发现的问题并提交**

```bash
git add -A
git commit -m "fix: project-dashboard - 集成验证修复"
```

---

## Self-Review

**Spec 覆盖：**
- ✅ command 生成 HTML 到 `.drifox/reports/` → Task 2/3
- ✅ welcome tab iframe 展示 → Task 4
- ✅ 追问按钮复用 context-tag 触发命令 → Task 4（`data-action="ask" data-content="/project-dashboard"`）
- ✅ 生成后刷新显示 → Task 4 `_handler` 调 `_refresh_welcome_cards()`
- ✅ 高度控制 460px → Task 4 `_render_welcome_tab`
- ✅ 概要行文字精简 → Task 3 `generate_html` summary
- ✅ 4 图（commit 趋势/贡献者/语言/文件类型）→ Task 3
- ✅ 零主程序改动 → 全链路复用现有机制
- ✅ 无第三方依赖 → stdlib + 现有 vendor echarts

**占位符扫描：** 无 TBD/TODO；所有代码完整可执行。

**类型一致性：** `collect_data` 返回 dict 键名在 Task 2/3/4 一致；`write_report(project_root, is_dark) -> str` 签名三处引用一致；`_render_welcome_tab(ctx)` 与 `register_welcome_tab` 的 render_func 签名一致。
