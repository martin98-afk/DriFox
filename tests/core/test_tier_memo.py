# -*- coding: utf-8 -*-
"""[E2] tier memo 回归：shape/normalize 产物 memo 的命中、miss 与键安全。

效果口径：同一 content 字符串对象二次投影不再重复 parse/dump（shape）
或四条正则全文扫描（normalize）；miss（新对象）与 id 复用（过期条目）
路径行为与无 memo 时一致。
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from importlib import import_module  # noqa: E402

_shape_mod = import_module("plugins.system-context.context_tiers.tool_shape")
_norm_mod = import_module("plugins.system-context.context_tiers.tool_normalize")

shape_json_text = _shape_mod.shape_json_text
normalize_text = _norm_mod.normalize_text

# 大小超过 tier 生效门槛（json_shape_max_chars 默认 8000），贴近真实调用口径
def _big_json(n_keys: int = 200) -> str:
    return json.dumps({f"key{i}": "x" * 200 for i in range(n_keys)}, ensure_ascii=False)

@pytest.fixture(autouse=True)
def _clear_memos():
    _shape_mod._shape_memo.clear()
    _norm_mod._norm_memo.clear()
    yield
    _shape_mod._shape_memo.clear()
    _norm_mod._norm_memo.clear()

def test_shape_memo_hit_skips_reparse(monkeypatch):
    """同一 str 对象二次调用：json.loads/dumps 调用数为 0，产物一致。"""
    text = _big_json()
    first = shape_json_text(text)
    assert first != text and len(first) < len(text), "大 dict JSON 应被骨架化"

    calls = {"loads": 0, "dumps": 0}
    real_loads, real_dumps = json.loads, json.dumps

    def counting_loads(*a, **k):
        calls["loads"] += 1
        return real_loads(*a, **k)

    def counting_dumps(*a, **k):
        calls["dumps"] += 1
        return real_dumps(*a, **k)

    monkeypatch.setattr(_shape_mod.json, "loads", counting_loads)
    monkeypatch.setattr(_shape_mod.json, "dumps", counting_dumps)
    second = shape_json_text(text)
    assert second == first
    assert calls == {"loads": 0, "dumps": 0}, f"命中应零 parse/dump，实测 {calls}"

def test_shape_memo_miss_recomputes():
    """新 str 对象（内容不同）：正常骨架化，产物正确。"""
    out = shape_json_text(_big_json(n_keys=100))
    assert '"_omitted_keys"' in out

def test_shape_memo_id_reuse_stale_entry():
    """id 复用防线：过期条目（orig is not text）不返回 stale 产物。"""
    text = _big_json()
    fake_orig = "unrelated text with different content body"
    _shape_mod._shape_memo[(id(text), len(text))] = (fake_orig, "STALE")
    out = shape_json_text(text)
    assert out != "STALE"
    assert out == shape_json_text(_big_json()), "重算产物应与干净计算一致"

def test_shape_memo_lru_bound():
    """写入超上限逐出最旧条目，容量收敛在 512。"""
    for i in range(_shape_mod._SHAPE_MEMO_MAX + 50):
        shape_json_text(json.dumps({"i": i, "pad": "y" * 300}))
    assert len(_shape_mod._shape_memo) <= _shape_mod._SHAPE_MEMO_MAX

def test_normalize_memo_hit_skips_rescan(monkeypatch):
    """同一 str 对象二次调用：四条正则与门控零执行。"""
    dirty = ("log line with \x1b[31mcolor\x1b[0m and trailing   \n" * 40) + "\n\n\n\nend"
    first = normalize_text(dirty)
    assert first != dirty

    class _CountingRe:
        def __init__(self, inner):
            self._inner = inner
            self.calls = 0

        def _tick(self, r, *a, **k):
            self.calls += 1
            return getattr(self._inner, r)(*a, **k)

        def sub(self, *a, **k):
            return self._tick("sub", *a, **k)

        def search(self, *a, **k):
            return self._tick("search", *a, **k)

    proxies = {}
    for name in ("_ANSI_RE", "_CTRL_RE", "_TRAILING_WS_RE", "_BLANK_RUN_RE"):
        p = _CountingRe(getattr(_norm_mod, name))
        proxies[name] = p
        monkeypatch.setattr(_norm_mod, name, p)

    second = normalize_text(dirty)
    assert second == first
    total = sum(p.calls for p in proxies.values())
    assert total == 0, f"命中应零正则执行，实测 {total}"

def test_normalize_memo_miss_and_idempotent():
    """新对象正常计算；产物回喂输出不变（幂等，memo 依赖的前提）。"""
    dirty = ("error \x1b[2Kprogress   \n" * 50) + "\n\n\n\ntail"
    out1 = normalize_text(dirty)
    out2 = normalize_text(dirty + "#different")
    assert out1 != dirty
    assert out2 != out1
    assert normalize_text(out1) == out1

def test_normalize_memo_id_reuse_stale_entry():
    """id 复用防线：过期条目不返回 stale 产物。"""
    dirty = "\x1b[31mred\x1b[0m and trailing   \n" * 30
    fake_orig = "completely other body"
    _norm_mod._norm_memo[(id(dirty), len(dirty))] = (fake_orig, "STALE")
    out = normalize_text(dirty)
    assert out != "STALE"
    assert out == normalize_text(dirty + "#v2").replace("#v2", "") or out.startswith("red and trailing"), (
        "重算产物应与干净计算一致"
    )

def test_normalize_memo_lru_bound():
    """写入超上限逐出最旧条目，容量收敛在 512。"""
    for i in range(_norm_mod._NORM_MEMO_MAX + 50):
        normalize_text(f"text {i} with trailing   \n and \x1b[0m codes")
    assert len(_norm_mod._norm_memo) <= _norm_mod._NORM_MEMO_MAX

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
