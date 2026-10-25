# -*- coding: utf-8 -*-
"""test_memory_sections.py — 全局记忆分区（schema + 校验 + 蒸馏强制归属）回归。

覆盖：分区定义读写、块解析、标题校验（孤儿/块内小节/总述条目/纯散文）、
分区内插入、改名同步、prompt 注入。
"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_STORE = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "store.py"
_PROMPTS = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "prompts.py"

_spec = importlib.util.spec_from_file_location("test_memory_sections_store", str(_STORE))
store = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_memory_sections_store", store)
_spec.loader.exec_module(store)

_spec2 = importlib.util.spec_from_file_location("test_memory_sections_prompts", str(_PROMPTS))
prompts = importlib.util.module_from_spec(_spec2)
sys.modules.setdefault("test_memory_sections_prompts", prompts)
_spec2.loader.exec_module(prompts)


SECTIONS = [
    {"name": "工程方法论", "hint": "跨项目通用的做法、验证方法、纪律"},
    {"name": "用户偏好", "hint": "表达方式、交互习惯、明确禁止的事项"},
]


def _aid_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hub" / "build"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 分区定义读写 ─────────────────────────────────────────


def test_sections_roundtrip(tmp_path):
    aid_dir = _aid_dir(tmp_path)
    assert store.load_sections(aid_dir) == []
    r = store.save_sections(aid_dir, SECTIONS)
    assert r["ok"] and r["count"] == 2
    loaded = store.load_sections(aid_dir)
    assert [s["name"] for s in loaded] == ["工程方法论", "用户偏好"]
    assert loaded[0]["hint"].startswith("跨项目通用")


def test_sections_reject_duplicate_and_bad_name(tmp_path):
    aid_dir = _aid_dir(tmp_path)
    dup = store.save_sections(aid_dir, [{"name": "A"}, {"name": "A"}])
    assert not dup["ok"] and "重复" in dup["error"]
    bad = store.save_sections(aid_dir, [{"name": "#非法"}])
    assert not bad["ok"]


def test_sections_corrupt_file_degrades_to_freeform(tmp_path):
    aid_dir = _aid_dir(tmp_path)
    store.sections_path(aid_dir).write_text("{not json", encoding="utf-8")
    assert store.load_sections(aid_dir) == []
    # 空 schema 恒通过校验（自由格式）
    assert store.validate_global_sections("随便写点什么\n## 任意标题", [])["ok"]


# ── 块解析 ───────────────────────────────────────────────


def test_parse_section_blocks_splits_preamble_and_blocks():
    text = "这段是总述\n\n## 工程方法论\n- 条目一\n- 条目二\n\n## 用户偏好\n- 条目三\n"
    parsed = store.parse_section_blocks(text)
    assert parsed["preamble"] == "这段是总述"
    assert [b["name"] for b in parsed["blocks"]] == ["工程方法论", "用户偏好"]
    assert parsed["blocks"][0]["lines"] == ["- 条目一", "- 条目二"]


# ── 校验 ─────────────────────────────────────────────────


def test_validate_ok_for_wellformed():
    text = "## 工程方法论\n- 缓存粒度放在调用次数多那层\n\n## 用户偏好\n- 回复要短\n"
    v = store.validate_global_sections(text, SECTIONS)
    assert v["ok"], v


def test_validate_rejects_unknown_title_and_reports_preview():
    text = "## 工程方法论\n- 正常条目\n\n## 缓存策略\n- 命中率为 0 即粒度错\n- 第二条\n"
    v = store.validate_global_sections(text, SECTIONS)
    assert not v["ok"] and v["error"] == "section_invalid"
    unknown = v["unknown_titles"]
    assert len(unknown) == 1
    assert unknown[0]["title"] == "缓存策略" and unknown[0]["count"] == 2
    assert unknown[0]["preview"][0].startswith("- 命中率为 0")


def test_validate_rejects_header_inside_block():
    text = "## 工程方法论\n- 正常\n### 并发\n- 子条目\n"
    v = store.validate_global_sections(text, SECTIONS)
    assert not v["ok"]
    assert v["header_in_block"][0]["line"] == "### 并发"
    assert v["header_in_block"][0]["section"] == "工程方法论"


def test_validate_rejects_preamble_bullets():
    text = "总述\n- 偷跑条目\n\n## 工程方法论\n- 正常\n"
    v = store.validate_global_sections(text, SECTIONS)
    assert not v["ok"] and v["preamble_bullets"] == 1


def test_validate_rejects_pure_prose():
    v = store.validate_global_sections("这里全是散文，没有任何条目，也没有分区标题。", SECTIONS)
    assert not v["ok"] and v.get("empty_entries")


def test_validate_allows_empty_blocks_as_escape_hatch():
    """全删条目只留 ## 头：收敛兜底出口，必须能过。"""
    v = store.validate_global_sections("## 工程方法论\n\n## 用户偏好\n", SECTIONS)
    assert v["ok"], v


def test_validate_allows_preamble_text_without_bullets():
    v = store.validate_global_sections("一句总述。\n\n## 工程方法论\n- 条目\n", SECTIONS)
    assert v["ok"], v


def test_validate_diag_carries_skeleton_and_counts():
    v = store.validate_global_sections("## 缓存策略\n- 孤儿条目\n", SECTIONS)
    assert "## 工程方法论" in v["skeleton"]
    assert v["counts"] == {"工程方法论": 0, "用户偏好": 0}
    assert v["hint"]


# ── 分区内插入 ───────────────────────────────────────────


def test_insert_into_existing_section_at_block_end():
    text = "## 工程方法论\n- 条目一\n\n## 用户偏好\n- 偏好一\n"
    out = store.insert_into_section(text, "工程方法论", "- 条目二")
    assert out.index("- 条目一") < out.index("- 条目二") < out.index("## 用户偏好")


def test_insert_creates_section_when_missing():
    out = store.insert_into_section("## 工程方法论\n- 条目一\n", "用户偏好", "- 偏好一")
    assert "## 用户偏好" in out and "- 偏好一" in out
    assert out.index("## 工程方法论") < out.index("## 用户偏好")


def test_insert_on_empty_file_keeps_body():
    out = store.insert_into_section("", "用户偏好", "- 偏好一")
    assert "## 用户偏好" in out and "- 偏好一" in out


def test_append_memory_file_lands_in_section(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text("## 工程方法论\n- 旧条目\n\n## 用户偏好\n- 旧偏好\n", encoding="utf-8")
    r = store.append_memory_file(p, "- 新条目", "工程方法论")
    assert r["ok"]
    text = p.read_text(encoding="utf-8")
    assert text.index("- 旧条目") < text.index("- 新条目") < text.index("## 用户偏好")


# ── 改名同步 ─────────────────────────────────────────────


def test_rename_section_title_only_exact_header():
    text = "## 工程方法论\n- 提到 ## 工程方法论 的条目\n"
    out = store.rename_section_title(text, "工程方法论", "工程做法")
    assert out.startswith("## 工程做法\n")
    assert "提到 ## 工程方法论 的条目" in out


# ── prompt 注入 ──────────────────────────────────────────


def test_memory_system_has_no_section_block_without_schema():
    text = prompts.build_memory_system("/g/MEMORY.md", "/p", {"global": 4000, "project": 3000})
    assert "全局记忆分区" not in text


def test_memory_system_injects_section_list():
    text = prompts.build_memory_system("/g/MEMORY.md", "/p", {"global": 4000}, SECTIONS)
    assert "全局记忆分区（强制）" in text
    assert "- 工程方法论：跨项目通用的做法、验证方法、纪律" in text
    assert "禁出现清单外的 ## 标题" in text


def test_distill_gate_without_sections_keeps_freeform_step():
    text = prompts.build_distill_gate(["2026-10-01"], "/p/MEMORY.md", "/g/MEMORY.md")
    assert "分区" not in text
    assert "宁缺勿滥" in text


def test_distill_gate_with_sections_forces_reassign_rules():
    text = prompts.build_distill_gate(["2026-10-01"], "/p/MEMORY.md", "/g/MEMORY.md", SECTIONS)
    assert "禁自建分区" in text
    assert "不确定就留在原地" in text
    assert "取清单中靠前的那个" in text
    assert "任何分区都不匹配 → 删除该条" in text
    assert "dry_run=true" in text
    assert "skeleton" in text
    # 第 4 步被替换成多段说明，5/6 仍要存在
    assert "5. 全部蒸馏完成后" in text and "6. 全部完成后" in text


# ── 默认分区（惰性落盘）─────────────────────────────────


def test_default_sections_shape():
    """默认三区按「内容成因」划分；hint 短标签（UI 单行），detail 含排除说明（prompt 用）。"""
    ds = store.default_sections()
    assert [s["name"] for s in ds] == ["工程方法论", "工具与环境", "用户偏好"]
    assert all(s["hint"] for s in ds) and all(s["detail"] for s in ds)
    # hint 必须短到能单行显示（UI 不折行）
    assert all(len(s["hint"]) <= 12 for s in ds), [s["hint"] for s in ds]
    # detail 必须带排除说明，否则模型会往最宽松的分区塞
    assert "不写具体工具行为" in ds[0]["detail"]
    assert "不写本项目业务规则" in ds[1]["detail"]
    assert "与具体项目无关" in ds[2]["detail"]
    # 注入 prompt 用 detail（完整规则），不是短标签
    prompt_text = store.sections_prompt_text(ds)
    assert "不写具体工具行为" in prompt_text


def test_default_sections_returns_copy():
    a = store.default_sections()
    a[0]["name"] = "改坏了"
    assert store.default_sections()[0]["name"] == "工程方法论"


def test_ensure_default_sections_writes_once(tmp_path):
    aid_dir = _aid_dir(tmp_path)
    first = store.ensure_default_sections(aid_dir)
    assert [s["name"] for s in first] == ["工程方法论", "工具与环境", "用户偏好"]
    assert store.sections_path(aid_dir).exists()
    # 第二次读的是文件，不再重写
    mtime = store.sections_path(aid_dir).stat().st_mtime_ns
    second = store.ensure_default_sections(aid_dir)
    assert second == first
    assert store.sections_path(aid_dir).stat().st_mtime_ns == mtime


def test_ensure_default_sections_respects_user_cleared(tmp_path):
    """用户把分区全删光 = 要自由格式，不能被重新塞回默认值。"""
    aid_dir = _aid_dir(tmp_path)
    store.save_sections(aid_dir, [])
    assert store.ensure_default_sections(aid_dir) == []
    assert store.load_sections(aid_dir) == []


def test_ensure_default_sections_keeps_custom(tmp_path):
    aid_dir = _aid_dir(tmp_path)
    store.save_sections(aid_dir, [{"name": "我的分区", "hint": "自定义"}])
    assert [s["name"] for s in store.ensure_default_sections(aid_dir)] == ["我的分区"]


# ── 全局记忆准入标准（说明书）───────────────────────────


def test_memory_system_documents_admission_rules():
    text = prompts.build_memory_system("/g/MEMORY.md", "/p", {"global": 4000})
    # 四类该进
    assert "用户的硬性要求" in text
    assert "工具与平台的客观坑" in text
    assert "验证方法论" in text
    assert "协作纪律" in text
    # 三不写
    assert "项目架构知识" in text
    assert "具体业务规则" in text
    assert "过程与临时状态" in text
    # 边界判据
    assert "工具坑" in text and "业务规则" in text
    assert "离开当前项目后仍然成立" in text
