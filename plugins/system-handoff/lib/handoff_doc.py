# -*- coding: utf-8 -*-
"""会话交接文档读写与新鲜度判定 —— 插件侧独立实现。

文件契约：`<project_root>/.drifox/handoff/<session_id>.md`（无工作目录时回退
`<app_data_dir>/handoff/`），frontmatter 只含
session_id / project_root / created_at / updated_at / injected_into。

插件不能 import 主程序内部模块，因此解析/序列化逻辑独立存在；格式契约由本
注释约束，改动 frontmatter 字段必须同步工具端与注入端。

新鲜度不使用布尔 consumed：新会话若只有 system 消息、用户没说话就被丢弃（不落库），
布尔标记会把交接永久标成已消费。改为 `injected_into` 记录已注入的 session_id 列表。
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FRONT_KEYS = ("session_id", "project_root", "created_at", "updated_at", "injected_into")
MAX_INJECTED = 5

# 新鲜度阈值：交接写入后新增的消息条数与字符量（字符数 / 4 ≈ token）
FRESH_MAX_MESSAGES = 10
FRESH_MAX_TOKENS = 3000
_CHARS_PER_TOKEN = 4
# 文档年龄上限：超过则一律补写。防止几小时前的旧交接被当成「仍覆盖当前进度」直接切会话
FRESH_MAX_AGE_MS = 15 * 60 * 1000


def handoff_dir(project_root: str = "", app_data_dir: Optional[Path] = None) -> Path:
    """交接目录：有工作目录放项目内，否则回退应用数据目录"""
    root = (project_root or "").strip()
    if root:
        return Path(root) / ".drifox" / "handoff"
    base = Path(app_data_dir) if app_data_dir else Path.home() / ".drifox"
    return base / "handoff"


def handoff_path(project_root: str, session_id: str, app_data_dir: Optional[Path] = None) -> Path:
    return handoff_dir(project_root, app_data_dir) / f"{session_id}.md"


def parse_front(text: str) -> Tuple[Dict[str, Any], str]:
    """解析 frontmatter（值统一按 JSON 解析，失败保留原字符串）"""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: Dict[str, Any] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        try:
            meta[key] = json.loads(raw)
        except Exception:
            meta[key] = raw
    return meta, text[end + 5 :]


def dump_front(meta: Dict[str, Any]) -> str:
    """序列化 frontmatter（值统一 JSON 编码，避免路径含冒号时歧义）"""
    lines = ["---"]
    for key in FRONT_KEYS:
        if key not in meta:
            continue
        lines.append(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def load(path: Path) -> Optional[Dict[str, Any]]:
    """读取交接文档；不存在或无法解析返回 None

    Returns:
        {"meta": dict, "body": str, "path": Path}
    """
    try:
        p = Path(path)
        if not p.is_file():
            return None
        text = p.read_text(encoding="utf-8")
    except Exception:
        return None
    meta, body = parse_front(text)
    if not meta or not body.strip():
        return None
    return {"meta": meta, "body": body.strip(), "path": p}


def mark_injected(path: Path, session_id: str) -> None:
    """把 session_id 追加进 injected_into（去重、保留最近 MAX_INJECTED 个）"""
    doc = load(path)
    if doc is None or not session_id:
        return
    meta = doc["meta"]
    injected = meta.get("injected_into")
    if not isinstance(injected, list):
        injected = []
    ids: List[str] = [str(x) for x in injected if x]
    if session_id in ids:
        ids.remove(session_id)
    ids.append(session_id)
    meta["injected_into"] = ids[-MAX_INJECTED:]
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(dump_front(meta))
            f.write("\n")
            f.write(doc["body"])
            f.write("\n")
    except Exception:
        pass


def updated_at_ms(meta: Dict[str, Any]) -> int:
    """frontmatter.updated_at（秒级本地时间）转毫秒时间戳，失败返回 0"""
    raw = str((meta or {}).get("updated_at") or "")
    try:
        return int(time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))) * 1000
    except Exception:
        return 0


def is_fresh(meta: Dict[str, Any], messages: Optional[List[Dict[str, Any]]]) -> bool:
    """交接是否仍然覆盖当前进度

    判据：updated_at 之后的新增消息条数 ≤ FRESH_MAX_MESSAGES，
    且这些消息的字符量 ≤ FRESH_MAX_TOKENS * 4；
    且文档本身不超过 FRESH_MAX_AGE_MS（否则旧交接会覆盖当前进度）。
    时间戳缺失的消息按「旧消息」处理，不计入新增。
    """
    cutoff = updated_at_ms(meta)
    if cutoff <= 0:
        return False
    if time.time() * 1000 - cutoff > FRESH_MAX_AGE_MS:
        return False
    newer = []
    for msg in messages or []:
        try:
            ts = int(msg.get("ts_ms") or 0)
        except Exception:
            ts = 0
        if ts > cutoff:
            newer.append(msg)
    if len(newer) > FRESH_MAX_MESSAGES:
        return False
    chars = 0
    for msg in newer:
        content = msg.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    chars += len(block["text"])
    return chars <= FRESH_MAX_TOKENS * _CHARS_PER_TOKEN
