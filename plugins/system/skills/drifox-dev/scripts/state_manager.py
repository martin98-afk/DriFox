"""drifox-dev 有状态技能的状态管理器。

提供以下能力：
- 加载/保存 state.json（自动从模板初始化）
- 原子写入（tempfile + os.replace，防止崩溃时损坏）
- 跨平台文件锁（Windows: msvcrt，Linux/Mac: fcntl）
- 版本迁移（schema 升级时自动迁移）
- CLI 子命令：init / show / focus / decision / pitfall / question / reset

用法（CLI）：
    python -m plugins.system.skills.drifox-dev.scripts.state_manager init
    python -m plugins.system.skills.drifox-dev.scripts.state_manager show
    python -m plugins.system.skills.drifox-dev.scripts.state_manager focus --task "重构 tool_control_card"
    python -m plugins.system.skills.drifox-dev.scripts.state_manager pitfall --module tool_control_card --symptom "..." --cause "..." --fix "..."
    python -m plugins.system.skills.drifox-dev.scripts.state_manager decision --scope "agent" --decision "..." --rationale "..."

也可以被其他脚本以 Python 模块方式 import 使用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------- 路径定位 ----------

# 本文件位置：plugins/system/skills/drifox-dev/scripts/state_manager.py
# 技能根目录 = parents[3]
SKILL_DIR = Path(__file__).resolve().parents[1]
STATE_DIR = SKILL_DIR / "state"
STATE_FILE = STATE_DIR / "state.json"
TEMPLATE_FILE = STATE_DIR / "state.template.json"

# 当前支持的 state schema 版本
CURRENT_VERSION = "1.0.0"

# 保留的最近决策/坑点/问题数量上限
MAX_RECENT_DECISIONS = 50
MAX_KNOWN_PITFALLS = 50
MAX_OPEN_QUESTIONS = 20


# ---------- 文件锁（跨平台）----------


class _NullLock:
    """占位锁（不支持的平台上退化为无锁，依赖原子 rename 保证安全）。"""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@contextmanager
def file_lock(filepath: Path):
    """获取状态文件的排他锁，跨平台。

    - Windows: msvcrt.locking
    - Linux/Mac: fcntl.flock
    - 其它平台: 无锁（仅依赖原子 rename）
    """
    lockfile = filepath.with_suffix(filepath.suffix + ".lock")
    try:
        if sys.platform == "win32":
            import msvcrt

            # 以二进制追加模式打开，文件不存在则创建
            fh = open(lockfile, "a+")
            try:
                # 锁 1 字节；区域是文件起始
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            except OSError:
                # LK_LOCK 在某些 Windows 终端可能失败，尝试 LK_NBLCK 后回退
                pass
            try:
                yield fh
            finally:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                fh.close()
        elif sys.platform in ("linux", "darwin"):
            import fcntl

            fh = open(lockfile, "w")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                yield fh
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()
        else:
            yield _NullLock()
    except (ImportError, OSError):
        # 锁不可用时不阻塞主流程（原子 rename 仍能保证数据完整性）
        yield _NullLock()


# ---------- 原子写入 ----------


def atomic_write_json(filepath: Path, data: dict) -> None:
    """原子写入 JSON：先写临时文件，再 rename 覆盖。"""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=filepath.parent,
        prefix=f".{filepath.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
    except Exception:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
        raise


# ---------- 状态加载/保存 ----------


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_state() -> dict:
    """加载 state.json；不存在则从模板初始化。

    返回的 state 一定包含 version 字段和所有顶层 key。
    """
    if not STATE_FILE.exists():
        return init_state()

    with file_lock(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[state_manager] 警告: 读取 {STATE_FILE.name} 失败: {e}，回退到模板", file=sys.stderr)
            return init_state()

    # 防御：data 必须是 dict 才能继续
    if not isinstance(data, dict):
        print(f"[state_manager] 警告: {STATE_FILE.name} 顶层不是 dict（{type(data).__name__}），回退到模板", file=sys.stderr)
        return init_state()

    # 迁移
    try:
        migrated = migrate_state(data)
    except Exception as e:
        print(f"[state_manager] 警告: 迁移失败: {e}，回退到模板", file=sys.stderr)
        return init_state()

    if migrated.get("version") != data.get("version"):
        save_state(migrated)
    return migrated


def save_state(state: dict) -> None:
    """加锁并原子写入 state.json。"""
    state["version"] = CURRENT_VERSION
    with file_lock(STATE_FILE):
        atomic_write_json(STATE_FILE, state)


def init_state() -> dict:
    """从模板初始化 state.json 并返回。"""
    if not TEMPLATE_FILE.exists():
        # 极端兜底：连模板都没有就构造一个最小可用版本
        minimal = {
            "version": CURRENT_VERSION,
            "current_focus": {"task": None, "module": None, "branch": None,
                              "started_at": None, "last_touched": None},
            "recent_decisions": [],
            "user_preferences": {
                "log_language": "zh",
                "comment_language": "zh",
                "naming_style": "snake_case",
                "no_unrelated_refactor": True,
                "auto_sync_docs": True,
            },
            "known_pitfalls": [],
            "open_questions": [],
            "auto_snapshot": {
                "last_updated": None,
                "key_files_lines": {},
                "recent_commits": [],
                "uncommitted_changes": {"dirty": False, "files": []},
            },
        }
        save_state(minimal)
        return minimal

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = CURRENT_VERSION
    # 移除 _comment 这类纯模板元数据，避免污染运行时 state.json
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    # 递归清理嵌套字典里的 _comment
    data = _strip_meta(data)
    save_state(data)
    return data


def _strip_meta(obj: Any) -> Any:
    """递归移除所有以 _ 开头的键（模板元数据）。"""
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_meta(v) for v in obj]
    return obj


# ---------- 版本迁移 ----------


def migrate_state(state: dict) -> dict:
    """根据 version 字段按顺序应用迁移。"""
    version = state.get("version", "0.0.0")
    if version == CURRENT_VERSION:
        return state

    # 未来扩展示例（保留注释演示迁移链模式）：
    # if version == "0.9.0":
    #     state = _migrate_0_9_to_1_0(state)
    #     version = "1.0.0"
    #
    # if version == "1.0.0":
    #     state = _migrate_1_0_to_1_1(state)
    #     version = "1.1.0"

    state["version"] = CURRENT_VERSION
    return state


# ---------- 业务操作（带去重/容量限制）----------


def _gen_id(existing: list[dict], prefix: str) -> str:
    """生成 3 位自增 ID（去重友好，足够小型技能使用）。"""
    used = {item.get("id", "") for item in existing}
    n = 1
    while True:
        cand = f"{prefix}{n:03d}"
        if cand not in used:
            return cand
        n += 1


def set_focus(task: str, module: str | None = None, branch: str | None = None) -> dict:
    """设置或更新 current_focus。"""
    state = load_state()
    focus = state.get("current_focus", {})
    now = _now_iso()

    if not focus.get("started_at"):
        focus["started_at"] = now
    focus["task"] = task
    if module is not None:
        focus["module"] = module
    if branch is not None:
        focus["branch"] = branch
    focus["last_touched"] = now

    state["current_focus"] = focus
    save_state(state)
    return focus


def clear_focus() -> None:
    state = load_state()
    state["current_focus"] = {
        "task": None, "module": None, "branch": None,
        "started_at": None, "last_touched": None,
    }
    save_state(state)


def add_decision(scope: str, decision: str, rationale: str) -> dict:
    state = load_state()
    entry = {
        "id": _gen_id(state.get("recent_decisions", []), "D"),
        "date": _now_iso(),
        "scope": scope,
        "decision": decision,
        "rationale": rationale,
    }
    decisions = state.get("recent_decisions", [])
    decisions.insert(0, entry)
    state["recent_decisions"] = decisions[:MAX_RECENT_DECISIONS]
    save_state(state)
    return entry


def add_pitfall(module: str, symptom: str, cause: str, fix: str) -> dict:
    state = load_state()
    # 同模块去重（用 cause 去重更稳）
    pitfalls = state.get("known_pitfalls", [])
    for p in pitfalls:
        if p.get("module") == module and p.get("cause") == cause:
            p["symptom"] = symptom
            p["fix"] = fix
            p["updated_at"] = _now_iso()
            state["known_pitfalls"] = pitfalls
            save_state(state)
            return p

    entry = {
        "id": _gen_id(pitfalls, "P"),
        "module": module,
        "symptom": symptom,
        "cause": cause,
        "fix": fix,
        "discovered_at": _now_iso(),
    }
    pitfalls.insert(0, entry)
    state["known_pitfalls"] = pitfalls[:MAX_KNOWN_PITFALLS]
    save_state(state)
    return entry


def add_question(question: str, context: str = "") -> dict:
    state = load_state()
    entry = {
        "id": _gen_id(state.get("open_questions", []), "Q"),
        "question": question,
        "context": context,
        "created_at": _now_iso(),
    }
    questions = state.get("open_questions", [])
    questions.insert(0, entry)
    state["open_questions"] = questions[:MAX_OPEN_QUESTIONS]
    save_state(state)
    return entry


def resolve_question(qid: str) -> None:
    state = load_state()
    state["open_questions"] = [
        q for q in state.get("open_questions", []) if q.get("id") != qid
    ]
    save_state(state)


def update_snapshot(snapshot: dict) -> None:
    """由 snapshot_project.py 调用，更新 auto_snapshot 字段。"""
    state = load_state()
    state["auto_snapshot"] = snapshot
    save_state(state)


def set_preference(key: str, value: Any) -> None:
    if key.startswith("_"):
        raise ValueError("不能修改 _ 开头的内部字段")
    state = load_state()
    prefs = state.get("user_preferences", {})
    if not isinstance(prefs, dict):
        prefs = {}
    prefs[key] = value
    state["user_preferences"] = prefs
    save_state(state)


def render_state_summary(state: dict) -> str:
    """渲染给 AI 看的精简状态摘要（作为技能加载的注入前缀）。"""
    lines = ["# drifox-dev 有状态技能 — 当前状态摘要", ""]

    focus = state.get("current_focus") or {}
    if focus.get("task"):
        lines.append("## 🎯 当前焦点")
        lines.append(f"- 任务: {focus.get('task')}")
        if focus.get("module"):
            lines.append(f"- 模块: {focus['module']}")
        if focus.get("branch"):
            lines.append(f"- 分支: {focus['branch']}")
        if focus.get("started_at"):
            lines.append(f"- 开始: {focus['started_at']}")
        if focus.get("last_touched"):
            lines.append(f"- 上次: {focus['last_touched']}")
        lines.append("")

    prefs = state.get("user_preferences", {})
    if prefs:
        lines.append("## 👤 用户偏好")
        for k, v in prefs.items():
            if k.startswith("_"):
                continue
            lines.append(f"- {k}: `{v}`")
        lines.append("")

    decisions = state.get("recent_decisions", [])
    if decisions:
        lines.append("## 🧭 近期决策（最近 5 条）")
        for d in decisions[:5]:
            lines.append(f"- **{d.get('id')}** [{d.get('date', '')[:10]}] "
                         f"{d.get('scope')}: {d.get('decision')}")
        lines.append("")

    pitfalls = state.get("known_pitfalls", [])
    if pitfalls:
        lines.append(f"## ⚠️ 已知坑点（共 {len(pitfalls)} 条）")
        for p in pitfalls[:5]:
            lines.append(f"- **{p.get('id')}** [{p.get('module')}] "
                         f"{p.get('symptom')} → 根因: {p.get('cause')[:60]}")
        if len(pitfalls) > 5:
            lines.append(f"- ... 另 {len(pitfalls) - 5} 条详见 state.json")
        lines.append("")

    questions = state.get("open_questions", [])
    if questions:
        lines.append(f"## ❓ 开放问题（{len(questions)}）")
        for q in questions[:3]:
            lines.append(f"- **{q.get('id')}** {q.get('question')}")
        if len(questions) > 3:
            lines.append(f"- ... 另 {len(questions) - 3} 条")
        lines.append("")

    snap = state.get("auto_snapshot") or {}
    if snap.get("last_updated"):
        lines.append("## 📊 项目快照")
        lines.append(f"- 更新于: {snap.get('last_updated')}")
        kfl = snap.get("key_files_lines") or {}
        if kfl:
            lines.append("- 关键文件行数（实时统计）:")
            for path, lines_n in list(kfl.items())[:5]:
                lines.append(f"  - `{path}`: {lines_n} 行")
            if len(kfl) > 5:
                lines.append(f"  - ... 另 {len(kfl) - 5} 个")
        commits = snap.get("recent_commits") or []
        if commits:
            lines.append("- 最近 commits:")
            for c in commits[:3]:
                lines.append(f"  - `{c.get('hash', '')[:7]}` {c.get('message', '')[:70]}")
        uc = snap.get("uncommitted_changes") or {}
        if uc.get("dirty"):
            lines.append(f"- ⚠️ 有 {len(uc.get('files', []))} 个未提交变更")
        lines.append("")

    lines.append("---")
    lines.append("（以上为动态状态，请结合下方 SKILL.md 静态骨架使用）")
    return "\n".join(lines)


# ---------- CLI ----------


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_init(args) -> int:
    state = init_state()
    print(f"[state_manager] 已初始化 {STATE_FILE}")
    _print_json(state)
    return 0


def cmd_show(args) -> int:
    state = load_state()
    if getattr(args, "summary", False):
        print(render_state_summary(state))
    else:
        _print_json(state)
    return 0


def cmd_focus(args) -> int:
    if args.clear:
        clear_focus()
        print("[state_manager] 已清除 current_focus")
        return 0
    focus = set_focus(args.task, module=args.module, branch=args.branch)
    print(f"[state_manager] 已设置焦点: {focus.get('task')}")
    return 0


def cmd_decision(args) -> int:
    entry = add_decision(args.scope, args.decision, args.rationale)
    print(f"[state_manager] 已记录决策 {entry['id']}: {entry['decision']}")
    return 0


def cmd_pitfall(args) -> int:
    entry = add_pitfall(args.module, args.symptom, args.cause, args.fix)
    print(f"[state_manager] 已记录坑点 {entry['id']} ({entry['module']})")
    return 0


def cmd_question(args) -> int:
    if args.resolve:
        resolve_question(args.resolve)
        print(f"[state_manager] 已解决/移除问题 {args.resolve}")
        return 0
    entry = add_question(args.question, args.context or "")
    print(f"[state_manager] 已记录开放问题 {entry['id']}: {entry['question']}")
    return 0


def cmd_preference(args) -> int:
    # 把 --value 解析为 JSON（支持字符串/数字/布尔/null/对象/数组）
    try:
        v = json.loads(args.value)
    except json.JSONDecodeError:
        v = args.value
    set_preference(args.key, v)
    print(f"[state_manager] 已设置偏好 {args.key} = {v}")
    return 0


def cmd_path(args) -> int:
    print(STATE_FILE)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drifox-dev-state",
        description="drifox-dev 有状态技能的状态管理器",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="从模板初始化 state.json").set_defaults(func=cmd_init)

    sp = sub.add_parser("show", help="查看 state.json（默认 JSON，加 --summary 渲染摘要）")
    sp.add_argument("--summary", action="store_true", help="渲染给 AI 看的精简摘要")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("focus", help="设置/更新/清除 current_focus")
    sp.add_argument("--task", help="任务名（清除时省略）")
    sp.add_argument("--module", help="相关模块")
    sp.add_argument("--branch", help="相关 git 分支")
    sp.add_argument("--clear", action="store_true", help="清除焦点")
    sp.set_defaults(func=cmd_focus)

    sp = sub.add_parser("decision", help="记录一条架构/技术决策")
    sp.add_argument("--scope", required=True, help="决策作用域（agent/chat_worker/...）")
    sp.add_argument("--decision", required=True, help="决策内容")
    sp.add_argument("--rationale", required=True, help="决策理由")
    sp.set_defaults(func=cmd_decision)

    sp = sub.add_parser("pitfall", help="记录一条已知坑点")
    sp.add_argument("--module", required=True, help="出现坑的模块")
    sp.add_argument("--symptom", required=True, help="症状")
    sp.add_argument("--cause", required=True, help="根因")
    sp.add_argument("--fix", required=True, help="修复方案")
    sp.set_defaults(func=cmd_pitfall)

    sp = sub.add_parser("question", help="记录或解决一个开放问题")
    sp.add_argument("--question", help="问题内容（不传则视为解决）")
    sp.add_argument("--context", help="问题背景")
    sp.add_argument("--resolve", help="用 --resolve Q001 解决问题")
    sp.set_defaults(func=cmd_question)

    sp = sub.add_parser("preference", help="设置/更新用户偏好")
    sp.add_argument("--key", required=True, help="偏好名")
    sp.add_argument("--value", required=True, help="值（支持 JSON 字符串）")
    sp.set_defaults(func=cmd_preference)

    sub.add_parser("path", help="打印 state.json 绝对路径").set_defaults(func=cmd_path)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
