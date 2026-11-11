# -*- coding: utf-8 -*-
"""/sample_asr 命令执行体 —— 演示「经能力池按 capability_type 取配置 → 调用」全链路。

流程：provider.list 过滤 capability_type=speech_recognition 的实例 → 取首个实例 id →
model.config_for(id, capability_type="speech_recognition") 拿完整配置 →
httpx POST {API_URL}/audio/transcriptions（multipart：file + model）→
ui.notify 回显识别文本。

演示插件，非生产质量：单次请求（后台线程执行避免卡界面）、无重试、不做格式转换；
API_KEY 永不落日志/通知。
"""

import os
import threading
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from loguru import logger

COMMAND_NAME = "sample_asr"
COMMAND_DESCRIPTION = "样例·语音识别：经能力池取 speech_recognition 类型服务商配置，调用 OpenAI 兼容 /audio/transcriptions"
PLUGIN_NAME = "sample-asr"

# 本命令消费的能力类型（ProviderDef.capability_type，约定全小写）
CAPABILITY_TYPE = "speech_recognition"

_ENDPOINT = "/audio/transcriptions"
_TIMEOUT_S = 60
_NOTIFY_MS = 10000
_MAX_ECHO = 600  # 成功回显截断长度（全文落日志）
_MAX_ERR_BODY = 200  # 失败回显的响应原文截断长度
_MIME = "application/octet-stream"  # 厂商按文件名/内容自行嗅探，演示用通用 MIME


def _context() -> Tuple[Optional[Any], Optional[Any]]:
    """取当前活跃窗口与能力池（无窗口返回 (None, None)）。"""
    try:
        from app.plugins.capabilities.resolve import active_main_widget, pool_for

        mw = active_main_widget()
        if mw is None:
            return None, None
        return mw, pool_for(PLUGIN_NAME, surface="ui", host=mw)
    except Exception as e:  # noqa: BLE001 - 命令链异常不打断输入
        logger.warning(f"[{PLUGIN_NAME}] /{COMMAND_NAME} 上下文解析失败: {e}")
        return None, None


def _notify(pool: Any, title: str, message: str, kind: str = "success") -> None:
    """经 ui.notify 回显（内部已处理跨线程投递）；无池时落日志兜底，不丢结果。"""
    message = " ".join(str(message).split())
    if pool is None or "ui.notify" not in pool:
        logger.info(f"[{PLUGIN_NAME}] {title}: {message}")
        return
    try:
        pool["ui.notify"](title, message, kind, _NOTIFY_MS)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[{PLUGIN_NAME}] 通知失败: {e} | {title}: {message}")


def _safe_base(url: str) -> str:
    """脱敏 base_url：只保留 scheme://host（API_KEY 永不参与任何回显）。"""
    try:
        parts = urlparse(url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    except Exception:  # noqa: BLE001
        pass
    return "<无效 URL>"


def _pick_instance(pool: Any) -> Optional[dict]:
    """provider.list 过滤 capability_type 匹配的首个实例。"""
    entries = pool["provider.list"]() or []
    for item in entries:
        if isinstance(item, dict) and str(item.get("capability_type") or "") == CAPABILITY_TYPE:
            return item
    return None


def _resolve_config(pool: Any) -> Tuple[Optional[dict], str]:
    """取首个匹配实例的完整配置；失败返回 (None, 用户可读原因)。"""
    instance = _pick_instance(pool)
    if instance is None:
        return None, f"未配置该类型服务商（capability_type={CAPABILITY_TYPE}）"
    instance_id = str(instance.get("id") or "")
    config = pool["model.config_for"](instance_id, capability_type=CAPABILITY_TYPE)
    if not isinstance(config, dict) or not config:
        return None, f"无法解析服务商实例 {instance_id!r} 的完整配置"
    if not str(config.get("API_URL") or "").strip():
        return None, f"服务商实例 {instance_id!r} 缺少 API_URL"
    if not str(config.get("API_KEY") or "").strip():
        return None, f"服务商实例 {instance_id!r} 缺少 API_KEY"
    if not str(config.get("模型名称") or "").strip():
        return None, f"服务商实例 {instance_id!r} 缺少模型名称"
    return config, ""


def handle_command(args: str) -> None:
    """function 处理器（签名与 FunctionCommandHandlers 一致）：args=音频文件路径。"""
    audio_path = os.path.expanduser((args or "").strip().strip("\"'"))
    mw, pool = _context()
    if mw is None:
        logger.warning(f"[{PLUGIN_NAME}] /{COMMAND_NAME} 无活跃窗口，跳过")
        return
    if pool is None:
        logger.warning(f"[{PLUGIN_NAME}] 能力池构建失败，无法执行 /{COMMAND_NAME}")
        return

    if not audio_path or not os.path.isfile(audio_path):
        _notify(pool, "音频文件不存在", f"用法：/sample_asr <音频文件路径>（收到: {audio_path or '<空>'}）", "warning")
        return
    if "provider.list" not in pool or "model.config_for" not in pool:
        _notify(pool, "能力未授权", "manifest 需声明 provider.list / model.config_for", "warning")
        return

    config, reason = _resolve_config(pool)
    if config is None:
        _notify(pool, "无法调用", reason, "warning")
        return

    api_url = str(config.get("API_URL") or "").strip().rstrip("/")
    api_key = str(config.get("API_KEY") or "").strip()
    model_name = str(config.get("模型名称") or "").strip()

    # HTTP 请求放后台线程（命令 handler 在主线程，同步等 60s 会卡界面）；
    # 结果经 ui.notify 投递主线程回显（ui.notify 内部已做主线程投递）。
    thread = threading.Thread(
        target=_run_request, args=(pool, audio_path, api_url, api_key, model_name), daemon=True
    )
    thread.start()


def _run_request(pool: Any, audio_path: str, api_url: str, api_key: str, model_name: str) -> None:
    """后台线程：multipart 上传音频文件，识别文本经 ui.notify 回显。"""
    import httpx

    url = f"{api_url}{_ENDPOINT}"
    try:
        with open(audio_path, "rb") as fh:
            file_bytes = fh.read()
    except OSError as e:
        _notify(pool, "读取音频失败", f"{audio_path}: {e}", "error")
        return

    files = {"file": (os.path.basename(audio_path), file_bytes, _MIME)}
    data = {"model": model_name}
    try:
        resp = httpx.post(
            url, headers={"Authorization": f"Bearer {api_key}"}, files=files, data=data, timeout=_TIMEOUT_S
        )
    except Exception as e:  # noqa: BLE001
        _notify(pool, "请求失败", f"{_safe_base(url)} / {model_name}: {e}", "error")
        return

    if resp.status_code != 200:
        _notify(pool, f"HTTP {resp.status_code}", f"{_safe_base(url)} / {model_name}: {resp.text[:_MAX_ERR_BODY]}", "error")
        return

    try:
        text = str(resp.json().get("text") or "")
    except Exception as e:  # noqa: BLE001
        _notify(pool, "响应解析失败", f"{_safe_base(url)} / {model_name}: {resp.text[:_MAX_ERR_BODY]} ({e})", "error")
        return

    logger.info(f"[{PLUGIN_NAME}] /{COMMAND_NAME} 识别全文:\n{text}")
    shown = text if len(text) <= _MAX_ECHO else text[:_MAX_ECHO] + "…"
    _notify(pool, "样例·语音识别 调用成功", shown or "<空识别结果>")
