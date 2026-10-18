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
    assert parse_review("不是 JSON") is None
    assert parse_review("") is None
    assert parse_review('{"reason": "缺 decision"}') is None


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
