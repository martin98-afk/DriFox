#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Evolver DriFox Hook Handler

通过 DriFox Hook 系统自动捕获会话经验，写入 Evolver memory/ 目录，
并在适当时机触发 evolver CLI 生成进化 prompt。

使用方式（由 hooks.json 自动调用）：
    python evolver_hook.py --event=<EventName>

上下文数据通过 stdin (JSON) 传入，包含：
    - event_name: 事件名
    - message: 当前用户消息
    - project_root: 项目根目录
    - tool_name: 工具名（PostToolUse 时）
    - file: 操作文件路径（PostToolUse 时）
    - response: AI 回复内容（PostAssistantMessage 时）
    - error: 错误信息（PostAssistantMessage 出错时）
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime


# ============================================================
# 工具函数
# ============================================================

def get_script_dir() -> Path:
    """获取本脚本所在目录（用于定位插件资源）"""
    return Path(__file__).resolve().parent


def get_plugin_dir() -> Path:
    """获取插件根目录"""
    return get_script_dir().parent


def get_project_root(ctx: dict) -> Path:
    """获取项目根目录（优先从上下文取，回退到 CWD）"""
    root = ctx.get("project_root", "")
    if root:
        return Path(root)
    return Path.cwd()


def ensure_memory_dir(project_root: Path) -> Path:
    """确保 memory/ 目录存在，返回路径"""
    memory_dir = project_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    return memory_dir


def get_memory_log_path(memory_dir: Path, log_type: str) -> Path:
    """获取指定类型的日志文件路径"""
    today = datetime.now().strftime("%Y-%m-%d")
    return memory_dir / f"{log_type}_{today}.jsonl"


def append_jsonl(path: Path, data: dict):
    """追加一行 JSONL 到文件"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def check_evolver_installed() -> bool:
    """检查 evolver CLI 是否可用"""
    try:
        result = subprocess.run(
            ["npx", "--yes", "@evomap/evolver", "--version"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # 回退：检查全局 npm 安装
    try:
        result = subprocess.run(
            ["evolver", "--version"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def run_evolver(project_root: Path) -> str:
    """运行 evolver CLI，返回 GEP prompt 输出"""
    try:
        result = subprocess.run(
            ["npx", "--yes", "@evomap/evolver"],
            capture_output=True, text=True, timeout=60,
            cwd=str(project_root),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0:
            return result.stdout
        return f"[Evolver] CLI failed (exit {result.returncode}): {result.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return "[Evolver] CLI timed out after 60s"
    except FileNotFoundError:
        return "[Evolver] CLI not found (run: npm install -g @evomap/evolver)"
    except Exception as e:
        return f"[Evolver] Error: {e}"


def sanitize_for_json(obj):
    """递归清洗不可 JSON 序列化的对象"""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [sanitize_for_json(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        return str(obj)


# ============================================================
# 事件处理器
# ============================================================

def handle_session_start(ctx: dict):
    """SessionStart: 初始化检查"""
    project_root = get_project_root(ctx)
    memory_dir = ensure_memory_dir(project_root)
    plugin_dir = get_plugin_dir()

    # 检查 evolver 是否可用
    evolver_ok = check_evolver_installed()

    # 写入初始化事件
    append_jsonl(
        get_memory_log_path(memory_dir, "session"),
        {
            "event": "SessionStart",
            "timestamp": time.time(),
            "datetime": datetime.now().isoformat(),
            "project_root": str(project_root),
            "evolver_installed": evolver_ok,
            "plugin_version": "1.0.0",
        }
    )

    # 如果 evolver 不可用，在 memory/ 下写一个 README 提示
    if not evolver_ok:
        hint_file = memory_dir / "README.md"
        if not hint_file.exists():
            hint_file.write_text(
                "# Evolver Memory\n\n"
                "此目录由 DriFox Evolver 插件管理，用于存储会话经验日志。\n\n"
                "## 安装 Evolver CLI 以启用自进化\n\n"
                "```bash\n"
                "npm install -g @evomap/evolver\n"
                "```\n"
                "\n安装后重启 DriFox 即可自动激活进化能力。\n",
                encoding="utf-8",
            )


def handle_post_tool_use(ctx: dict):
    """PostToolUse: 记录工具操作到 memory"""
    project_root = get_project_root(ctx)
    memory_dir = ensure_memory_dir(project_root)

    tool_name = ctx.get("tool_name", "unknown")
    file_path = ctx.get("file", "")
    message = ctx.get("message", "")

    record = {
        "event": "PostToolUse",
        "timestamp": time.time(),
        "datetime": datetime.now().isoformat(),
        "tool": tool_name,
        "file": file_path,
        "message_preview": message[:200] if message else "",
    }

    append_jsonl(get_memory_log_path(memory_dir, "tools"), record)


def handle_post_assistant_message(ctx: dict):
    """PostAssistantMessage: 记录 AI 回复到 memory"""
    project_root = get_project_root(ctx)
    memory_dir = ensure_memory_dir(project_root)

    response = ctx.get("response", "")
    error = ctx.get("error", "")
    message = ctx.get("message", "")

    record = {
        "event": "PostAssistantMessage",
        "timestamp": time.time(),
        "datetime": datetime.now().isoformat(),
        "user_message_preview": message[:200] if message else "",
        "response_preview": response[:500] if response else "",
        "has_error": bool(error),
        "error_preview": error[:200] if error else "",
    }

    append_jsonl(get_memory_log_path(memory_dir, "assistant"), record)

    # 每累计 N 条 assistant 消息，自动触发 evolver 检查
    log_path = get_memory_log_path(memory_dir, "assistant")
    if log_path.exists():
        line_count = sum(1 for _ in open(log_path, "r", encoding="utf-8"))
        # 每 10 轮触发一次进化检查
        if line_count > 0 and line_count % 10 == 0:
            if check_evolver_installed():
                gep_prompt = run_evolver(project_root)
                # 将进化 prompt 写入 memory 供后续使用
                if gep_prompt and not gep_prompt.startswith("[Evolver]"):
                    append_jsonl(
                        get_memory_log_path(memory_dir, "evolution"),
                        {
                            "event": "EvolutionTrigger",
                            "timestamp": time.time(),
                            "datetime": datetime.now().isoformat(),
                            "cycle": line_count // 10,
                            "gep_prompt": gep_prompt[:1000],
                        }
                    )


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Evolver DriFox Hook Handler")
    parser.add_argument("--event", required=True, help="事件名 (SessionStart/PostToolUse/PostAssistantMessage)")
    args = parser.parse_args()

    # 从 stdin 读取上下文（HookManager 传入的 JSON）
    try:
        raw = sys.stdin.read()
        ctx = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        ctx = {}

    # 清洗上下文（确保可序列化）
    ctx = sanitize_for_json(ctx)

    # 路由到对应处理器
    event_name = args.event
    handler_map = {
        "SessionStart": handle_session_start,
        "PostToolUse": handle_post_tool_use,
        "PostAssistantMessage": handle_post_assistant_message,
    }

    handler = handler_map.get(event_name)
    if handler:
        try:
            handler(ctx)
        except Exception as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            sys.exit(1)
    else:
        print(json.dumps({"error": f"Unknown event: {event_name}"}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
