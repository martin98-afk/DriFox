# -*- coding: utf-8 -*-
"""CodeBuddy 每日任务链：协议层请求形状 + 编排层状态机 + 幂等落盘。

覆盖：
1. 协议层 —— 报活跃打到 billing 域且 body 为数组、领奖打到 Web 域且带
   x-client-platform、桌面事件注入指纹、JWT 取 uid
2. 编排层 —— 任务状态判定（需要动作 / 可领奖）、领养前置、accept 后领奖、
   缓存条目附加字段不被刷新覆盖
3. 真机校准过的关键结论：领奖路径 task_code 在 URL 里（不是 body）
"""

import base64
import importlib.util
import json
from pathlib import Path

import pytest

_PROVIDER_DIR = Path("plugins/system-providers/providers")
_CODEBUDDY = _PROVIDER_DIR / "codebuddy.py"
_BUDDY_AUTH = _PROVIDER_DIR / "buddy_auth.py"

pytestmark = pytest.mark.skipif(
    not (_CODEBUDDY.exists() and _BUDDY_AUTH.exists()),
    reason="system-providers 插件未安装",
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ba():
    return _load(_BUDDY_AUTH, "codebuddy_buddy_auth_test")


@pytest.fixture(scope="module")
def cb():
    return _load(_CODEBUDDY, "codebuddy_plugin_test")


def _jwt(claims: dict) -> str:
    """构造无签名三段式 JWT（仅测解析）。"""
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{head}.{body}.sig"


# ── 协议层：请求形状 ────────────────────────────────────


def test_uid_from_access_token(ba):
    token = _jwt({"sub": "user-abc", "exp": 1})
    assert ba.uid_from_access_token(token) == "user-abc"
    assert ba.uid_from_access_token("") == ""
    assert ba.uid_from_access_token("not-a-jwt") == ""


def test_report_activity_posts_event_array_to_billing(ba, monkeypatch):
    """活跃上报：billing 域 /v2/report，body 是数组且带 userId（缺则静默丢弃）。"""
    captured = {}

    def fake(method, url, body=None, headers=None, timeout=30.0):
        captured.update(method=method, url=url, body=body, headers=headers)
        return {"code": 0}

    monkeypatch.setattr(ba, "_api_request", fake)
    result = ba.report_activity("at", "uid-1", count=3, gap=0)

    assert result["ok"] == 3
    assert captured["method"] == "POST"
    assert captured["url"] == ba.BILLING_ENDPOINT + "/v2/report"
    assert isinstance(captured["body"], list) and len(captured["body"]) == 1
    ev = captured["body"][0]
    assert ev["eventCode"] == "chat_request_send"
    assert ev["userId"] == "uid-1"
    assert captured["headers"]["X-User-Id"] == "uid-1"


def test_claim_reward_uses_web_domain_and_path_code(ba, monkeypatch):
    """领奖：Web 域 + task_code 在路径 + x-client-platform: web（CLI 域该路径不存在）。"""
    captured = {}

    def fake(method, url, body=None, headers=None, timeout=30.0):
        captured.update(method=method, url=url, body=body, headers=headers)
        return {"code": 0, "data": {"already_claimed": False, "credit": 100, "energy": 5}}

    monkeypatch.setattr(ba, "_api_request", fake)
    res = ba.claim_reward("at", "uid-1", "chat_5")

    assert res["ok"] is True
    assert res["credit"] == 100 and res["energy"] == 5
    assert captured["url"] == ba.WEB_ENDPOINT + "/activity/growth/tasks/chat_5/claim"
    assert captured["body"] is None
    assert captured["headers"]["x-client-platform"] == "web"


def test_claim_reward_idempotent_already_claimed(ba, monkeypatch):
    """重复领奖按成功处理（already_claimed 不算失败）。"""
    monkeypatch.setattr(
        ba, "_api_request", lambda *a, **k: {"code": 0, "data": {"already_claimed": True}}
    )
    res = ba.claim_reward("at", "uid-1", "chat_5")
    assert res["ok"] is True and res["already_claimed"] is True and res["credit"] == 0


def test_report_desktop_events_injects_fingerprint(ba, monkeypatch):
    """桌面事件：每个事件都带 workbuddy-desktop 指纹，业务字段优先。"""
    captured = {}

    def fake(method, url, body=None, headers=None, timeout=30.0):
        captured.update(url=url, body=body, headers=headers)
        return {"code": 0}

    monkeypatch.setattr(ba, "_api_request", fake)
    ba.report_desktop_events("at", "uid-1", "nick", [{"eventCode": "x", "ideType": "override"}])

    assert captured["url"] == ba.ENDPOINT + "/v2/report"
    ev = captured["body"][0]
    assert ev["extName"] == "workbuddy-desktop"
    assert ev["userId"] == "uid-1"
    assert ev["ideType"] == "override"  # 业务字段覆盖同名单
    assert captured["headers"]["X-User-Id"] == "uid-1"


def test_desktop_chat_sequence_shape(ba):
    """桌面对话链：6 个事件，顺序与判据一致，含成功回执。"""
    events = ba.desktop_chat_sequence("conv", "req", "msg")
    codes = [e["eventCode"] for e in events]
    assert codes == [
        "agent_task_created",
        "chat_message_send",
        "chat_request_send",
        "chat_message_response",
        "chat_message_status",
        "chat_request_response",
    ]
    resp = events[3]
    assert resp["isSuccessful"] is True
    assert resp["conversationId"] == "conv"
    assert resp["finishReason"] == "stop"


def test_desktop_fingerprint_stable_per_uid(ba):
    """同一 uid 的设备标识跨调用恒定，不同 uid 互异（防多号关联风控）。"""
    a1 = ba.desktop_fingerprint("uid-a", "n")
    a2 = ba.desktop_fingerprint("uid-a", "n")
    b1 = ba.desktop_fingerprint("uid-b", "n")
    assert a1["machineId"] == a2["machineId"]
    assert a1["machineId"] != b1["machineId"]
    assert len(a1["machineId"]) == 36


def test_template_sequence_joins_chat_chain(ba):
    """模板事件组：chat 链 6 事件 + with_template + template_used。"""
    events = ba.desktop_template_sequence("conv", "req", "1", "深度研究")
    codes = [e["eventCode"] for e in events]
    assert codes[-2:] == ["agent_task_created_with_template", "template_used"]
    assert codes[0] == "agent_task_created"
    assert len(events) == 8


def test_expert_actual_use_keeps_real_request_id(ba):
    """专家使用事件必须带真实 requestId（自造 UUID 不计数）。"""
    expert = {"expert_id": "ex_1", "display_name_zh": "专家", "version": "1.0.0", "categories": []}
    ev = ba.desktop_expert_actual_use(expert, "conv", "cmb-0123456789abcdef0123456789abcdef")
    assert ev["requestId"].startswith("cmb-")
    assert ev["conversationId"] == "conv"
    assert ev["mode"] == "craft"


# ── 编排层：状态机 ──────────────────────────────────────


def test_needs_action_and_claimable_matrix(cb):
    """任务状态机：未接受/进行中需要动作，达标可领，已领不再动。"""
    assert cb._needs_action({"accept_status": "accepted", "progress": {"current": 2, "target": 5}})
    assert not cb._needs_action({"accept_status": "accepted", "progress": {"current": 5, "target": 5}})
    assert not cb._needs_action({"accept_status": "claimed"})
    assert not cb._needs_action({"status": "completed"})
    assert not cb._needs_action(None)

    assert cb._claimable({"status": "completed"})
    assert cb._claimable({"accept_status": "accepted", "progress": {"current": 5, "target": 5}})
    assert not cb._claimable({"accept_status": "claimed"})
    assert not cb._claimable({"accept_status": "accepted", "progress": {"current": 1, "target": 5}})


def test_task_progress_tolerates_missing_fields(cb):
    """progress 缺失/形态异常时按 0/1 兜底，不抛异常。"""
    assert cb._task_progress({}) == (0, 1)
    assert cb._task_progress({"progress": None}) == (0, 1)
    assert cb._task_progress({"progress": {"current": 3, "target": 0}}) == (3, 1)


# ── 缓存条目：附加字段不被刷新覆盖 ──────────────────────


def test_cache_put_keeps_uid_and_daily(cb, monkeypatch, tmp_path):
    """刷新 token 时不得丢掉 uid / nickname / daily（每日任务幂等标记靠它）。"""
    monkeypatch.setattr(cb, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cb, "_CACHE", {})
    entry = cb._cache_put("rt-1", "at-1", uid="uid-1", nickname="昵称")
    entry["daily"] = {"date": "2026-10-28", "done": {"adopt": True}}
    cb._save_entry(entry)

    refreshed = cb._cache_put("rt-1", "at-2")
    assert refreshed["uid"] == "uid-1"
    assert refreshed["nickname"] == "昵称"
    assert refreshed["daily"]["done"]["adopt"] is True
    assert refreshed["access_token"] == "at-2"


# ── 编排层：每日流程顺序与前置 ──────────────────────────


class _FakeBA:
    """记录调用顺序的协议层替身（不发网络请求）。"""

    def __init__(self, tasks=None):
        self.calls = []
        self._tasks = tasks or {}

    def report_activity(self, at, uid, count=1, **kw):
        self.calls.append(("report", count))
        return {"ok": count}

    def buddy_info(self, at, uid):
        self.calls.append(("buddy_info",))
        return {"buddy": None}

    def buddy_agreement(self, at, uid):
        self.calls.append(("agreement",))
        return {"code": 0}

    def buddy_adopt(self, at, uid):
        self.calls.append(("adopt",))
        return {"ok": True, "credit": 300, "energy": 8, "gated": False}

    def growth_tasks(self, at, uid):
        return list(self._tasks.values())

    def accept_tasks(self, at, uid, codes):
        self.calls.append(("accept", sorted(codes)))
        for code in codes:
            self._tasks[code]["accept_status"] = "accepted"
        return {}

    def claim_reward(self, at, uid, code):
        self.calls.append(("claim", code))
        return {"ok": True, "credit": 100, "energy": 5, "already_claimed": False}

    def growth_streak(self, at, uid):
        return {"streak": {"days": 0}, "redemption_status": {}}

    def lottery_summary(self, at, uid):
        return {"chances": 0}

    def travel_status(self, at, uid):
        return {"state": "idle", "daily_limit_reached": True}

    def claim_gift(self, at, uid):
        return {"ok": False, "credit": 0}


def _patch_settle(cb, monkeypatch):
    monkeypatch.setattr(cb, "_CLAIM_SETTLE_SEC", 0)
    monkeypatch.setattr(cb, "_REPORT_GAP", 0)
    monkeypatch.setattr(cb, "_save_entry", lambda entry: None)


def test_daily_flow_reports_then_adopts_then_accepts(cb, monkeypatch):
    """顺序约束：先活跃上报 → 领养 → accept（未领养时 accept 会被前置拦住）。"""
    _patch_settle(cb, monkeypatch)
    tasks = {"chat_5": {"task_code": "chat_5", "accept_status": "not_accepted",
                        "progress": {"current": 0, "target": 5}}}
    fake = _FakeBA(tasks)
    monkeypatch.setattr(cb, "_run_desktop_chain", lambda *a, **k: [])
    monkeypatch.setattr(cb, "_run_chat_tasks", lambda *a, **k: [])

    cb._run_daily_tasks(fake, "at", "uid", "nick", {"refresh_token": "rt"})

    order = [c[0] for c in fake.calls]
    assert order.index("report") < order.index("adopt") < order.index("accept")


def test_daily_flow_claims_completed_tasks(cb, monkeypatch):
    """达标任务走领奖，已领任务不再重复领。"""
    _patch_settle(cb, monkeypatch)
    tasks = {
        "template_5": {"task_code": "template_5", "accept_status": "accepted", "status": "completed",
                       "progress": {"current": 5, "target": 5}},
        "chat_5": {"task_code": "chat_5", "accept_status": "claimed",
                   "progress": {"current": 5, "target": 5}},
    }
    fake = _FakeBA(tasks)
    monkeypatch.setattr(cb, "_run_desktop_chain", lambda *a, **k: [])
    monkeypatch.setattr(cb, "_run_chat_tasks", lambda *a, **k: [])

    cb._run_daily_tasks(fake, "at", "uid", "nick", {"refresh_token": "rt"})

    claims = [c[1] for c in fake.calls if c[0] == "claim"]
    assert claims == ["template_5"]


def test_daily_flow_survives_step_failure(cb, monkeypatch):
    """单步抛异常不中断后续（守护线程里任何一步挂掉都不能拖垮整轮）。"""
    _patch_settle(cb, monkeypatch)

    class _BoomBA(_FakeBA):
        def report_activity(self, *a, **k):
            raise RuntimeError("network down")

    tasks = {"template_5": {"task_code": "template_5", "status": "completed",
                            "accept_status": "accepted", "progress": {"current": 5, "target": 5}}}
    fake = _BoomBA(tasks)
    monkeypatch.setattr(cb, "_run_desktop_chain", lambda *a, **k: [])
    monkeypatch.setattr(cb, "_run_chat_tasks", lambda *a, **k: [])

    cb._run_daily_tasks(fake, "at", "uid", "nick", {"refresh_token": "rt"})

    assert [c[1] for c in fake.calls if c[0] == "claim"] == ["template_5"]


def test_daily_tasks_disabled_by_default(cb):
    """默认关闭：无 PluginConfigRegistry 注册（单测直接 exec 模块）时按停用处理。"""
    assert cb._auto_task_disabled() is True
    called = []
    cb._run_daily_tasks = lambda *a, **k: called.append(1)
    try:
        cb._run_daily_for_entry(object(), "at", {"refresh_token": "rt"})
    finally:
        del cb._run_daily_tasks
    assert called == []

def test_plugin_manifest_declares_auto_tasks_off():
    """manifest 契约：auto_daily_tasks 必须声明为 bool 且默认 false（防回退成默认开）。"""
    import json as _json

    manifest = _json.loads(
        (Path("plugins/system-providers/.drifox-plugin/plugin.json")).read_text(encoding="utf-8")
    )
    fields = {f["key"]: f for f in manifest["config_schema"]["fields"]}
    field = fields["auto_daily_tasks"]
    assert field["type"] == "bool"
    assert field["default"] is False
    assert field["env"] == "DRIFOX_CODEBUDDY_TASKS"

def test_daily_tasks_enabled_when_config_true(cb, monkeypatch):
    """配置开为 true 后放行（开关不再缓存，每轮重新读）。"""
    from app.plugins.managers.plugin_config_store import PluginConfigStore

    monkeypatch.setattr(PluginConfigStore, "get", lambda self, p, k: True)
    assert cb._auto_task_disabled() is False


# ── 回归：专家定位按 id 而非名字 ──────────────────────────


def test_expert_hint_extracts_id_and_task_name(cb):
    """jump_url 里的 expertId 与「」里的任务用名都能取到。"""
    task = {
        "jump_url": "https://www.workbuddy.cn/work/launch/?deeplink=workbuddy%3A%2F%2Fexperts"
        "%3FexpertId%3Dex_GDl31WahyIH0&buddy_type=workbuddy",
        "task_desc": "在专家页搜索并召唤「中国电商运营专家」，完成一次对话。",
    }
    eid, name = cb._expert_hint(task)
    assert eid == "ex_GDl31WahyIH0"
    assert name == "中国电商运营专家"


def test_resolve_expert_matches_by_id_not_name(cb, monkeypatch):
    """回归：任务名与市场真实名不同（中国电商运营专家 vs 卖得好），必须靠 id 命中。

    曾按 display_name_zh 字符串匹配 → 匹配不到 → 伪造本地条目 → 上报 200 不计分。
    """
    pool = [
        {"expert_id": "ex_other", "display_name_zh": "中国电商运营专家", "version": "9.9.9"},
        {"expert_id": "ex_GDl31WahyIH0", "display_name_zh": "卖得好", "version": "1.0.1"},
    ]
    task = {
        "jump_url": "x?expertId=ex_GDl31WahyIH0",
        "task_desc": "召唤「中国电商运营专家」",
    }
    expert = cb._resolve_expert(object(), "at", "uid", task, pool)
    assert expert["expert_id"] == "ex_GDl31WahyIH0"
    assert expert["display_name_zh"] == "卖得好"
    assert expert["version"] == "1.0.1"


def test_resolve_expert_falls_back_to_keyword_then_id(cb, monkeypatch):
    """首页池没有时用任务名当关键词搜，仍按 id 认人（不按名字字符串比）。"""

    class _BA:
        def market_expert_list(self, at, uid, expert_type="", keyword=""):
            assert keyword == "直播电商教练"
            return [
                {"expert_id": "ex_WZY136wV5aMt", "display_name_zh": "播旺旺", "version": "1.0.2"}
            ]

    task = {"jump_url": "x?expertId=ex_WZY136wV5aMt", "task_desc": "召唤「直播电商教练」"}
    expert = cb._resolve_expert(_BA(), "at", "uid", task, [])
    assert expert["expert_id"] == "ex_WZY136wV5aMt"
    assert expert["display_name_zh"] == "播旺旺"


def test_resolve_expert_never_fabricates(cb):
    """定位不到真实条目时返回空表：伪造条目会导致上报 200 但不计分。"""
    task = {"jump_url": "x?expertId=ex_unknown", "task_desc": "召唤「不存在的专家」"}
    assert cb._resolve_expert(object(), "at", "uid", task, []) == {}


def test_task_confirmed_reads_server_progress(cb, monkeypatch):
    """done 标记必须回读服务端进度：上报 200 不等于计分。"""
    states = [
        {"x": {"progress": {"current": 0, "target": 1}, "accept_status": "accepted"}},
        {"x": {"progress": {"current": 1, "target": 1}, "accept_status": "accepted"}},
        {"x": {"status": "completed"}},
        {},
    ]
    seq = iter(states)
    monkeypatch.setattr(cb, "_task_map", lambda *a, **k: next(seq))

    assert cb._task_confirmed(None, "at", "uid", "x") is False  # 0/1 未推进
    assert cb._task_confirmed(None, "at", "uid", "x") is True   # 1/1 推进
    assert cb._task_confirmed(None, "at", "uid", "x") is True   # completed
    assert cb._task_confirmed(None, "at", "uid", "x") is False  # 任务消失


# ── 回归：专家任务按形态识别，新任务无需改代码 ──────────────


def test_is_expert_task_accepts_any_code_shape(cb):
    """上游活动任务 code 随活动起名：只要形态对，任意 code 都该接管。"""
    for code in ("expert_use_maidehao", "WorkBuddy_Valorant", "brand_new_activity_2027"):
        task = {
            "task_code": code,
            "jump_url": "x?expertId=ex_GDl31WahyIH0",
            "task_desc": "在专家页搜索并召唤「某个专家」，完成一次对话。",
        }
        assert cb._is_expert_task(task) is True, code


def test_is_expert_task_rejects_non_expert_shapes(cb):
    """缺 expertId / 缺专家名 / 缺召唤动作 的都不算专家任务。"""
    assert not cb._is_expert_task({"task_code": "x", "task_desc": "召唤「专家」对话"})
    assert not cb._is_expert_task(
        {"task_code": "x", "jump_url": "x?expertId=ex_1", "task_desc": "完成一次对话"}
    )
    # 「召唤」单独出现已足够（判据是 召唤 or 对话）
    assert cb._is_expert_task(
        {"task_code": "x", "jump_url": "x?expertId=ex_1", "task_desc": "召唤「专家」"}
    )


def test_is_expert_task_excludes_dedicated_branches(cb):
    """轻量云与专家团有专属判据分支，不能被通用形态识别重复接管。"""
    lh = {
        "task_code": "Expert_lighthouse",
        "jump_url": "x?expertId=ex_2cvvUZQhDyeJ",
        "task_desc": "召唤「腾讯轻量云专家」完成一次对话",
    }
    team = {
        "task_code": "Expert_team_use_3",
        "jump_url": "x?expertId=ex_team",
        "task_desc": "召唤「某专家团」完成一次对话",
    }
    assert cb._is_expert_task(lh) is False
    assert cb._is_expert_task(team) is False
