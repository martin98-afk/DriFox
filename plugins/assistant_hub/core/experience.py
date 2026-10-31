# -*- coding: utf-8 -*-
"""experience.py — 助手经验库（工具型 recall/record，对齐 openhanako lib/tools/experience.ts）。

渐进式披露存储：
- experience.md          索引（分类 + 条数 + 摘要 + 路径），rebuild_index 自动生成，不手写
- experience/<分类>.md    分类文件（`- ` 数字列表）

写入只有一条通道：模型经 recall_experience / record_experience 工具自主维护，
无后台反思/压缩管线（原 LLM 整理链路已移除）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_MAX_CATEGORY = 12

# ── 路径 ────────────────────────────────────────────────


def experience_dir(aid_dir: Path) -> Path:
    return Path(aid_dir) / "experience"


def index_path(aid_dir: Path) -> Path:
    return Path(aid_dir) / "experience.md"


def _safe_name(category: str) -> str:
    """分类名 → 文件名（中文保留，滤路径非法字符）。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "", category)
    return name[:32] or "未分类"


def normalize_category(category: str) -> str:
    return (category or "").strip()[:_MAX_CATEGORY]


def existing_categories(aid_dir: Path) -> List[str]:
    """现有分类名列表（按文件名稳定排序）。"""
    d = experience_dir(aid_dir)
    if not d.exists():
        return []
    return sorted(f.stem for f in d.glob("*.md") if f.is_file())


# ── 记录 / 读取 ─────────────────────────────────────────


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def record_entry(aid_dir: Path, category: str, content: str) -> Dict:
    """记录一条经验到分类文件并重建索引；同分类同文本去重。

    分类数为软限制：不做硬性拒绝，靠提示词引导 LLM 优先复用已有分类；
    条目总量超水位时由自动压缩（consolidate）整理。
    """
    cat = normalize_category(category)
    content = (content or "").strip()
    if not cat or not content:
        return {"added": False, "reason": "empty"}
    d = experience_dir(aid_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{_safe_name(cat)}.md"
    existing = _read(f)
    if content in existing:
        return {"added": False, "reason": "duplicate"}
    lines = [ln for ln in existing.splitlines() if ln.strip().startswith("- ")]
    lines.append(f"- {content}")
    header = f"# {cat}\n\n" if not existing.startswith("# ") else ""
    f.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    rebuild_index(aid_dir)
    return {"added": True, "file": str(f)}


def delete_entry(aid_dir: Path, category: str, index: int) -> Dict:
    f = experience_dir(aid_dir) / f"{_safe_name(normalize_category(category))}.md"
    lines = [ln for ln in _read(f).splitlines() if ln.strip().startswith("- ")]
    if not (0 <= index < len(lines)):
        return {"deleted": False, "reason": "index_out_of_range"}
    lines.pop(index)
    header = f"# {normalize_category(category)}\n\n" if lines else ""
    f.write_text(header + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    rebuild_index(aid_dir)
    return {"deleted": True}


def rebuild_index(aid_dir: Path) -> str:
    """扫描 experience/*.md 重建 experience.md 索引；返回索引文本。"""
    docs = list_documents(aid_dir)
    lines = ["# 经验索引", ""]
    if not docs:
        lines.append("（暂无经验）")
    for doc in docs:
        lines.append(f"## {doc['category']}（{doc['count']} 条）")
        if doc["preview"]:
            lines.append(f"{doc['preview']}")
        lines.append(f"→ {experience_dir(aid_dir) / doc['file']}")
        lines.append("")
    text = "\n".join(lines).strip() + "\n"
    p = index_path(aid_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return text


def list_documents(aid_dir: Path) -> List[Dict]:
    d = experience_dir(aid_dir)
    out: List[Dict] = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.md")):
        lines = [ln for ln in _read(f).splitlines() if ln.strip().startswith("- ")]
        preview = "；".join(ln.lstrip("- ").strip()[:20] for ln in lines[:3])
        out.append(
            {
                "category": (f.stem),
                "file": f.name,
                "count": len(lines),
                "preview": preview,
            }
        )
    return out


def read_document(aid_dir: Path, category: str) -> str:
    f = experience_dir(aid_dir) / f"{_safe_name(normalize_category(category))}.md"
    return _read(f)


def read_index(aid_dir: Path) -> str:
    """读索引；缺失时先重建（保证 recall 无参时总有内容）。"""
    p = index_path(aid_dir)
    if not p.exists():
        return rebuild_index(aid_dir)
    return _read(p)
