"""migrate.py — v1 MEMORY.md 文本 → v2 事件与 legacy 段。

自包含：不 import store / manager，分区定义由调用方传入。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

LEGACY_SECTION = "_legacy"
_H2 = re.compile(r"^##\s+(.+?)\s*$")


def _match_section(name: str, sections: List[Dict[str, str]]) -> str:
    target = str(name or "").strip()
    for s in sections or []:
        if str(s.get("name") or "") == target:
            return target
    for s in sections or []:
        detail = str(s.get("detail") or "")
        if target and target in detail:
            return str(s.get("name") or "")
    return ""


def parse_legacy(text: str, sections: List[Dict[str, str]]) -> Dict[str, Any]:
    """解析旧全局记忆。

    能识别的 `- ` 条目转事件；孤儿分区、散文、小节标题整段进 legacy。
    """
    events: List[Dict[str, Any]] = []
    legacy: List[Dict[str, Any]] = []
    current = ""
    prose: List[str] = []

    def flush(section: str, line_no: int) -> None:
        if prose:
            legacy.append({"text": "\n".join(prose).strip(), "reason": "prose", "line_no": line_no})
            prose.clear()

    for idx, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.rstrip()
        m = _H2.match(line)
        if m:
            flush(current, idx)
            title = m.group(1).strip()
            current = _match_section(title, sections)
            if not current:
                prose.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("-") and current:
            body = stripped.lstrip("-").strip()
            if body:
                events.append(
                    {
                        "op": "put",
                        "section": current,
                        "text": body,
                        "origin": "import",
                        "source": {"kind": "migration"},
                    }
                )
        else:
            prose.append(line)

    flush(current, 0)
    return {
        "events": events,
        "legacy": legacy,
        "stats": {
            "entries": len(events),
            "legacy": len(legacy),
            "sections": len({e["section"] for e in events}),
        },
    }