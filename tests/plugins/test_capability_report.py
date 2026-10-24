# -*- coding: utf-8 -*-
"""capability_report 工具：差集判定逻辑单测。

只测纯函数 parse_audits / load_manifests / build_report，不碰真实日志。
"""
import json

from tools.capability_report import build_report, load_manifests, parse_audits


def _write_manifest(root, plugin, caps, api_version=2):
    d = root / plugin / ".drifox-plugin"
    d.mkdir(parents=True)
    data = {"name": plugin, "api_version": api_version}
    if caps is not None:
        data["capabilities"] = caps
    (d / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    return d


def test_parse_audits_filters_since_and_builtin(tmp_path):
    log = tmp_path / "all.log"
    log.write_text(
        "\n".join(
            [
                "2026-10-20 09:00:00 | INFO | [CapabilityAudit] plugin=a cap=model.config level=high",
                "2026-10-24 09:00:00 | INFO | [CapabilityAudit] plugin=a cap=ui.notify level=low",
                "2026-10-24 09:00:01 | INFO | [CapabilityAudit] plugin=(builtin) cap=state.kv level=none",
                "无关行",
            ]
        ),
        encoding="utf-8",
    )
    used = parse_audits([str(tmp_path)])
    assert used["a"] == {"model.config": 1, "ui.notify": 1}
    assert "(builtin)" not in used
    # since 收窄：升级前历史被排除
    used2 = parse_audits([str(tmp_path)], since="2026-10-23")
    assert used2["a"] == {"ui.notify": 1}


def test_build_report_diffs(tmp_path):
    _write_manifest(tmp_path, "legacy-plug", None, api_version=1)
    _write_manifest(tmp_path, "v2-plug", ["model.config", "session.read"])
    used = {
        "legacy-plug": {"model.config": 2, "state.kv": 1},
        "v2-plug": {"model.config": 1},
        "ghost-plug": {"mcp.call": 1},
    }
    manifests = load_manifests([str(tmp_path)])
    report = build_report(used, manifests)

    # 兼容期：建议清单剔除 UNGATED（state.kv）
    assert report["legacy_suggest"]["legacy-plug"] == ["model.config"]
    # v2：声明未用 / 无越权
    assert report["unused"]["v2-plug"] == ["session.read"]
    assert "v2-plug" not in report["unauthorized"]
    # 本地未安装
    assert report["orphan_usage"]["ghost-plug"] == ["mcp.call"]
    # 无使用记录的插件进 no_usage
    assert "legacy-plug" not in report["no_usage"]


def test_build_report_unauthorized_detected(tmp_path):
    """v2 插件用到声明外能力 → 进 unauthorized（出现即说明有绕过路径）。"""
    _write_manifest(tmp_path, "v2-plug", ["model.config"])
    used = {"v2-plug": {"mcp.call": 1}}
    report = build_report(used, load_manifests([str(tmp_path)]))
    assert report["unauthorized"]["v2-plug"] == ["mcp.call"]
