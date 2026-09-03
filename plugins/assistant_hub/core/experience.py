# -*- coding: utf-8 -*-
"""experience.py — 助手经验库（工具型 recall/record + 定期反思，对齐 openhanako lib/tools/experience.ts）。

渐进式披露存储：
- experience.md          索引（分类 + 条数 + 摘要 + 路径），rebuild_index 自动生成，不手写
- experience/<分类>.md    分类文件（`- ` 数字列表）

反思（reflect）：日批时从近期记忆提炼 0-3 条工作心得，经 LLM 输出 JSON 数组后逐条记录。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_MAX_CATEGORY = 8
# 全库经验条目总数超过该值时触发自动压缩（反思落库后检查）
_CONSOLIDATE_THRESHOLD = 50


def _prompts(*required: str):
    """取 prompts 模块；缓存命中但缺 required 函数（热重载后旧版）→ 重新加载。"""
    key = "assistant_hub_core.memory.prompts"
    mod = sys.modules.get(key)
    if mod is not None and all(hasattr(mod, r) for r in required):
        return mod
    spec = importlib.util.spec_from_file_location(key, str(_THIS.parent / "memory" / "prompts.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


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


# ── 记录 / 读取 ─────────────────────────────────────────


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def record_entry(aid_dir: Path, category: str, content: str) -> Dict:
    """记录一条经验到分类文件并重建索引；同分类同文本去重。"""
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


# ── 压缩 ────────────────────────────────────────────────


def read_all_entries(aid_dir: Path) -> List[Dict[str, str]]:
    """读全库条目（跨分类）。"""
    out: List[Dict[str, str]] = []
    for doc in list_documents(aid_dir):
        f = experience_dir(aid_dir) / doc["file"]
        for ln in _read(f).splitlines():
            if ln.strip().startswith("- "):
                out.append({"category": doc["category"], "content": ln.strip()[2:]})
    return out


def total_entries(aid_dir: Path) -> int:
    return sum(d["count"] for d in list_documents(aid_dir))


def needs_consolidate(aid_dir: Path) -> bool:
    """全库条目总数超过压缩水位。"""
    return total_entries(aid_dir) > _CONSOLIDATE_THRESHOLD


def write_all_entries(aid_dir: Path, items: List[Dict[str, str]]) -> Dict:
    """全库重写：旧文件备份到 experience.bak/ 后按新分类重建；条目级去重。"""
    d = experience_dir(aid_dir)
    d.mkdir(parents=True, exist_ok=True)
    bak = d.parent / "experience.bak"
    bak.mkdir(exist_ok=True)
    old_files = list(d.glob("*.md"))
    for f in old_files:
        (bak / f.name).write_text(_read(f), encoding="utf-8")
    by_cat: Dict[str, List[str]] = {}
    for it in items:
        cat = normalize_category(str(it.get("category") or ""))
        content = str(it.get("content") or "").strip()
        if not cat or not content:
            continue
        bucket = by_cat.setdefault(cat, [])
        if content not in bucket:
            bucket.append(content)
    # 安全阀：原本有条目但结果全被过滤 → 拒绝清库（防 LLM 异常输出毁库）
    if not by_cat and old_files:
        return {"count": 0, "rejected": True}
    for f in old_files:
        f.unlink()
    total = 0
    for cat, contents in by_cat.items():
        f = d / f"{_safe_name(cat)}.md"
        f.write_text(f"# {cat}\n\n" + "\n".join(f"- {c}" for c in contents) + "\n", encoding="utf-8")
        total += len(contents)
    rebuild_index(aid_dir)
    return {"count": total}


def consolidate(aid_dir: Path, *, llm: Callable) -> Dict:
    """LLM 全库压缩（跨分类语义合并去重 + 重新归类）；LLM 失败/输出异常时不动原文件。"""
    entries = read_all_entries(aid_dir)
    before = len(entries)
    if before < 2:
        return {"changed": False, "reason": "too_few", "before": before, "after": before}
    try:
        prompts = _prompts("build_consolidate")
        raw = (llm(prompts.build_consolidate(entries)) or "").strip()
    except Exception as e:
        logger.warning(f"[assistant_hub.experience] 压缩 LLM 失败: {e}")
        return {"changed": False, "reason": f"llm_error: {e}", "before": before, "after": before}
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
    except Exception:
        return {"changed": False, "reason": "bad_json", "before": before, "after": before}
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        return {"changed": False, "reason": "bad_json", "before": before, "after": before}
    # 安全阀：输出空数组或条目数不减 → 视为无价值/异常输出，不动原文件
    if not data:
        return {"changed": False, "reason": "empty_output", "before": before, "after": before}
    r = write_all_entries(aid_dir, data)
    if r.get("rejected"):
        return {"changed": False, "reason": "rejected", "before": before, "after": before}
    return {"changed": True, "before": before, "after": r["count"]}


# ── 反思 ────────────────────────────────────────────────


def reflect(aid_dir: Path, *, identity_and_persona: str, memory_md: str, llm: Callable) -> Dict:
    """从近期记忆提炼 0-3 条工作心得写入经验库；返回 {"added","items"}。"""
    existing = read_index(aid_dir)
    try:
        prompts = _prompts("build_reflect")
        raw = (llm(prompts.build_reflect(identity_and_persona, memory_md, existing)) or "").strip()
    except Exception as e:
        logger.warning(f"[assistant_hub.experience] 反思 LLM 失败: {e}")
        return {"added": 0, "items": [], "error": str(e)}
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
    except Exception:
        return {"added": 0, "items": [], "error": "bad_json"}
    if not isinstance(data, list):
        return {"added": 0, "items": [], "error": "bad_json"}
    added_items: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cat = normalize_category(str(item.get("category") or ""))
        content = str(item.get("content") or "").strip()
        if not cat or not content:
            continue
        r = record_entry(aid_dir, cat, content)
        if r.get("added"):
            added_items.append({"category": cat, "content": content})
    return {"added": len(added_items), "items": added_items}
