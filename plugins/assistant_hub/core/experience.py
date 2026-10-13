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
import time
from pathlib import Path
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_MAX_CATEGORY = 12
# 全库经验条目总数超过该值时才考虑自动压缩。
# 定 120 而非 50：条目本身高度原子（踩坑/决策各一条、语义不重叠）时，
# 压不动是正常结果；水位过低会让「已无冗余可压」的库反复触发压缩。
_CONSOLIDATE_THRESHOLD = 120
# 距上次成功压缩后新增多少条才再压：防止压完不减（或减少极少）时死循环触发。
_CONSOLIDATE_GROWTH = 30
# 分批压缩单批条目上限：条目多时单批 prompt+全量 JSON 输出耗时撞 LLM 超时线
# （实测 113 条 26k 字符 prompt 单次 >240s 无产出，见 2026-10-13 排查）
_CONSOLIDATE_BATCH = 60
# 压缩失败后自动路径冷却秒数：防失败任务每次反思重烧双倍超时（手动压缩不受限）
_CONSOLIDATE_COOLDOWN = 30 * 60


def _prompts(*required: str):
    """取 prompts 模块；mtime 自检防热重载旧缓存，required 兜底校验函数存在。"""
    key = "assistant_hub_core.memory.prompts"
    path = _THIS.parent / "memory" / "prompts.py"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    mod = sys.modules.get(key)
    if mod is not None and getattr(mod, "_source_mtime", -1.0) >= mtime and all(hasattr(mod, r) for r in required):
        return mod
    spec = importlib.util.spec_from_file_location(key, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    module._source_mtime = mtime
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
    """自动压缩判据：总量超水位 **且** 距上次成功压缩后新增条目达增长量。

    只判总量会死循环：条目高度原子（语义不重叠）时压缩只能减 1-2 条，
    下次反思照样超水位再压一次（2026-10-13 实测 112 条压完仍 112）。
    增量判据让「已无冗余可压」的库安静下来。
    """
    total = total_entries(aid_dir)
    if total <= _CONSOLIDATE_THRESHOLD:
        return False
    data = _read_state(aid_dir)
    last = float(data.get("last_total", 0))
    return total >= (last + _CONSOLIDATE_GROWTH)


def _consolidate_state_path(aid_dir: Path) -> Path:
    return experience_dir(aid_dir) / ".consolidate_state.json"


def _read_state(aid_dir: Path) -> Dict[str, Any]:
    try:
        data = json.loads(_consolidate_state_path(aid_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(aid_dir: Path, data: Dict[str, Any]) -> None:
    try:
        experience_dir(aid_dir).mkdir(parents=True, exist_ok=True)
        _consolidate_state_path(aid_dir).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _mark_consolidate_failed(aid_dir: Path) -> None:
    data = _read_state(aid_dir)
    data["failed_at"] = time.time()
    _write_state(aid_dir, data)


def _clear_consolidate_state(aid_dir: Path) -> None:
    """压缩成功：清失败冷却，记本次压缩后的总量作下次增量判据基线。

    记 after 而非 before：以压缩后实际存量为基线，
    「新增 N 条再压」的语义才对得上。
    """
    data = _read_state(aid_dir)
    data.pop("failed_at", None)
    data["last_total"] = float(total_entries(aid_dir))
    _write_state(aid_dir, data)


def consolidate_in_cooldown(aid_dir: Path) -> bool:
    """上次压缩失败后是否仍在冷却期（自动路径跳过；手动不受限）。"""
    at = float(_read_state(aid_dir).get("failed_at", 0))
    return bool(at) and (time.time() - at) < _CONSOLIDATE_COOLDOWN


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


def consolidate(aid_dir: Path, *, llm: Callable, timeout: float = 240.0, retries: int = 1) -> Dict:
    """LLM 全库压缩（分批跨分类语义合并去重 + 重新归类）；失败时不动原文件并记冷却。

    分批原因：条目多时单批 prompt 与全量 JSON 输出的总耗时撞 LLM 超时线，
    分批让单批远离红线。任一批失败即整体失败：不动原文件、记录冷却
    （auto 路径按 consolidate_in_cooldown 跳过，防每轮反思重烧超时）。
    """
    entries = read_all_entries(aid_dir)
    before = len(entries)
    if before < 2:
        return {"changed": False, "reason": "too_few", "before": before, "after": before}
    prompts = _prompts("build_consolidate")
    batches = [entries[i : i + _CONSOLIDATE_BATCH] for i in range(0, len(entries), _CONSOLIDATE_BATCH)]
    merged: List[Dict[str, str]] = []
    for bi, batch in enumerate(batches):
        try:
            raw = (llm(prompts.build_consolidate(batch), timeout=timeout, retries=retries) or "").strip()
        except Exception as e:
            _mark_consolidate_failed(aid_dir)
            logger.warning(f"[assistant_hub.experience] 压缩 LLM 失败（批 {bi + 1}/{len(batches)}）: {e}")
            return {"changed": False, "reason": f"llm_error: {e}", "before": before, "after": before}
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            data = json.loads(raw)
        except Exception:
            _mark_consolidate_failed(aid_dir)
            return {"changed": False, "reason": "bad_json", "before": before, "after": before}
        if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
            _mark_consolidate_failed(aid_dir)
            return {"changed": False, "reason": "bad_json", "before": before, "after": before}
        merged.extend(x for x in data if isinstance(x, dict))
    # 安全阀：全部批次输出为空 → 视为异常输出，不动原文件
    if not merged:
        return {"changed": False, "reason": "empty_output", "before": before, "after": before}
    r = write_all_entries(aid_dir, merged)
    if r.get("rejected"):
        return {"changed": False, "reason": "rejected", "before": before, "after": before}
    _clear_consolidate_state(aid_dir)
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
