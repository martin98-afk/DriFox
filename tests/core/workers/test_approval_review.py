# -*- coding: utf-8 -*-
"""ApprovalReviewTask：prompt 构建 / 结果解析白名单"""

from app.core.workers.approval_review import build_review_prompt, parse_review


def test_parse_review_allow():
    assert parse_review('{"decision": "allow", "reason": "常规构建"}') == {
        "decision": "allow",
        "reason": "常规构建",
    }


def test_parse_review_code_block_and_case():
    raw = "```json\n{\"decision\": \"DENY\", \"reason\": \"删除系统目录\"}\n```"
    assert parse_review(raw) == {"decision": "deny", "reason": "删除系统目录"}


def test_parse_review_surrounding_text():
    assert parse_review('审核结论：{"decision": "allow", "reason": ""} 以上') == {
        "decision": "allow",
        "reason": "",
    }


def test_parse_review_rejects_bad_decision():
    """decision 白名单外一律 None（宿主回落人工，绝不猜）"""
    assert parse_review('{"decision": "maybe", "reason": "x"}') is None
    assert parse_review("") is None
    assert parse_review('{"reason": "缺 decision"}') is None
    assert parse_review("不是 JSON") is None  # 无拒绝语义词的长/中性文本不猜测


def test_parse_review_text_fallback_deny():
    """模型不守 JSON 格式输出短句：拒绝语义 → deny（fail-safe 侧回退）"""
    assert parse_review(" 不行") == {"decision": "deny", "reason": "不行"}
    assert parse_review("拒绝执行，该命令会删除系统目录")["decision"] == "deny"
    assert parse_review("dangerous command, deny")["decision"] == "deny"
    assert parse_review("不能放行：超出工作目录")["decision"] == "deny"


def test_parse_review_text_fallback_never_allow():
    """allow 文本回退带否定词防护：否定前缀存在时绝不判 allow"""
    assert parse_review("不能放行")["decision"] == "deny"  # 含否定 → deny 表命中
    assert parse_review("不可同意") is None  # 否定 + 无 deny 表词 → 回落人工
    assert parse_review("不允许执行")["decision"] == "deny"  # deny 表词优先


def test_parse_review_text_fallback_allow():
    """短句明确 allow 语义 → allow（否定词防护通过后）"""
    assert parse_review(" 可以") == {"decision": "allow", "reason": "可以"}
    assert parse_review("可以执行，常规构建操作")["decision"] == "allow"
    assert parse_review("allow")["decision"] == "allow"
    assert parse_review("没问题，执行吧")["decision"] == "allow"


def test_parse_review_text_fallback_long_output_ignored():
    """长文本不做语义回退（避免从大段解释里误捡关键词）"""
    assert parse_review("分析" * 50 + "结论是拒绝") is None


def test_parse_review_reason_truncated():
    raw = '{"decision": "deny", "reason": "' + "x" * 500 + '"}'
    assert parse_review(raw)["reason"] == "x" * 200


def test_build_review_prompt_contains_context():
    p = build_review_prompt(
        "Bash",
        {"command": "rm -rf /"},
        "D:/work/proj",
        {"writes": True, "paths": ["C:/Windows"]},
    )
    assert "Bash" in p and "rm -rf /" in p and "D:/work/proj" in p
    assert "deny" in p  # 提示词含输出格式要求
