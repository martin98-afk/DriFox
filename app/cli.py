# -*- coding: utf-8 -*-
"""
DriFox CLI — 无头模式入口

提供命令行界面调用 DriFox 核心引擎：
    drifox chat [-z "prompt"] [-w WORKDIR] [--json]     # 聊天（默认）
    drifox doctor                                         # 环境诊断
    drifox status                                         # 状态查看
    drifox --version                                      # 版本号

架构原则：
    - 复用 app.core.backend.ChatBackend 的所有核心组件
    - 通过无头 QApplication 提供 Qt 事件循环（不创建窗口）
    - 信号 → stdout 适配器代替 UI 渲染
"""
import argparse
import os
import sys
import json as stdjson
from typing import Optional

from PyQt5.QtCore import QEventLoop


def build_arg_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器"""
    parser = argparse.ArgumentParser(
        prog="drifox",
        description="DriFox 飘狐 — 轻量化 AI 桌面对话助手（命令行模式）",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="显示版本号并退出",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="子命令")

    # === chat 子命令（默认）===
    chat_parser = subparsers.add_parser("chat", help="启动聊天（默认）")
    chat_parser.add_argument(
        "-z", "--oneshot",
        type=str,
        default="",
        help="一次性查询模式：直接执行 prompt 并退出",
    )
    chat_parser.add_argument(
        "-w", "--workdir",
        type=str,
        default=None,
        help="工作目录（默认当前目录）",
    )
    chat_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="JSON 格式输出（仅 oneshot 模式）",
    )

    # === doctor 子命令 ===
    subparsers.add_parser("doctor", help="环境诊断")

    # === status 子命令 ===
    subparsers.add_parser("status", help="查看系统状态")

    return parser


def main():
    """CLI 主入口"""
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.version:
        _show_version()
        return

    subcommand = args.subcommand or "chat"

    if subcommand == "chat":
        _run_chat(args)
    elif subcommand == "doctor":
        _run_doctor()
    elif subcommand == "status":
        _run_status()
    else:
        parser.print_help()


def _show_version():
    """显示版本号"""
    try:
        from app.constants import __version__
    except ImportError:
        __version__ = "0.2.8-dev"
    print(f"DriFox v{__version__}")


def _run_chat(args):
    """启动聊天模式"""
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import QEventLoop

    # 创建无头 QApplication（不创建任何窗口）
    app = QApplication.instance()
    if app is None:
        # 不传入 sys.argv 以避免解析冲突
        app = QApplication([])

    # 初始化后端
    backend = _init_headless_backend(args.workdir)
    if not backend:
        print("错误: 后端初始化失败", file=sys.stderr)
        sys.exit(1)

    if args.oneshot:
        _run_oneshot(backend, args)
    else:
        _run_repl(backend, args)


def _init_headless_backend(workdir: Optional[str] = None):
    """初始化无头后端

    复用 ChatBackend 的所有核心组件，只是不连接 UI 信号。
    """
    try:
        from app.core.backend import ChatBackend
        from app.utils.config import Settings

        settings = Settings.get_instance()
        cwd = workdir or os.getcwd()

        backend = ChatBackend()

        def get_model_config():
            """从配置构建模型配置（简化版，不依赖 UI 的 _valid_configs）"""
            saved = dict(settings.llm_saved_providers.value or {})
            selected = settings.llm_selected_model.value or ""

            # 尝试用 selected key 查找已保存的 provider 配置
            if selected and selected in saved:
                cfg = dict(saved[selected])
                # 确保必要字段存在
                cfg.setdefault("provider", cfg.get("provider_name", "openai"))
                cfg.setdefault("模型名称", cfg.get("模型名称", cfg.get("model", "gpt-4")))
                cfg.setdefault("API_KEY", cfg.get("api_key", settings.llm_api_key.value or ""))
                cfg.setdefault("APIBase", cfg.get("base_url", settings.llm_api_base.value))
                return cfg

            # 兜底：从 Settings 基本配置构建
            return {
                "provider": "openai",
                "模型名称": settings.llm_model.value or "gpt-4",
                "API_KEY": settings.llm_api_key.value or "",
                "APIBase": settings.llm_api_base.value or "https://api.openai.com/v1",
                "最大Token": settings.llm_max_tokens.value or 4096,
                "温度": settings.llm_temperature.value or 0.7,
            }

        backend.initialize(
            get_model_config=get_model_config,
            workdir=cwd,
        )
        return backend
    except Exception as e:
        print(f"后端初始化失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return None


def _run_oneshot(backend, args):
    """一次性查询模式"""
    event_loop = QEventLoop()

    event_loop = QEventLoop()
    collected_content = []
    collected_tool_calls = []
    error_message = None

    def on_stream_chunk(chunk: str):
        if not args.json:
            print(chunk, end="", flush=True)
        collected_content.append(chunk)

    def on_reasoning_chunk(piece: str):
        if not args.json:
            print(f"\033[90m{piece}\033[0m", end="", flush=True)

    def on_tool_call(id_, name, arguments, round_id=None):
        collected_tool_calls.append({"name": name, "arguments": arguments})
        if not args.json:
            args_summary = _summarize_args(name, arguments)
            print(f"\n\033[33m┊ 🛠  {name}{args_summary}\033[0m", flush=True)

    def on_tool_result(id_, name, arguments, result):
        if not args.json:
            status = "✓" if getattr(result, 'success', True) else "✗"
            print(f"\033[33m┊ {status} {name} 返回\033[0m", flush=True)

    def on_stream_finished(response):
        if args.json:
            output = stdjson.dumps({
                "content": "".join(collected_content),
                "tool_calls": collected_tool_calls,
                "error": error_message,
            }, ensure_ascii=False, indent=2)
            print(output)
        else:
            print()  # 换行
        event_loop.quit()

    def on_error(error: str):
        nonlocal error_message
        error_message = error
        print(f"\n\033[31m错误: {error}\033[0m", file=sys.stderr)
        event_loop.quit()

    def on_question_asked(id_, questions, extra):
        """自动回答 N/A 跳过 question 工具"""
        backend.provide_question_answer("N/A")

    # 使用 set_all_callbacks 注册回调（ChatEngine._emit 使用这些回调）
    callbacks = {
        "content_received": on_stream_chunk,
        "reasoning_content_received": on_reasoning_chunk,
        "tool_call_started": on_tool_call,
        "tool_result_received": lambda i, n, a, r: on_tool_result(i, n, a, r),
        "stream_finished": on_stream_finished,
        "error": on_error,
        "question_asked": on_question_asked,
    }
    backend.set_all_callbacks(callbacks)

    # 发送 prompt
    success = backend.send_message_to_engine(args.oneshot)
    if not success:
        print("错误: 无法发送消息", file=sys.stderr)
        sys.exit(1)

    # 等待完成
    event_loop.exec_()


def _run_repl(backend, args):
    """交互式 REPL 模式"""
    event_loop = QEventLoop()

    _streaming = [False]
    _pending_exit = [False]

    def on_stream_chunk(chunk: str):
        print(chunk, end="", flush=True)

    def on_stream_started():
        _streaming[0] = True

    def on_stream_finished(response):
        _streaming[0] = False
        print()  # 换行
        if _pending_exit[0]:
            event_loop.quit()

    def on_error(error: str):
        _streaming[0] = False
        print(f"\n\033[31m错误: {error}\033[0m", file=sys.stderr)

    def on_tool_call(id_, name, arguments, round_id=None):
        args_summary = _summarize_args(name, arguments)
        print(f"\n\033[33m┊ 🛠  {name}{args_summary}\033[0m", flush=True)

    def on_question_asked(id_, questions, extra):
        backend.provide_question_answer("N/A")

    # 注册回调到 ChatEngine
    callbacks = {
        "content_received": on_stream_chunk,
        "tool_call_started": on_tool_call,
        "stream_started": on_stream_started,
        "stream_finished": on_stream_finished,
        "error": on_error,
        "question_asked": on_question_asked,
    }
    backend.set_all_callbacks(callbacks)

    print("\033[36m" + "=" * 50 + "\033[0m")
    print("\033[36m  DriFox CLI 交互模式\033[0m")
    print("\033[36m  输入 /help 查看命令，/quit 退出\033[0m")
    print("\033[36m" + "=" * 50 + "\033[0m")
    print()

    try:
        while not _pending_exit[0]:
            try:
                user_input = input("\033[32m❯ \033[0m")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input.strip():
                continue

            if user_input.strip() == "/quit":
                if _streaming[0]:
                    _pending_exit[0] = True
                    backend.stop_streaming()
                break

            if user_input.strip() == "/help":
                _show_repl_help()
                continue

            if user_input.strip() == "/clear":
                backend.create_session()
                print("\033[90m--- 会话已重置 ---\033[0m")
                continue

            backend.send_message_to_engine(user_input)
    finally:
        print("\n\033[90m再见！\033[0m")


def _show_repl_help():
    """显示 REPL 帮助"""
    print("""\033[36m
可用命令:
  /help      显示此帮助
  /clear     重置当前会话
  /quit      退出 CLI
\033[0m""")


def _summarize_args(tool_name: str, arguments: dict) -> str:
    """简化工具参数显示"""
    if tool_name in ("read", "write", "edit", "multi_edit"):
        path = arguments.get("path") or arguments.get("filePath", "")
        return f" \033[90m{path}\033[0m" if path else ""
    if tool_name == "bash":
        cmd = arguments.get("command", "")
        return f" \033[90m{cmd[:60]}\033[0m" if cmd else ""
    if tool_name == "grep":
        p = arguments.get("pattern", "")
        return f" \033[90m/{p}/\033[0m" if p else ""
    if tool_name in ("websearch", "webfetch"):
        q = arguments.get("query") or arguments.get("url", "")
        return f" \033[90m{q[:60]}\033[0m" if q else ""
    return ""


def _run_doctor():
    """环境诊断"""
    import platform

    print("\033[36m=== DriFox 环境诊断 ===\033[0m")
    print()

    # Python 版本
    print(f"Python:   {sys.version.split()[0]}")
    print(f"平台:     {platform.system()} {platform.release()}")
    print(f"架构:     {platform.machine()}")

    # 检查 PyQt5
    try:
        from PyQt5.QtCore import QT_VERSION_STR
        print(f"PyQt5:    {QT_VERSION_STR} ✓")
    except ImportError:
        print("PyQt5:    ✗ 未安装")

    # 检查关键依赖
    deps = [
        ("openai", "OpenAI SDK"),
        ("loguru", "Loguru"),
        ("orjson", "orjson"),
        ("httpx", "HTTPX"),
    ]
    for mod_name, display_name in deps:
        try:
            __import__(mod_name)
            print(f"{display_name}: ✓")
        except ImportError:
            print(f"{display_name}: ✗ 未安装")

    print()
    print("\033[36m=== 诊断完成 ===\033[0m")


def _run_status():
    """显示系统状态"""
    print("\033[36m=== DriFox 状态 ===\033[0m")
    print()

    try:
        from app.utils.config import Settings
        settings = Settings.get_instance()
        saved = dict(settings.llm_saved_providers.value or {})
        selected = settings.llm_selected_model.value or ""
        if selected and selected in saved:
            cfg = saved[selected]
            print(f"当前模型:  {cfg.get('模型名称', '未设置')}")
            print(f"Provider:  {cfg.get('provider_name', '未设置')}")
            print(f"Base URL:  {cfg.get('APIBase', '未设置')}")
            print(f"API Key:   {'已设置' if cfg.get('API_KEY') else '未设置'}")
        else:
            print(f"当前模型:  {settings.llm_model.value or '未设置'}")
            print(f"Base URL:  {settings.llm_api_base.value or '未设置'}")
            print(f"API Key:   {'已设置' if settings.llm_api_key.value else '未设置'}")
    except Exception as e:
        print(f"配置读取失败: {e}")

    print()


if __name__ == "__main__":
    main()
