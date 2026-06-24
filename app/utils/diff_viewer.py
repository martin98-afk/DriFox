# -*- coding: utf-8 -*-
"""
Git Diff 差异对比模块 - Cursor/VS Code 风格差异审查工具

特性:
  1. 并排对比（Split View）- 旧版/新版左右对照，同一行严格对齐
  2. 统一直列（Unified View）- 删除块→新增块 成片展示，不逐行配对
  3. 一键切换视图模式
  4. 自动合并相邻 hunk（间距 ≤ 5 行）
  5. 点击文件路径 → 系统编辑器打开
  6. 行内词级差异高亮
  7. 上下文智能折叠（连续未修改行自动收起）
"""

import difflib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import orjson as json
from loguru import logger
from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineView
from PyQt5.QtWidgets import QDialog, QHBoxLayout

from app.core.webengine_profile import create_transient_web_profile

try:
    import psutil
except Exception:
    psutil = None

_HUNK_HEADER_PATTERN = re.compile(r"@@ -(\d+),?\d* \+(\d+),?\d* @@")


def _get_rss_mb() -> Optional[float]:
    if psutil is None:
        return None
    try:
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return None


# ==========================================================================
# 1. Hunk 合并器
# ==========================================================================
class DiffHunkMerger:
    """将间距过小的相邻 hunk 合并为一个大块，减少碎片化展示。

    触发条件:
      - 两个相邻 hunk 之间的 context 行数 ≤ MERGE_THRESHOLD
    """

    MERGE_THRESHOLD = 5

    @classmethod
    def merge_file_hunks(cls, lines: List[str]) -> List[str]:
        if not lines:
            return lines
        hunks = cls._split_hunks(lines)
        if len(hunks) <= 1:
            return lines
        merged = [hunks[0]]
        for h in hunks[1:]:
            prev = merged[-1]
            gap = cls._hunk_gap(prev, h)
            if gap is not None and 0 < gap <= cls.MERGE_THRESHOLD:
                merged[-1] = cls._merge_pair(prev, h)
            else:
                merged.append(h)
        result: List[str] = []
        for h in merged:
            result.extend(h)
        return result

    @classmethod
    def _split_hunks(cls, lines: List[str]) -> List[List[str]]:
        hunks: List[List[str]] = []
        current: List[str] = []
        for line in lines:
            if line.startswith("@@") and current:
                hunks.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            hunks.append(current)
        return hunks

    @classmethod
    def _hunk_gap(cls, hunk_a: List[str], hunk_b: List[str]) -> Optional[int]:
        header_a = None
        for line in reversed(hunk_a):
            if line.startswith("@@"):
                header_a = line
                break
        if not header_a:
            return None
        match_a = _HUNK_HEADER_PATTERN.search(header_a)
        if not match_a:
            return None
        old_start_a = int(match_a.group(1))
        new_start_a = int(match_a.group(2))
        old_off, new_off = cls._count_hunk_lines(hunk_a)

        header_b = None
        for line in hunk_b:
            if line.startswith("@@"):
                header_b = line
                break
        if not header_b:
            return None
        match_b = _HUNK_HEADER_PATTERN.search(header_b)
        if not match_b:
            return None
        old_start_b = int(match_b.group(1))
        new_start_b = int(match_b.group(2))

        gap_old = old_start_b - (old_start_a + old_off)
        gap_new = new_start_b - (new_start_a + new_off)
        if gap_old >= 0 and gap_new >= 0:
            return max(gap_old, gap_new)
        return None

    @classmethod
    def _count_hunk_lines(cls, hunk: List[str]) -> tuple:
        old = 0
        new = 0
        for line in hunk:
            if line.startswith("@@"):
                continue
            if line.startswith("-") and not line.startswith("---"):
                old += 1
            elif line.startswith("+") and not line.startswith("+++"):
                new += 1
            elif line.startswith(" ") or (not line.startswith(("-", "+"))):
                old += 1
                new += 1
        return old, new

    @classmethod
    def _merge_pair(cls, hunk_a: List[str], hunk_b: List[str]) -> List[str]:
        match_a = None
        for line in hunk_a:
            m = _HUNK_HEADER_PATTERN.search(line)
            if m:
                match_a = m
                break
        match_b = None
        for line in hunk_b:
            m = _HUNK_HEADER_PATTERN.search(line)
            if m:
                match_b = m
                break
        if not match_a or not match_b:
            return hunk_a + hunk_b
        old_start = int(match_a.group(1))
        new_start = int(match_a.group(2))
        old_total, new_total = cls._count_hunk_lines(hunk_a)
        old_total_b, new_total_b = cls._count_hunk_lines(hunk_b)
        old_count = old_total + old_total_b
        new_count = new_total + new_total_b
        new_header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"
        body_a = [l for l in hunk_a if not l.startswith("@@")]
        body_b = [l for l in hunk_b if not l.startswith("@@")]
        return [new_header] + body_a + body_b


# ==========================================================================
# 2. CSS 主题 — Cursor/VS Code 风格
# ==========================================================================
THEME_CSS = r"""
<style>
    :root {
        --bg: #0d1117;
        --bg2: #161b22;
        --bg3: #1c2128;
        --border: #30363d;
        --text: #c9d1d9;
        --text2: #8b949e;
        --text3: #6e7681;
        --accent: #58a6ff;
        --accent-bg: rgba(56,139,253,0.15);
        --green: #3fb950;
        --green-bg: rgba(63,185,80,0.12);
        --green-bg2: rgba(63,185,80,0.18);
        --red: #f85149;
        --red-bg: rgba(248,81,73,0.12);
        --red-bg2: rgba(248,81,73,0.18);
        --purple: #a371f7;
        --purple-bg: rgba(163,113,247,0.15);
        --radius: 6px;
        --mono: 'Cascadia Code','JetBrains Mono','Fira Code','Consolas','Monaco',monospace;
        --sans: -apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans SC',sans-serif;
        --lh: 22px;
        --fs: 12.5px;
    }
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
        font-family:var(--sans); background:var(--bg); color:var(--text);
        line-height:1.5; height:100vh; overflow:hidden; font-size:12px;
        user-select:none;
    }
    /* 代码区域可选中，UI 元素保持不可选中 */
    .du-code, .ds-code, .file-hdr .fh-path, .sb-search input { user-select:text; -webkit-user-select:text; }

    /* ---- Layout ---- */
    .app { display:flex; height:100vh; }

    /* ---- Sidebar ---- */
    .sidebar {
        width:260px; min-width:260px;
        background:var(--bg2); border-right:1px solid var(--border);
        display:flex; flex-direction:column; overflow:hidden;
    }
    .sb-hdr {
        padding:12px 14px 8px; border-bottom:1px solid var(--border);
        font-size:11px; font-weight:600; text-transform:uppercase;
        letter-spacing:0.5px; color:var(--text2); flex-shrink:0;
    }
    .sb-search {
        padding:6px 10px; border-bottom:1px solid var(--border); flex-shrink:0;
    }
    .sb-search input {
        width:100%; padding:5px 8px; background:var(--bg);
        border:1px solid var(--border); border-radius:4px;
        color:var(--text); font-size:11px; outline:none;
    }
    .sb-search input:focus { border-color:var(--accent); }
    .sb-stats {
        padding:6px 10px; background:var(--bg3); border-bottom:1px solid var(--border);
        display:flex; align-items:center; gap:8px; font-size:11px; flex-shrink:0;
    }
    .sb-stats .add { color:var(--green); font-weight:600; }
    .sb-stats .del { color:var(--red); font-weight:600; }
    .tree-list { flex:1; overflow-y:auto; padding:2px 0; }
    .tree-item {
        display:flex; align-items:center; padding:6px 10px 6px 12px;
        cursor:pointer; border-left:3px solid transparent;
        color:var(--text); font-size:11px; text-decoration:none;
        transition:background 0.1s;
    }
    .tree-item:hover { background:rgba(255,255,255,0.03); }
    .tree-item.active { background:var(--accent-bg); border-left-color:var(--accent); }
    .tree-item .icon { margin-right:6px; font-size:13px; opacity:0.7; flex-shrink:0; }
    .tree-item .info { flex:1; min-width:0; }
    .tree-item .name {
        font-family:var(--mono); font-size:11px; white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis; display:block;
    }
    .tree-item .dir {
        font-size:9px; color:var(--text3); white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis; display:block;
    }
    .tree-badge {
        font-size:9px; padding:1px 5px; border-radius:8px;
        font-weight:500; flex-shrink:0; margin-left:4px;
    }
    .tree-badge.add { background:var(--green-bg); color:var(--green); }
    .tree-badge.del { background:var(--red-bg); color:var(--red); }
    .tree-badge.new { background:var(--green-bg2); color:var(--green); }
    .tree-badge.rm  { background:var(--red-bg2); color:var(--red); }

    /* ---- Content ---- */
    .content { flex:1; display:flex; flex-direction:column; overflow:hidden; }

    /* View toggle */
    .view-bar {
        display:flex; align-items:center; padding:5px 10px;
        background:var(--bg2); border-bottom:1px solid var(--border);
        gap:8px; flex-shrink:0;
    }
    .view-bar .spacer { flex:1; }
    .view-tgl {
        display:flex; border:1px solid var(--border); border-radius:4px;
        overflow:hidden;
    }
    .view-tgl button {
        padding:4px 12px; font-size:11px; font-family:var(--sans);
        background:transparent; color:var(--text2); border:none;
        cursor:pointer; transition:all 0.15s ease; position:relative;
    }
    .view-tgl button:first-child { border-right:1px solid var(--border); }
    .view-tgl button.active {
        background:var(--accent-bg); color:var(--accent); font-weight:500;
    }
    .view-tgl button:hover:not(.active) {
        background:rgba(255,255,255,0.04); color:var(--text);
    }

    /* ---- Diff scroll area ---- */
    .diff-scroll { flex:1; overflow:auto; }

    /* 横向滚动通过 JS 同步各行的滚动位置，实现容器级一致的滚动体验 */

    /* ---- File block ---- */
    .file-block { border-bottom:1px solid var(--border); }
    .file-block:last-child { border-bottom:none; }

    .file-hdr {
        position:sticky; top:0; z-index:10;
        background:var(--bg3); padding:6px 12px;
        border-bottom:1px solid var(--border);
        display:flex; align-items:center; gap:8px;
    }
    .file-hdr .fh-icon { font-size:14px; }
    .file-hdr .fh-path {
        color:var(--accent); font-family:var(--mono); font-size:12px;
        flex:1; min-width:0; white-space:nowrap; overflow:hidden;
        text-overflow:ellipsis; cursor:pointer; text-decoration:none;
    }
    .file-hdr .fh-path:hover { text-decoration:underline; }
    .file-hdr .fh-add { color:var(--green); font-family:var(--mono); font-size:11px; }
    .file-hdr .fh-del { color:var(--red); font-family:var(--mono); font-size:11px; }
    .file-hdr .fh-open {
        padding:3px 10px; font-size:10px; font-family:var(--sans);
        background:var(--bg2); border:1px solid var(--border); border-radius:4px;
        color:var(--text2); cursor:pointer; flex-shrink:0;
        transition:all 0.15s ease;
    }
    .file-hdr .fh-open:hover {
        background:rgba(88,166,255,0.1);
        border-color:var(--accent); color:var(--accent);
    }
    .file-hdr .fh-open:active {
        background:rgba(88,166,255,0.2);
    }

    /* ================================================================ */
    /*  UNIFIED VIEW — 删除块 → 新增块，不逐行配对                       */
    /* ================================================================ */
    .diff-unified { width:100%; }

    .du-row {
        display:flex; align-items:stretch;
        min-height:var(--lh); font-family:var(--mono);
        font-size:var(--fs); line-height:var(--lh);
    }
    .du-num {
        width:44px; min-width:44px; padding:0 6px;
        text-align:right; color:var(--text3); user-select:none;
        font-size:10px; line-height:var(--lh);
    }
    .du-sign {
        width:16px; min-width:16px; text-align:center;
        user-select:none; font-size:11px; line-height:var(--lh);
    }
    .du-code {
        flex:1; padding:0 10px; white-space:pre;
        line-height:var(--lh); overflow-x:auto; overflow-y:hidden;
    }

    /* Unified: hunk header */
    .du-row.hh {
        background:var(--accent-bg);
        border-top:1px solid rgba(56,139,253,0.18);
        border-bottom:1px solid rgba(56,139,253,0.18);
    }
    .du-row.hh .du-num { color:transparent; }
    .du-row.hh .du-sign { color:var(--accent); }
    .du-row.hh .du-code { color:var(--accent); font-size:11px; }

    /* Unified: delete block */
    .du-row.del-s { background:var(--red-bg); }
    .du-row.del-m { background:var(--red-bg); }
    .du-row.del-e { background:var(--red-bg); border-bottom:2px solid rgba(248,81,73,0.3); }
    .du-row.del-s .du-num, .du-row.del-m .du-num, .du-row.del-e .du-num { color:var(--text3); }
    .du-row.del-s .du-sign, .du-row.del-m .du-sign, .du-row.del-e .du-sign { color:var(--red); }
    .du-row.del-s .du-code, .du-row.del-m .du-code, .du-row.del-e .du-code { color:#d0d0d0; }

    /* Unified: add block */
    .du-row.add-s { background:var(--green-bg); }
    .du-row.add-m { background:var(--green-bg); }
    .du-row.add-e { background:var(--green-bg); border-bottom:2px solid rgba(63,185,80,0.3); }
    .du-row.add-s .du-num, .du-row.add-m .du-num, .du-row.add-e .du-num { color:var(--text3); }
    .du-row.add-s .du-sign, .du-row.add-m .du-sign, .du-row.add-e .du-sign { color:var(--green); }
    .du-row.add-s .du-code, .du-row.add-m .du-code, .du-row.add-e .du-code { color:#d0d0d0; }

    /* Unified: context */
    .du-row.ctx { background:transparent; }
    .du-row.ctx .du-num { color:var(--text3); }
    .du-row.ctx .du-sign { color:var(--text3); }
    .du-row.ctx .du-code { color:var(--text); }

    /* Word-diff highlights */
    .w-add { background:rgba(63,185,80,0.3); border-radius:2px; color:#d0ffd0; }
    .w-del { background:rgba(248,81,73,0.3); border-radius:2px; color:#ffd0d0; }

    /* ================================================================ */
    /*  SPLIT VIEW — 真正的左右并排                                     */
    /* ================================================================ */
    .diff-split { width:100%; }

    .ds-pair {
        display:flex; align-items:stretch;
        min-height:var(--lh); font-family:var(--mono);
        font-size:var(--fs); line-height:var(--lh);
    }
    .ds-side {
        flex:1; min-width:0; display:flex; align-items:stretch;
    }
    .ds-side:first-child { border-right:1px solid var(--border); }
    .ds-num {
        width:38px; min-width:38px; padding:0 4px;
        text-align:right; color:var(--text3); user-select:none;
        font-size:10px; line-height:var(--lh);
    }
    .ds-code {
        flex:1; padding:0 8px; white-space:pre;
        line-height:var(--lh); overflow-x:auto; overflow-y:hidden;
    }

    /* Split: hunk header */
    .ds-pair.hh { background:var(--accent-bg); }
    .ds-pair.hh .ds-num { color:transparent; }

    /* Split: deleted (left red, right empty) */
    .ds-pair.del .ds-side:first-child { background:var(--red-bg); }
    .ds-pair.del .ds-side:first-child .ds-code { color:#d0d0d0; }
    .ds-pair.del .ds-side:first-child .ds-num { color:var(--red); }
    .ds-pair.del .ds-side:last-child { background:rgba(255,255,255,0.01); }
    .ds-pair.del .ds-side:last-child .ds-code { color:transparent; }
    .ds-pair.del .ds-side:last-child .ds-num { color:transparent; }

    /* Split: added (left empty, right green) */
    .ds-pair.add .ds-side:first-child { background:rgba(255,255,255,0.01); }
    .ds-pair.add .ds-side:first-child .ds-code { color:transparent; }
    .ds-pair.add .ds-side:first-child .ds-num { color:transparent; }
    .ds-pair.add .ds-side:last-child { background:var(--green-bg); }
    .ds-pair.add .ds-side:last-child .ds-code { color:#d0d0d0; }
    .ds-pair.add .ds-side:last-child .ds-num { color:var(--green); }

    /* Split: modified (both have content) */
    .ds-pair.mod .ds-side:first-child { background:var(--red-bg); }
    .ds-pair.mod .ds-side:first-child .ds-code { color:#d0d0d0; }
    .ds-pair.mod .ds-side:first-child .ds-num { color:var(--red); }
    .ds-pair.mod .ds-side:last-child { background:var(--green-bg); }
    .ds-pair.mod .ds-side:last-child .ds-code { color:#d0d0d0; }
    .ds-pair.mod .ds-side:last-child .ds-num { color:var(--green); }

    /* Split: context */
    .ds-pair.ctx .ds-side { background:transparent; }
    .ds-pair.ctx .ds-side .ds-code { color:var(--text); }
    .ds-pair.ctx .ds-side .ds-num { color:var(--text3); }

    /* ---- Fold button ---- */
    .fold-btn {
        display:flex; align-items:center; justify-content:center;
        padding:4px 14px;
        background:var(--accent-bg);
        border-top:1px solid rgba(56,139,253,0.15);
        border-bottom:1px solid rgba(56,139,253,0.15);
        color:var(--accent); cursor:pointer;
        font-size:10px; font-family:var(--sans); gap:6px;
        user-select:none; transition:all 0.15s ease;
    }
    .fold-btn:hover {
        background:rgba(56,139,253,0.22);
        color:#79c0ff;
    }
    .fold-btn:active {
        background:rgba(56,139,253,0.30);
    }

    /* ---- Empty state ---- */
    .empty-state {
        text-align:center; padding:80px 20px; color:var(--text2);
    }
    .empty-state .icon { font-size:48px; opacity:0.3; margin-bottom:12px; }
    .empty-state h2 { font-size:16px; font-weight:600; color:var(--text); }

    /* ---- Scrollbar ---- */
    ::-webkit-scrollbar { width:6px; height:6px; }
    ::-webkit-scrollbar-track { background:transparent; }
    ::-webkit-scrollbar-thumb { background:rgba(255,255,255,0.12); border-radius:3px; }
    ::-webkit-scrollbar-thumb:hover { background:rgba(255,255,255,0.22); }
    /* 代码区域滚动条极细，减少视觉杂乱 */
    .du-code::-webkit-scrollbar, .ds-code::-webkit-scrollbar { height:3px; }
    .du-code::-webkit-scrollbar-thumb, .ds-code::-webkit-scrollbar-thumb { background:rgba(255,255,255,0.10); }
    .du-code::-webkit-scrollbar-thumb:hover, .ds-code::-webkit-scrollbar-thumb:hover { background:rgba(255,255,255,0.20); }

    /* ---- Initial loading pulse ---- */
    .diff-scroll:empty::after {
        content: '加载差异中...';
        display:flex; align-items:center; justify-content:center;
        height:200px; color:var(--text2); font-size:13px;
        animation:pulse 1.5s ease-in-out infinite;
    }
    @keyframes pulse {
        0%,100% { opacity:0.3; } 50% { opacity:0.8; }
    }
    .file-block[data-placeholder] { min-height:40px; }
</style>
"""


# ==========================================================================
# 3. HTML 生成器
# ==========================================================================
class DiffHtmlGenerator:
    """Git Diff HTML 生成器 - Cursor 风格"""

    @classmethod
    def escape_html(cls, text: str) -> str:
        if not text:
            return ""
        return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))

    @classmethod
    def generate_html_report(
        cls, diff_output: str, session_id: str = "",
        lazy_load: bool = True, default_view: str = "split"
    ) -> str:
        if diff_output is None:
            diff_output = ""

        files = cls._parse_diff(diff_output)
        total_adds = sum(f["additions"] for f in files)
        total_dels = sum(f["deletions"] for f in files)
        total_files = len(files)

        tree_html = ""
        blocks_html = ""
        preload_n = 1 if lazy_load and total_files > 1 else total_files
        files_meta = cls._gen_files_meta(files)

        for i, fi in enumerate(files):
            fid = f"file-{i}"
            tree_html += cls._tree_item(fi, fid)
            # 将每文件的 lines 单独序列化，避免一个巨大 JSON 导致解析卡死
            lines_json = json.dumps(fi["lines"]).decode("utf-8")
            if i < preload_n:
                blocks_html += cls._file_block(fi, fid)
                blocks_html += f'\n<script type="application/json" id="ld-{fid}">{lines_json}</script>'
            elif lazy_load:
                blocks_html += (
                    f'<div class="file-block" id="{fid}" data-placeholder="true"></div>'
                    f'\n<script type="application/json" id="ld-{fid}">{lines_json}</script>'
                )

        if not files:
            blocks_html = """<div class="empty-state"><div class="icon">&#10003;</div>
                <h2>没有检测到文件差异</h2>
                <p>当前会话没有修改任何文件</p></div>"""

        split_on = "active" if default_view == "split" else ""
        unified_on = "active" if default_view == "unified" else ""
        dv = json.dumps(default_view).decode("utf-8")

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>文件差异对比</title>{THEME_CSS}</head>
<body>
<div class="app">
<div class="sidebar">
    <div class="sb-hdr">已修改的文件 <span style="font-weight:400;color:var(--text3)">({total_files})</span></div>
    <div class="sb-search"><input placeholder="搜索文件..." oninput="filterFiles(this.value)"></div>
    <div class="sb-stats">
        <span class="add">+{total_adds}</span> <span class="del">-{total_dels}</span>
        <span style="flex:1"></span>
        <button class="fh-open" id="toggle-fold-btn" onclick="toggleFold()">全部展开</button>
    </div>
    <div class="tree-list">{tree_html}</div>
</div>
<div class="content">
    <div class="view-bar">
        <div class="view-tgl">
            <button class="{split_on}" id="btn-split" onclick="switchView('split')">并排对比</button>
            <button class="{unified_on}" id="btn-unified" onclick="switchView('unified')">统一直列</button>
        </div>
        <span class="spacer"></span>
        <span style="font-size:9px;color:var(--text3)">点击文件路径可在编辑器中打开</span>
    </div>
    <div class="diff-scroll" id="diff-scroll">{blocks_html}</div>
</div>
</div>
<script>
window._dm={files_meta};window._lf=new Set({list(range(preload_n))});
window._pc={preload_n};window._cv={dv};window._ae=false;

function esc(s){{var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}}

function wd(o,n){{
    if(o.length+n.length>2000)return{{o:esc(o),n:esc(n)}};
    var a=Array.from(o),b=Array.from(n),ma=a.length,mb=b.length,p=0,ss=0;
    while(p<ma&&p<mb&&a[p]===b[p])p++;
    while(ss<ma-p&&ss<mb-p&&a[ma-1-ss]===b[mb-1-ss])ss++;
    var pre=a.slice(0,p).join(''),suf=a.slice(ma-ss).join('');
    var om=a.slice(p,ma-ss).join(''),nm=b.slice(p,mb-ss).join('');
    return{{o:esc(pre)+(om?'<span class="w-del">'+esc(om)+'</span>':'')+esc(suf),
             n:esc(pre)+(nm?'<span class="w-add">'+esc(nm)+'</span>':'')+esc(suf)}};
}}

// ---- Unified: block-based rows ----
function uRow(cls,on,nn,sg,cd,dt){{
    return '<div class="du-row '+cls+'" data-type="'+(dt||'')+'">'+
        '<span class="du-num">'+(on||'')+'</span>'+
        '<span class="du-num">'+(nn||'')+'</span>'+
        '<span class="du-sign">'+(sg||'')+'</span>'+
        '<span class="du-code">'+(cd||'')+'</span></div>';
}}

// ---- Split: pair-based rows ----
function sPair(cls,on,nn,oc,nc,dt){{
    return '<div class="ds-pair '+cls+'" data-type="'+(dt||'')+'">'+
        '<div class="ds-side"><span class="ds-num">'+(on||'')+'</span><span class="ds-code">'+(oc||'')+'</span></div>'+
        '<div class="ds-side"><span class="ds-num">'+(nn||'')+'</span><span class="ds-code">'+(nc||'')+'</span></div></div>';
}}

function genRows(ls){{
    var uh='',sh='',oln=1,nln=1,i=0;
    while(i<ls.length){{
        var ln=ls[i];
        if(ln.indexOf('@@')===0){{
            var m=ln.match(/@@ -(\\d+),?\\d* \\+(\\d+),?\\d* @@/);
            if(m){{oln=parseInt(m[1]);nln=parseInt(m[2]);}}
            uh+=uRow('hh','','','',esc(ln),'hunk-header');
            sh+=sPair('hh','','','','','hunk-header');
            i++;
        }}else if(ln.indexOf('-')===0&&ln.indexOf('---')!==0){{
            // Collect delete block
            var ds=i,delLines=[],delNums=[];
            while(i<ls.length&&ls[i].indexOf('-')===0&&ls[i].indexOf('---')!==0){{
                delLines.push(ls[i].substring(1)); delNums.push(oln++); i++;
            }}
            var dc=delLines.length;
            // Collect add block (if immediately follows)
            var addLines=[],addNums=[];
            if(i<ls.length&&ls[i].indexOf('+')===0&&ls[i].indexOf('+++')!==0){{
                while(i<ls.length&&ls[i].indexOf('+')===0&&ls[i].indexOf('+++')!==0){{
                    addLines.push(ls[i].substring(1)); addNums.push(nln++); i++;
                }}
            }}
            var ac=addLines.length;
            var pc=Math.min(dc,ac);

            // Build unified: delete block + add block
            for(var k=0;k<dc;k++){{
                var cl=k===0&&dc>1?'del-s':k===dc-1?'del-e':'del-m';
                if(dc===1)cl='del-e';
                var w=pc>0&&k<pc?wd(delLines[k],addLines[k]):null;
                uh+=uRow(cl,delNums[k],'',w?'-':'-',w?w.o:esc(delLines[k]),'deleted');
            }}
            for(var k=0;k<ac;k++){{
                var cl=k===0&&ac>1?'add-s':k===ac-1?'add-e':'add-m';
                if(ac===1)cl='add-e';
                var w=pc>0&&k<pc?wd(delLines[k],addLines[k]):null;
                uh+=uRow(cl,'',addNums[k],w?'+':'+',w?w.n:esc(addLines[k]),'added');
            }}

            // Build split: delete (left filled, right empty) then add (left empty, right filled)
            for(var k=0;k<dc;k++){{
                if(k<pc){{
                    var w=wd(delLines[k],addLines[k]);
                    sh+=sPair('mod',delNums[k],addNums[k],w.o,w.n,'modified');
                }}else{{
                    sh+=sPair('del',delNums[k],'',esc(delLines[k]),'','deleted');
                }}
            }}
            for(var k=pc;k<ac;k++){{
                sh+=sPair('add','',addNums[k],'',esc(addLines[k]),'added');
            }}
        }}else if(ln.indexOf('+')===0&&ln.indexOf('+++')!==0){{
            // Standalone added (no preceding delete block) — no border
            uh+=uRow('add-m','',nln,'+',esc(ln.substring(1)),'added');
            sh+=sPair('add','',nln,'',esc(ln.substring(1)),'added');
            nln++;i++;
        }}else if(ln.indexOf(' ')==0){{
            var ct=ln.substring(1);
            uh+=uRow('ctx',oln,nln,'',esc(ct),'context');
            sh+=sPair('ctx',oln,nln,esc(ct),esc(ct),'context');
            oln++;nln++;i++;
        }}else{{
            uh+=uRow('ctx','','','',esc(ln),'context');
            sh+=sPair('ctx','','','','','context');
            i++;
        }}
    }}
    return{{u:uh,s:sh}};
}}

function genBlock(fi,lines){{
    var as=fi.additions>0?'<span class="fh-add">+'+fi.additions+'</span>':'';
    var ds=fi.deletions>0?'<span class="fh-del">-'+fi.deletions+'</span>':'';
    var ep=encodeURIComponent(fi.path);
    var h='<div class="file-hdr">'+
        '<span class="fh-icon">'+fi.icon+'</span>'+
        '<a class="fh-path" href="drifox://open-file?path='+ep+'" title="点击打开">'+esc(fi.path)+'</a>'+
        as+ds+
        '<button class="fh-open" data-path="'+ep+'" onclick="openFile(decodeURIComponent(this.dataset.path))">打开</button></div>';
    var r=genRows(lines);
    return h+'<div class="diff-unified" data-view="unified" style="display:none">'+r.u+'</div>'+
        '<div class="diff-split" data-view="split">'+r.s+'</div>';
}}


function loadFile(id,idx){{
    if(window._lf.has(idx))return;
    window._lf.add(idx);
    var fi=window._dm[idx];if(!fi)return;
    var el=document.getElementById(id);
    if(el){{
        var ld=document.getElementById('ld-'+id);
        var lines=ld?JSON.parse(ld.textContent):[];
        el.innerHTML=genBlock(fi,lines);
        el.removeAttribute('data-placeholder');
        applyFold(el);applyView(el);
        if(window._do)window._do.observe(el);
    }}
}}

function applyView(c){{
    var v=window._cv;
    c.querySelectorAll('[data-view]').forEach(function(t){{t.style.display=t.getAttribute('data-view')===v?'':'none';}});
}}

function switchView(v){{
    window._cv=v;
    document.querySelectorAll('.view-tgl button').forEach(function(b){{b.classList.remove('active');}});
    document.getElementById(v==='split'?'btn-split':'btn-unified').classList.add('active');
    document.querySelectorAll('.file-block').forEach(function(b){{applyView(b);}});
}}

// Context folding
function applyFold(c){{
    var TH=8,HD=3,TL=3;
    // For unified: fold .du-row where data-type=context
    // For split: fold .ds-pair where data-type=context
    var tables=c.querySelectorAll('.diff-unified, .diff-split');
    tables.forEach(function(t){{
        var isSplit=t.classList.contains('diff-split');
        var sel=isSplit?'.ds-pair':'.du-row';
        var rows=t.querySelectorAll(sel);
        if(!rows.length)return;
        var ranges=[],start=-1;
        for(var i=0;i<rows.length;i++){{
            var dt=rows[i].getAttribute('data-type');
            if(dt==='context'){{if(start===-1)start=i;}}
            else{{if(start!==-1){{ranges.push({{s:start,e:i-1,
                prev:start>0?rows[start-1].getAttribute('data-type'):null,
                next:rows[i].getAttribute('data-type')}});start=-1;}}}}
        }}
        if(start!==-1){{ranges.push({{s:start,e:rows.length-1,
            prev:start>0?rows[start-1].getAttribute('data-type'):null,next:null}});}}
        for(var r=ranges.length-1;r>=0;r--){{
            var rg=ranges[r],cnt=rg.e-rg.s+1;
            var kh=(rg.prev==='hunk-header'||rg.prev===null)?0:HD;
            var kt=(rg.next==='hunk-header'||rg.next===null)?0:TL;
            var fc=cnt-kh-kt;if(fc<=0)continue;
            var fs=rg.s+kh,fe=rg.e-kt,hidden=[];
            for(var i=fs;i<=fe;i++){{rows[i].style.display='none';hidden.push(rows[i]);}}
            var btn=document.createElement('div');btn.className='fold-btn';
            btn.innerHTML='<span>&#9662;</span> '+fc+' 行未更改';
            btn._h=hidden;
            btn.addEventListener('click',function(){{this._h.forEach(function(rr){{rr.style.display='';}});this.style.display='none';}});
            var anchor=fe+1<rows.length?rows[fe+1]:null;
            if(anchor&&anchor.parentNode===t)t.insertBefore(btn,anchor);else t.appendChild(btn);
        }}
    }});
}}

function toggleFold(){{
    window._ae=!window._ae;
    var tb=document.getElementById('toggle-fold-btn');
    document.querySelectorAll('.fold-btn').forEach(function(btn){{
        var h=btn._h;if(!h||!h.length)return;
        if(window._ae){{h.forEach(function(r){{r.style.display='';}});btn.style.display='none';}}
        else{{h.forEach(function(r){{r.style.display='none';}});btn.style.display='';}}
    }});
    if(tb)tb.textContent=window._ae?'全部收缩':'全部展开';
}}

function openFile(p){{
    var a=document.createElement('a');
    a.href='drifox://open-file?path='+encodeURIComponent(p);
    document.body.appendChild(a);a.click();document.body.removeChild(a);
}}

// File tree interaction
document.querySelectorAll('.tree-item').forEach(function(item){{
    item.addEventListener('click',function(e){{
        e.preventDefault();
        var tid=this.getAttribute('data-target');
        var idx=parseInt(tid.replace('file-',''));
        loadFile(tid,idx);
        document.querySelectorAll('.tree-item').forEach(function(el){{el.classList.remove('active');}});
        this.classList.add('active');
        var t=document.getElementById(tid);
        if(t)t.scrollIntoView({{behavior:'smooth',block:'start'}});
    }});
}});

document.addEventListener('keydown',function(e){{
    if(['j','k','ArrowDown','ArrowUp'].indexOf(e.key)===-1)return;
    var items=Array.from(document.querySelectorAll('.tree-item:not([style*="display: none"])'));
    if(!items.length)return;
    var act=document.querySelector('.tree-item.active'),idx=items.indexOf(act);
    if(idx===-1)idx=0;
    if(e.key==='j'||e.key==='ArrowDown')idx=Math.min(idx+1,items.length-1);
    else idx=Math.max(idx-1,0);
    items[idx].click();e.preventDefault();
}});

var _st=null;
function filterFiles(q){{
    clearTimeout(_st);
    _st=setTimeout(function(){{
        q=q.toLowerCase();
        document.querySelectorAll('.tree-item').forEach(function(item){{
            var nm=(item.querySelector('.name')||item).textContent.toLowerCase();
            var dr=(item.querySelector('.dir')||{{textContent:''}}).textContent.toLowerCase();
            var tl=(item.getAttribute('title')||'').toLowerCase();
            item.style.display=(tl+nm+dr).indexOf(q)===-1&&nm.indexOf(q)===-1&&dr.indexOf(q)===-1?'none':'';
        }});
        if(q&&!window._ae)toggleFold();
    }},q?150:0);
}}

window._do=new IntersectionObserver(function(es){{
    es.forEach(function(e){{
        if(e.isIntersecting){{
            var id=e.target.id,idx=parseInt(id.replace('file-',''));
            loadFile(id,idx);
            var ti=document.querySelector('.tree-item[data-target="'+id+'"]');
            if(ti){{document.querySelectorAll('.tree-item').forEach(function(el){{el.classList.remove('active');}});ti.classList.add('active');}}
        }}
    }});
}},{{threshold:0.1,rootMargin:'200px'}});

document.querySelectorAll('.file-block').forEach(function(b){{window._do.observe(b);}});

requestAnimationFrame(function(){{
    document.querySelectorAll('.file-block').forEach(function(b){{applyFold(b);applyView(b);}});
    var f=document.querySelector('.tree-item');if(f)f.classList.add('active');
}});

// ---- Horizontal scroll sync ----
(function(){{
    function syncCodeCells(container, rowSelector, cellSelector){{
        container.querySelectorAll(rowSelector).forEach(function(row){{
            var cells=row.querySelectorAll(cellSelector);
            if(cells.length<2)return;
            var _busy=false;
            cells.forEach(function(cell){{
                cell.addEventListener('scroll',function(){{
                    if(_busy)return;_busy=true;
                    var l=this.scrollLeft;
                    cells.forEach(function(o){{if(o!==this)o.scrollLeft=l;}},this);
                    _busy=false;
                }});
            }});
        }});
    }}
    // Unified: 每个 .du-row 内的所有 .du-code 同步（同一行只有一个，无操作）
    // Split: 每个 .ds-pair 内左右两个 .ds-code 同步
    document.querySelectorAll('.file-block').forEach(function(block){{
        syncCodeCells(block,'.ds-pair','.ds-code');
    }});
    // 懒加载新文件时自动绑定
    var _origLoad=loadFile;
    window.loadFile=function(id,idx){{
        _origLoad(id,idx);
        requestAnimationFrame(function(){{
            var block=document.getElementById(id);
            if(block){{
                syncCodeCells(block,'.ds-pair','.ds-code');
            }}
        }});
    }};
}})();
</script>
</body></html>"""

    # ======================================================================
    # Diff 解析
    # ======================================================================
    @classmethod
    def _parse_diff(cls, diff_output: str) -> List[Dict]:
        if not diff_output:
            return []
        files = []
        cur_file = None
        cur_lines = []
        cur_stats = {"additions": 0, "deletions": 0}
        cur_status = "modified"
        for line in diff_output.split("\n"):
            if line.startswith("--- "):
                if cur_file and cur_lines:
                    merged = DiffHunkMerger.merge_file_hunks(cur_lines)
                    files.append({"path": cur_file, "additions": cur_stats["additions"],
                                  "deletions": cur_stats["deletions"], "status": cur_status,
                                  "lines": merged})
                parts = line[4:].strip()
                cur_status = "added" if parts == "/dev/null" else "modified"
                if parts.startswith("a/") or parts.startswith("b/"):
                    cur_file = parts[2:]
                else:
                    cur_file = parts
                cur_lines = []
                cur_stats = {"additions": 0, "deletions": 0}
                continue
            if line.startswith("+++ "):
                if line[4:].strip() == "/dev/null" and cur_status != "added":
                    cur_status = "deleted"
                continue
            if cur_file is None:
                continue
            if line.startswith("+") and not line.startswith("+++"):
                cur_stats["additions"] += 1
            elif line.startswith("-") and not line.startswith("---"):
                cur_stats["deletions"] += 1
            cur_lines.append(line)
        if cur_file and cur_lines:
            merged = DiffHunkMerger.merge_file_hunks(cur_lines)
            files.append({"path": cur_file, "additions": cur_stats["additions"],
                          "deletions": cur_stats["deletions"], "status": cur_status,
                          "lines": merged})
        return files

    # ---- Tree item ----
    @classmethod
    def _tree_item(cls, fi: Dict, fid: str) -> str:
        p = fi["path"]
        adds = fi["additions"]
        dels = fi["deletions"]
        st = fi.get("status", "modified")
        icon = cls._icon(p)
        name = Path(p).name
        d = str(Path(p).parent).replace("\\", "/")
        if d == ".": d = ""
        badges = ""
        if st == "added":
            badges += '<span class="tree-badge new">新增</span>'
        elif st == "deleted":
            badges += '<span class="tree-badge rm">删除</span>'
        else:
            if adds > 0: badges += f'<span class="tree-badge add">+{adds}</span>'
            if dels > 0: badges += f'<span class="tree-badge del">-{dels}</span>'
        dir_h = f'<span class="dir">{cls.escape_html(d)}</span>' if d else ""
        return f'''<a href="#{fid}" class="tree-item" data-target="{fid}" title="{cls.escape_html(p)}">
            <span class="icon">{icon}</span>
            <div class="info"><span class="name">{cls.escape_html(name)}</span>{dir_h}</div>{badges}</a>'''

    # ---- File block (pre-rendered) ----
    @classmethod
    def _file_block(cls, fi: Dict, fid: str) -> str:
        p = fi["path"]
        adds = fi["additions"]
        dels = fi["deletions"]
        icon = cls._icon(p)
        add_s = f'<span class="fh-add">+{adds}</span>' if adds > 0 else ""
        del_s = f'<span class="fh-del">-{dels}</span>' if dels > 0 else ""
        ep = cls.escape_html(p)
        ep_js = p.replace("\\", "\\\\").replace("'", "\\'")

        header = f'''<div class="file-hdr">
            <span class="fh-icon">{icon}</span>
            <a class="fh-path" href="drifox://open-file?path={ep}" title="点击打开">{ep}</a>
            {add_s}{del_s}
            <button class="fh-open" onclick="openFile('{ep_js}')">打开</button></div>'''

        rows = cls._gen_rows(fi)
        return f'''<div class="file-block" id="{fid}">
            {header}
            <div class="diff-unified" data-view="unified" style="display:none">{rows["u"]}</div>
            <div class="diff-split" data-view="split">{rows["s"]}</div></div>'''

    @classmethod
    def _gen_rows(cls, fi: Dict) -> Dict[str, str]:
        """生成统一视图和分列视图的行 HTML。核心逻辑：块状聚合。"""
        lines = fi["lines"]
        u = []
        s = []
        oln = 1; nln = 1; i = 0

        while i < len(lines):
            ln = lines[i]

            if ln.startswith("@@"):
                m = _HUNK_HEADER_PATTERN.search(ln)
                if m:
                    oln = int(m.group(1)); nln = int(m.group(2))
                esc = cls.escape_html(ln)
                u.append(f'<div class="du-row hh" data-type="hunk-header">'
                         f'<span class="du-num"></span><span class="du-num"></span>'
                         f'<span class="du-sign"></span><span class="du-code">{esc}</span></div>')
                s.append('<div class="ds-pair hh" data-type="hunk-header">'
                         '<div class="ds-side"><span class="ds-num"></span><span class="ds-code"></span></div>'
                         '<div class="ds-side"><span class="ds-num"></span><span class="ds-code"></span></div></div>')
                i += 1

            elif ln.startswith("-") and not ln.startswith("---"):
                # 收集删除块
                del_start = i
                del_texts = []; del_nums = []
                while i < len(lines) and lines[i].startswith("-") and not lines[i].startswith("---"):
                    del_texts.append(lines[i][1:]); del_nums.append(oln); oln += 1; i += 1
                dc = len(del_texts)

                # 收集紧随的新增块
                add_start = i
                add_texts = []; add_nums = []
                while i < len(lines) and lines[i].startswith("+") and not lines[i].startswith("+++"):
                    add_texts.append(lines[i][1:]); add_nums.append(nln); nln += 1; i += 1
                ac = len(add_texts)
                pc = min(dc, ac)

                # ===== UNIFIED: 删除块 + 新增块，不逐行配对 =====
                for k in range(dc):
                    cls_suffix = "del-s" if (k == 0 and dc > 1) else ("del-e" if k == dc - 1 else "del-m")
                    if dc == 1: cls_suffix = "del-e"
                    # 仅在可配对的行上做 word-diff
                    if k < pc:
                        o_h, n_h = cls._word_diff(del_texts[k], add_texts[k])
                        u.append(f'<div class="du-row {cls_suffix}" data-type="deleted">'
                                 f'<span class="du-num">{del_nums[k]}</span><span class="du-num"></span>'
                                 f'<span class="du-sign">-</span><span class="du-code">{o_h}</span></div>')
                    else:
                        u.append(f'<div class="du-row {cls_suffix}" data-type="deleted">'
                                 f'<span class="du-num">{del_nums[k]}</span><span class="du-num"></span>'
                                 f'<span class="du-sign">-</span><span class="du-code">{cls.escape_html(del_texts[k])}</span></div>')
                for k in range(ac):
                    cls_suffix = "add-s" if (k == 0 and ac > 1) else ("add-e" if k == ac - 1 else "add-m")
                    if ac == 1: cls_suffix = "add-e"
                    if k < pc:
                        o_h, n_h = cls._word_diff(del_texts[k], add_texts[k])
                        u.append(f'<div class="du-row {cls_suffix}" data-type="added">'
                                 f'<span class="du-num"></span><span class="du-num">{add_nums[k]}</span>'
                                 f'<span class="du-sign">+</span><span class="du-code">{n_h}</span></div>')
                    else:
                        u.append(f'<div class="du-row {cls_suffix}" data-type="added">'
                                 f'<span class="du-num"></span><span class="du-num">{add_nums[k]}</span>'
                                 f'<span class="du-sign">+</span><span class="du-code">{cls.escape_html(add_texts[k])}</span></div>')

                # ===== SPLIT: 修改行做 word-diff，纯删/纯增行 做空对侧 =====
                for k in range(dc):
                    if k < pc:
                        o_h, n_h = cls._word_diff(del_texts[k], add_texts[k])
                        s.append(f'<div class="ds-pair mod" data-type="modified">'
                                 f'<div class="ds-side"><span class="ds-num">{del_nums[k]}</span><span class="ds-code">{o_h}</span></div>'
                                 f'<div class="ds-side"><span class="ds-num">{add_nums[k]}</span><span class="ds-code">{n_h}</span></div></div>')
                    else:
                        s.append(f'<div class="ds-pair del" data-type="deleted">'
                                 f'<div class="ds-side"><span class="ds-num">{del_nums[k]}</span><span class="ds-code">{cls.escape_html(del_texts[k])}</span></div>'
                                 f'<div class="ds-side"><span class="ds-num"></span><span class="ds-code"></span></div></div>')
                for k in range(pc, ac):
                    s.append(f'<div class="ds-pair add" data-type="added">'
                             f'<div class="ds-side"><span class="ds-num"></span><span class="ds-code"></span></div>'
                             f'<div class="ds-side"><span class="ds-num">{add_nums[k]}</span><span class="ds-code">{cls.escape_html(add_texts[k])}</span></div></div>')

            elif ln.startswith("+") and not ln.startswith("+++"):
                # 孤立新增（前面没有删除行）— 无底部边框
                u.append(f'<div class="du-row add-m" data-type="added">'
                         f'<span class="du-num"></span><span class="du-num">{nln}</span>'
                         f'<span class="du-sign">+</span><span class="du-code">{cls.escape_html(ln[1:])}</span></div>')
                s.append(f'<div class="ds-pair add" data-type="added">'
                         f'<div class="ds-side"><span class="ds-num"></span><span class="ds-code"></span></div>'
                         f'<div class="ds-side"><span class="ds-num">{nln}</span><span class="ds-code">{cls.escape_html(ln[1:])}</span></div></div>')
                nln += 1; i += 1

            elif ln.startswith(" "):
                ct = cls.escape_html(ln[1:] if ln else "")
                u.append(f'<div class="du-row ctx" data-type="context">'
                         f'<span class="du-num">{oln}</span><span class="du-num">{nln}</span>'
                         f'<span class="du-sign"></span><span class="du-code">{ct}</span></div>')
                s.append(f'<div class="ds-pair ctx" data-type="context">'
                         f'<div class="ds-side"><span class="ds-num">{oln}</span><span class="ds-code">{ct}</span></div>'
                         f'<div class="ds-side"><span class="ds-num">{nln}</span><span class="ds-code">{ct}</span></div></div>')
                oln += 1; nln += 1; i += 1

            else:
                esc = cls.escape_html(ln)
                u.append(f'<div class="du-row ctx" data-type="context">'
                         f'<span class="du-num"></span><span class="du-num"></span>'
                         f'<span class="du-sign"></span><span class="du-code">{esc}</span></div>')
                s.append('<div class="ds-pair ctx" data-type="context">'
                         '<div class="ds-side"><span class="ds-num"></span><span class="ds-code"></span></div>'
                         '<div class="ds-side"><span class="ds-num"></span><span class="ds-code"></span></div></div>')
                i += 1

        return {"u": "\n".join(u), "s": "\n".join(s)}

    # ---- Word diff ----
    @classmethod
    def _word_diff(cls, old_text: str, new_text: str) -> tuple:
        if len(old_text) + len(new_text) > 2000:
            return cls.escape_html(old_text), cls.escape_html(new_text)
        m = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
        o_p, n_p = [], []
        for tag, i1, i2, j1, j2 in m.get_opcodes():
            if tag == "equal":
                o_p.append(cls.escape_html(old_text[i1:i2]))
                n_p.append(cls.escape_html(new_text[j1:j2]))
            elif tag == "delete":
                o_p.append(f'<span class="w-del">{cls.escape_html(old_text[i1:i2])}</span>')
            elif tag == "insert":
                n_p.append(f'<span class="w-add">{cls.escape_html(new_text[j1:j2])}</span>')
            elif tag == "replace":
                o_p.append(f'<span class="w-del">{cls.escape_html(old_text[i1:i2])}</span>')
                n_p.append(f'<span class="w-add">{cls.escape_html(new_text[j1:j2])}</span>')
        return "".join(o_p), "".join(n_p)

    # ---- Files metadata (no lines, for lightweight initial load) ----
    @classmethod
    def _gen_files_meta(cls, files: List[Dict]) -> str:
        data = []
        for i, fi in enumerate(files):
            data.append({"id": f"file-{i}", "path": fi["path"],
                         "icon": cls._icon(fi["path"]),
                         "additions": fi["additions"], "deletions": fi["deletions"],
                         "status": fi.get("status", "modified")})
        r = json.dumps(data).decode("utf-8")
        return r.replace("</", "\\u003C/")

    # ---- Icon ----
    @classmethod
    def _icon(cls, path: str) -> str:
        if path.endswith(".py"): return "&#128464;"
        elif path.endswith(".json"): return "&#128196;"
        elif path.endswith((".js", ".ts")): return "&#128203;"
        elif path.endswith((".html", ".css", ".htm")): return "&#127760;"
        elif path.endswith((".md", ".txt")): return "&#128214;"
        return "&#128196;"

    # ---- Diff generation ----
    @classmethod
    def get_diff_for_files(cls, file_paths: List[str], session_id: str = "") -> str:
        try:
            existing = [f for f in file_paths if Path(f).exists()]
            if not existing:
                logger.warning("[DiffHtml] 没有有效文件路径")
                return ""
            from app.utils.utils import get_app_data_dir
            bd = get_app_data_dir() / "backups" / session_id
            if not bd.exists():
                logger.warning(f"[DiffHtml] 备份目录不存在: {bd}")
                return ""
            parts = []
            for fp in existing:
                try:
                    stem = Path(fp).stem
                    baks = sorted(f for f in bd.glob(f"{stem}*.bak") if not f.name.endswith(".after.bak"))
                    if not baks: continue
                    with open(baks[0], "r", encoding="utf-8", errors="replace") as f:
                        old = f.read()
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        new = f.read()

                    def _nl(c):
                        ls = c.splitlines(keepends=True)
                        if ls and not ls[-1].endswith("\n"): ls[-1] += "\n"
                        return ls

                    d = difflib.unified_diff(_nl(old), _nl(new),
                                             fromfile=str(Path(fp).resolve()),
                                             tofile=str(Path(fp).resolve()),
                                             lineterm="\n", n=10)
                    dt = "".join(d)
                    if dt: parts.append(dt)
                except Exception as e:
                    logger.warning(f"[DiffHtml] 对比失败 {fp}: {e}")
            return "\n".join(parts)
        except Exception as e:
            logger.error(f"[DiffHtml] 获取 diff 失败: {e}")
            return ""

    @classmethod
    def generate_report_for_files(cls, file_paths: List[str], session_id: str = "") -> str:
        return cls.generate_html_report(cls.get_diff_for_files(file_paths, session_id) or "", session_id)


# ==========================================================================
# 4. 工具调用参数审阅（保持不变）
# ==========================================================================
class ToolPayloadHtmlGenerator:
    @classmethod
    def _safe_json(cls, value) -> str:
        try:
            return json.dumps(value, option=json.OPT_INDENT_2, default=str).decode("utf-8")
        except Exception:
            return str(value)

    @classmethod
    def _field_summary(cls, arguments: Dict) -> str:
        if not isinstance(arguments, dict) or not arguments:
            return '<div class="field-empty">无参数</div>'
        high_risk = {"command","cmd","path","file","files","content","oldString","newString","edits","tasks","url","query"}
        rows = []
        for k, v in arguments.items():
            vt = cls._safe_json(v) if isinstance(v, (dict, list)) else str(v)
            pv = vt.replace("\n", " ")[:140] + ("..." if len(vt) > 140 else "")
            b = "重点" if k in high_risk else "参数"
            bc = "risk" if k in high_risk else "normal"
            rows.append(f"""<div class="field-row"><div class="field-top">
                <span class="field-key">{DiffHtmlGenerator.escape_html(str(k))}</span>
                <span class="field-badge {bc}">{b}</span></div>
                <div class="field-preview">{DiffHtmlGenerator.escape_html(pv)}</div></div>""")
        return "\n".join(rows)

    @classmethod
    def generate_html_report(cls, tool_name: str, tool_call_id: str = "", arguments: Dict = None) -> str:
        args = arguments or {}
        pa = cls._safe_json(args)
        ej = DiffHtmlGenerator.escape_html(pa)
        et = DiffHtmlGenerator.escape_html(tool_name or "unknown")
        ec = DiffHtmlGenerator.escape_html(tool_call_id or "")
        fr = cls._field_summary(args)
        return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>工具调用参数预览</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{--bg:#0d1117;--bg2:#161b22;--bg3:#1c2128;--border:#30363d;--text:#c9d1d9;--text2:#8b949e;--link:#58a6ff;--green:#3fb950;--yellow:#d29922;--red:#f85149;--blue-bg:rgba(31,111,235,0.15);--yel-bg:rgba(187,128,9,0.18);--mono:'Consolas',monospace;--sans:-apple-system,sans-serif}}
body{{font-family:var(--sans);background:var(--bg);color:var(--text);height:100vh;overflow:hidden;font-size:12px}}
.ra{{display:flex;height:100vh}}
.sb{{width:320px;min-width:320px;background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}}
.sb-h{{padding:14px 16px;border-bottom:1px solid var(--border)}}
.eyebrow{{color:var(--text2);font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}}
.tool-name{{color:var(--link);font-family:var(--mono);font-size:15px;overflow-wrap:anywhere}}
.call-id{{margin-top:8px;color:var(--text2);font-family:var(--mono);font-size:11px;overflow-wrap:anywhere}}
.sm{{padding:10px 16px;background:var(--bg3);color:var(--text2);border-bottom:1px solid var(--border)}}
.fl{{flex:1;overflow-y:auto;padding:10px}}
.fr{{padding:10px;border:1px solid var(--border);border-radius:6px;background:rgba(255,255,255,0.02);margin-bottom:8px}}
.ft{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.fk{{flex:1;color:var(--text);font-family:var(--mono);overflow-wrap:anywhere}}
.fb{{padding:1px 6px;border-radius:10px;font-size:11px;flex-shrink:0}}
.fb.risk{{background:var(--yel-bg);color:var(--yellow)}}
.fb.normal{{background:var(--blue-bg);color:var(--link)}}
.fp{{color:var(--text2);font-family:var(--mono);font-size:11px;overflow-wrap:anywhere}}
.fe{{color:var(--text2);padding:16px}}
.ct{{flex:1;display:flex;flex-direction:column;overflow:hidden}}
.ch{{padding:10px 16px;background:var(--bg3);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}}
.ctt{{flex:1;color:var(--text);font-weight:600}}
.cbtn{{padding:4px 10px;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--text2);cursor:pointer;font-size:12px}}
.cbtn:hover{{border-color:var(--link);color:var(--link)}}
.jw{{flex:1;overflow:auto;background:var(--bg)}}
pre{{padding:16px;font-family:var(--mono);font-size:12px;line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text)}}
.copied{{color:var(--green);margin-left:8px;display:none}}
::-webkit-scrollbar{{width:6px;height:6px}}
::-webkit-scrollbar-track{{background:transparent}}
::-webkit-scrollbar-thumb{{background:rgba(255,255,255,0.12);border-radius:3px}}
</style></head><body>
<div class="ra"><aside class="sb"><div class="sb-h"><div class="eyebrow">工具调用请求</div>
<div class="tool-name">{et}</div><div class="call-id">{ec}</div></div>
<div class="sm">请确认参数符合预期后再允许执行。</div><div class="fl">{fr}</div></aside>
<main class="ct"><div class="ch"><div class="ctt">完整参数 JSON</div>
<button class="cbtn" onclick="cp()">复制</button><span class="copied" id="copied">已复制</span></div>
<div class="jw"><pre id="payload">{ej}</pre></div></main></div>
<script>function cp(){{var t=document.getElementById('payload').innerText;var ta=document.createElement('textarea');
ta.value=t;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);
var tip=document.getElementById('copied');tip.style.display='inline';setTimeout(function(){{tip.style.display='none'}},1400);}}</script>
</body></html>"""


# ==========================================================================
# 5. DiffViewerWindow
# ==========================================================================
class _DiffWebPage(QWebEnginePage):
    """自定义 QWebEnginePage，拦截 drifox:// 协议以打开文件。"""
    def acceptNavigationRequest(self, url: QUrl, _type, is_main_frame):
        if url.scheme() == "drifox" and url.host() == "open-file":
            from urllib.parse import parse_qs, unquote
            qs = parse_qs(url.query())
            path = qs.get("path", [None])[0]
            if path:
                path = unquote(path)
                self._open_file(path)
            return False
        return super().acceptNavigationRequest(url, _type, is_main_frame)

    @staticmethod
    def _open_file(path: str):
        try:
            p = Path(path)
            if not p.exists():
                logger.warning(f"[DiffViewer] 文件不存在: {path}")
                return
            if sys.platform == "win32":
                os.startfile(str(p))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
            logger.info(f"[DiffViewer] 已打开文件: {path}")
        except Exception as e:
            logger.error(f"[DiffViewer] 打开失败: {path} - {e}")


class DiffViewerWindow:
    _instances = []

    @classmethod
    def close_all(cls):
        for w in cls._instances[:]:
            try: w.close()
            except Exception: pass
        cls._instances.clear()

    def __init__(self, parent=None, title: str = "文件差异对比"):
        self._disposed = False
        self._current_html = None
        self._window = QDialog(parent)
        self._window.setWindowTitle(title)
        self._window.resize(1300, 850)
        self._window.setAttribute(Qt.WA_DeleteOnClose, True)
        if parent:
            self._window.setWindowFlags(self._window.windowFlags() | Qt.WindowModal)
        layout = QHBoxLayout(self._window)
        layout.setContentsMargins(0, 0, 0, 0)
        self._webview = QWebEngineView()
        self._profile = create_transient_web_profile(self._window)
        self._page = _DiffWebPage(self._profile, self._webview)
        self._webview.setPage(self._page)
        layout.addWidget(self._webview)
        self._window.destroyed.connect(lambda: self._on_closed())
        self._instances.append(self)
        rss_mb = _get_rss_mb()
        if rss_mb is None:
            logger.debug(f"[DiffViewer] init title={title}")
        else:
            logger.debug(f"[DiffViewer] init title={title}, rss={rss_mb:.1f}MB")

    def _clear_web_content(self):
        if not hasattr(self, "_webview") or self._webview is None:
            return

        try:
            page = self._webview.page()
            if page:
                try:
                    page.runJavaScript("delete window._dm;delete window._lf;")
                except Exception:
                    pass
        except Exception:
            pass

        try:
            self._webview.setHtml("")
        except Exception:
            pass

    def _log_rss(self, stage: str):
        rss_mb = _get_rss_mb()
        if rss_mb is None:
            logger.debug(f"[DiffViewer] {stage}")
        else:
            logger.debug(f"[DiffViewer] {stage}, rss={rss_mb:.1f}MB")

    def _dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._log_rss("dispose_begin")
        self._clear_web_content()
        self._current_html = None
        if self in self._instances:
            self._instances.remove(self)

        try:
            if hasattr(self, "_webview") and self._webview is not None:
                self._webview.deleteLater()
        except Exception:
            pass
        try:
            if hasattr(self, "_profile") and self._profile is not None:
                self._profile.deleteLater()
        except Exception:
            pass
        try:
            if hasattr(self, "_window") and self._window is not None:
                self._window.deleteLater()
        except Exception:
            pass
        self._page = None
        self._profile = None
        self._webview = None
        self._window = None
        self._log_rss("dispose_end")

    def _on_closed(self):
        self._dispose()

    def load_html(self, html_content: str):
        html_len = len(html_content or "")
        self._log_rss(f"load_html_begin html_len={html_len}")
        self._webview.setHtml(html_content or "")
        self._current_html = None
        self._log_rss(f"load_html_called html_len={html_len}")
        QTimer.singleShot(
            1000, lambda: self._log_rss(f"load_html_post_1s html_len={html_len}")
        )

    def show(self):
        self._window.show(); self._window.raise_(); self._window.activateWindow()

    def close(self):
        try:
            self._current_html = None
            self._clear_web_content()
            if self._window is not None:
                self._window.close()
            else:
                self._dispose()
        except Exception:
            pass

    @property
    def widget(self):
        return self._window
