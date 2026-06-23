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
import json as stdjson
import os
import sys
from typing import Optional

from loguru import logger
from PyQt5.QtCore import QEventLoop

# CLI 独立版本号（与主应用版本解耦，可按需独立递增）
CLI_VERSION = "0.2.12"

# ANSI 颜色码
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_ORANGE = "\033[38;5;214m"
_GRAY = "\033[90m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _build_banner(model_info: str = "") -> str:
    """构造 DriFox ASCII banner（Claude Code 风格 + 飘狐元素）

    Args:
        model_info: 当前模型描述，如 "DeepSeek · deepseek-chat"
                    为空则不显示该行
    """
    fox_lines = [
        "    /\\_/\\\\  ",
        "   ( o.o ) ",
        "    > ^ <  ",
    ]
    if model_info:
        right_lines = [
            (f"{_BOLD}飘狐 · 轻量化 AI 桌面对话助手{_RESET}", fox_lines[0]),
            (f"{_CYAN}v{CLI_VERSION} · {model_info}{_RESET}", fox_lines[1]),
            ("", fox_lines[2]),
        ]
    else:
        right_lines = [
            (f"{_BOLD}飘狐 · 轻量化 AI 桌面对话助手{_RESET}", fox_lines[0]),
            (f"{_CYAN}v{CLI_VERSION}{_RESET}", fox_lines[1]),
            ("", fox_lines[2]),
        ]

    ascii_art = f"""{_ORANGE}
   ██████╗ ██████╗ ██╗███████╗ ██████╗ ██╗  ██╗{_RESET}
{_ORANGE}   ██╔══██╗██╔══██╗██║██╔════╝██╔═══██╗╚██╗██╔╝{_RESET}
{_ORANGE}   ██║  ██║██████╔╝██║█████╗  ██║   ██║ ╚███╔╝{_RESET}
{_ORANGE}   ██║  ██║██╔══██╗██║██╔══╝  ██║   ██║ ██╔██╗{_RESET}
{_ORANGE}   ██████╔╝██║  ██║██║██║     ╚██████╔╝██╔╝ ██╗{_RESET}
{_ORANGE}   ╚═════╝ ╚═╝  ╚═╝╚═╝╚═╝      ╚═════╝ ╚═╝  ╚═╝{_RESET}"""

    fox_block_lines = []
    for right, left in right_lines:
        fox_block_lines.append(f"  {_GRAY}{left}{_RESET}  {right}")

    fox_block = "\n".join(fox_block_lines)

    return (
        ascii_art
        + "\n\n"
        + fox_block
        + f"\n{_GRAY}────────────────────────────────────────────────{_RESET}\n"
        + f"  {_YELLOW}/help{_RESET}  ·  {_YELLOW}/clear{_RESET}  ·  {_YELLOW}/quit{_RESET}                          "
        + f"{_DIM}输入问题直接开始对话{_RESET}\n"
    )


def _print_banner(model_info: str = "") -> None:
    """打印 DriFox banner 到 stdout"""
    print(_build_banner(model_info))


def _resolve_model_info() -> str:
    """从 Settings 读取当前模型描述，用于 banner 显示。

    返回 "Provider · Model" 格式的字符串；配置缺失时返回空串。
    """
    try:
        from app.utils.config import Settings
        settings = Settings.get_instance()
        saved = dict(settings.llm_saved_providers.value or {})
        selected = settings.llm_selected_model.value or ""
        if selected and selected in saved:
            cfg = saved[selected]
            pname = cfg.get("provider_name", "") or "unknown"
            mname = cfg.get("模型名称", "") or "?"
            return f"{pname} · {mname}"
    except Exception:
        pass
    return ""



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
    chat_parser.add_argument(
        "-p", "--provider",
        type=str,
        default=None,
        help="选择模型服务商（名称或关键词，如 'deepseek'、'硅基'），不指定则使用上次选择的",
    )
    chat_parser.add_argument(
        "-m", "--model",
        type=str,
        default=None,
        help="指定模型名称（如 'deepseek-chat'、'gpt-4o'），覆盖服务商默认模型",
    )
    chat_parser.add_argument(
        "--api-base",
        type=str,
        default=None,
        help="覆盖 API Base URL",
    )
    chat_parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="覆盖 API Key",
    )
    chat_parser.add_argument(
        "--no-banner",
        action="store_true",
        default=False,
        help="不显示 ASCII banner（仅 REPL 模式）",
    )

    # === doctor 子命令 ===
    subparsers.add_parser("doctor", help="环境诊断")

    # === status 子命令 ===
    subparsers.add_parser("status", help="查看系统状态")

    # === providers 子命令 ===
    providers_parser = subparsers.add_parser("providers", help="列出已配置的模型服务商")
    providers_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="JSON 格式输出",
    )

    return parser


def main():
    """CLI 主入口"""
    # 禁用所有 loguru 输出，避免与大模型 stdout 混杂
    logger.remove()

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
    elif subcommand == "providers":
        _run_providers(args)
    else:
        parser.print_help()


def _show_version():
    """显示版本号

    优先级：
    1. 已安装包元数据 pip install → importlib.metadata
    2. pyproject.toml（开发模式 pip install -e .）
    3. CLI_VERSION 常量（兜底）
    """
    # 优先从已安装包元数据读取
    try:
        from importlib.metadata import version
        v = version("drifox-cli")
        print(f"DriFox v{v}")
        return
    except Exception:
        pass
    # 开发模式：从 pyproject.toml 读取
    try:
        import tomllib
        from pathlib import Path
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        if pyproject.exists():
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            v = data.get("project", {}).get("version", "")
            if v:
                print(f"DriFox v{v}")
                return
    except Exception:
        pass
    # 兜底：使用 CLI 独立版本号
    print(f"DriFox v{CLI_VERSION}")


def _run_chat(args):
    """启动聊天模式"""
    from PyQt5.QtWidgets import QApplication

    # 创建无头 QApplication（不创建任何窗口）
    app = QApplication.instance()
    if app is None:
        # 不传入 sys.argv 以避免解析冲突
        app = QApplication([])

    # 初始化后端（支持 provider/model 覆盖）
    backend = _init_headless_backend(
        args.workdir,
        provider_override=args.provider,
        model_override=args.model,
        api_base_override=args.api_base,
        api_key_override=args.api_key,
    )
    if not backend:
        print("错误: 后端初始化失败", file=sys.stderr)
        sys.exit(1)

    if args.oneshot:
        _run_oneshot(backend, args)
    else:
        _run_repl(backend, args)


def _init_headless_backend(
    workdir: Optional[str] = None,
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
    api_base_override: Optional[str] = None,
    api_key_override: Optional[str] = None,
):
    """初始化无头后端

    复用 ChatBackend 的所有核心组件，只是不连接 UI 信号。
    
    provider_override: 按名称/关键词匹配服务商（如 'deepseek'、'硅基'），None=使用已选
    model_override: 覆盖模型名称（如 'deepseek-chat'），None=使用服务商默认
    api_base_override: 覆盖 API Base URL
    api_key_override: 覆盖 API Key
    """
    try:
        from app.core.backend import ChatBackend
        from app.utils.config import Settings

        settings = Settings.get_instance()
        cwd = workdir or os.getcwd()

        backend = ChatBackend()

        def get_model_config():
            """从配置构建模型配置"""
            saved = dict(settings.llm_saved_providers.value or {})
            selected = settings.llm_selected_model.value or ""

            config_id = selected

            # ---- provider 覆盖：按名称/关键词匹配 ----
            if provider_override:
                matched = _match_provider(provider_override, saved, selected)
                if matched:
                    config_id = matched
                else:
                    print(f"\033[33m警告: 未找到匹配的服务商「{provider_override}」，使用当前配置\033[0m",
                          file=sys.stderr)

            # ---- 根据 config_id 获取配置 ----
            if config_id and config_id in saved:
                cfg = dict(saved[config_id])
            else:
                cfg = {}

            # ---- model 覆盖 ----
            final_model = model_override or cfg.get("模型名称", cfg.get("model", "gpt-4"))

            # ---- api_base 覆盖 ----
            base_url = (
                api_base_override
                or cfg.get("API_URL")
                or cfg.get("base_url")
                or settings.llm_api_base.value
            )

            # ---- api_key 覆盖 ----
            api_key = (
                api_key_override
                or cfg.get("API_KEY")
                or cfg.get("api_key")
                or settings.llm_api_key.value
                or ""
            )

            return {
                "provider": cfg.get("provider", cfg.get("provider_name", "openai")),
                "provider_name": cfg.get("provider_name", "openai"),
                "模型名称": final_model,
                "API_KEY": api_key,
                # NOTE: chat_worker 读取的是 "API_URL" 而非 "APIBase"
                "API_URL": base_url,
                "最大Token": cfg.get("最大Token", cfg.get("max_tokens", settings.llm_max_tokens.value or 4096)),
                "温度": cfg.get("温度", cfg.get("temperature", settings.llm_temperature.value or 0.7)),
                "认证方式": cfg.get("认证方式", "bearer"),
                "思考模式": cfg.get("思考模式"),
                "思考预算": cfg.get("思考预算"),
                "思考等级": cfg.get("思考等级"),
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


def _match_provider(keyword: str, saved: dict, current: str) -> Optional[str]:
    """按关键词匹配服务商 config_id。

    匹配优先级：
    1. config_id 精确匹配
    2. provider_name 精确/部分匹配（大小写不敏感）
    3. 模型名称部分匹配
    """
    kw = keyword.lower()

    # 精确匹配 config_id
    if kw in saved:
        return kw

    candidates = []
    for cid, info in saved.items():
        pname = (info.get("provider_name", "") or "").lower()
        mname = (info.get("模型名称", "") or "").lower()
        display = (info.get("name", "") or "").lower()

        if kw == pname or kw == display:
            return cid
        if kw in pname or kw in display or kw in mname:
            candidates.append(cid)

    if candidates:
        return candidates[0]
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

    # 展示 ASCII banner（除非 --no-banner）
    if not args.no_banner:
        _print_banner(model_info=_resolve_model_info())

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
            print(f"Base URL:  {cfg.get('API_URL', '未设置')}")
            print(f"API Key:   {'已设置' if cfg.get('API_KEY') else '未设置'}")
        else:
            print(f"当前模型:  {settings.llm_model.value or '未设置'}")
            print(f"Base URL:  {settings.llm_api_base.value or '未设置'}")
            print(f"API Key:   {'已设置' if settings.llm_api_key.value else '未设置'}")
    except Exception as e:
        print(f"配置读取失败: {e}")

    print()


def _run_providers(args):
    """列出已配置的模型服务商"""
    try:
        from app.constants import FREE_PROVIDERS
        from app.utils.config import Settings

        settings = Settings.get_instance()
        saved = dict(settings.llm_saved_providers.value or {})
        selected = settings.llm_selected_model.value or ""

        if args.json:
            output = []
            for cid, info in saved.items():
                entry = {
                    "config_id": cid,
                    "provider_name": info.get("provider_name", ""),
                    "model": info.get("模型名称", ""),
                    "api_url": info.get("API_URL", ""),
                    "api_key_set": bool(info.get("API_KEY", "")),
                    "is_active": cid == selected,
                }
                output.append(entry)
            print(stdjson.dumps(output, ensure_ascii=False, indent=2))
            return

        print("\033[36m=== 已配置的模型服务商 ===\033[0m")
        print()

        if not saved:
            print("  (暂无已保存的服务商配置)")
            print()
            print("  可用内置服务商:")
            for pname in FREE_PROVIDERS:
                default_model = FREE_PROVIDERS[pname].get("模型名称", "")
                print(f"    \033[33m{pname}\033[0m  \033[90m({default_model})\033[0m")
            print()
            print("  使用方式: drifox chat -p \"服务商名称\" -z \"你好\"")
            print()
            return

        for cid, info in sorted(saved.items()):
            pname = info.get("provider_name", cid)
            mname = info.get("模型名称", info.get("model", ""))
            url = info.get("API_URL", "")
            has_key = bool(info.get("API_KEY", ""))
            display_name = info.get("name", "") or pname

            indicator = "\033[32m▶\033[0m" if cid == selected else " "
            key_status = "\033[32m✓\033[0m" if has_key else "\033[31m未设置\033[0m"

            print(f"  {indicator} \033[1m{display_name}\033[0m")
            print(f"        模型: {mname}")
            print(f"        URL:  {url}")
            print(f"        Key:  {key_status}")
            if cid == selected:
                print("        (\033[32m当前选中\033[0m)")
            print()

        print(f"共 {len(saved)} 个服务商配置")
        print()
        print("使用方式:")
        print("  drifox chat -p \"服务商名关键词\" -z \"你好\"    # 指定服务商")
        print("  drifox chat -m \"模型名\" -z \"你好\"            # 指定模型")
        print("  drifox chat -p deepseek -m deepseek-chat -z \"你好\"")
        print()

    except Exception as e:
        print(f"获取服务商列表失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
