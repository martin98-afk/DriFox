# -*- coding: utf-8 -*-
"""标准 logging → loguru 接管链路测试（需真实文件 IO 与 sink，独立于纯函数测试）。

背景（2026-10 实机定位）：本模块只给 loguru 配 sink，标准 logging 是独立体系，
第三方库（lark_oapi / openai / httpx / aiohttp）与用 ``logging.getLogger`` 的插件
输出**一条都不落盘**。实证代价：飞书 WS 建连失败时 lark SDK 的
``connect failed, err: [SSL: CERTIFICATE_VERIFY_FAILED]`` 完全不可见，排查只能盲猜。

本测试用独立临时目录 + 独立 loguru sink，用例结束全部摘除，不污染全局状态。
"""
import logging
import time
from pathlib import Path

import pytest
from loguru import logger as _lz

from app.core.infra.logging_setup import _install_stdlib_logging_bridge

_READ_DELAY = 0.25  # loguru 异步写入落盘余量


@pytest.fixture
def log_dir(tmp_path):
    """独立日志目录 + 只服务本用例的 loguru sink；用例结束摘除。"""
    sink_id = _lz.add(tmp_path / "all.log", level="DEBUG", format="{message}", enqueue=False)
    yield tmp_path
    _lz.remove(sink_id)


@pytest.fixture(autouse=True)
def _clean_bridge():
    """还原 root logger，避免用例间互相污染（bridge 是全局一次性安装）。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    had_flag = getattr(root, "_drifox_loguru_bridge", False)
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    if had_flag:
        root._drifox_loguru_bridge = True
    elif hasattr(root, "_drifox_loguru_bridge"):
        delattr(root, "_drifox_loguru_bridge")


def _read(log_dir: Path, name: str = "all.log") -> str:
    time.sleep(_READ_DELAY)
    p = log_dir / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_stdlib_record_reaches_loguru(log_dir):
    """核心：标准 logging 的记录必须落进 loguru sink。"""
    _install_stdlib_logging_bridge()
    logging.getLogger("Lark").error("connect failed, err: [SSL: CERTIFICATE_VERIFY_FAILED]")
    assert "connect failed" in _read(log_dir)


def test_module_name_preserved(log_dir):
    """原 logger 名保留在 loguru 记录里（路由前缀匹配依赖它）。"""
    _install_stdlib_logging_bridge()
    seen: list[str] = []
    sink_id = _lz.add(lambda m: seen.append(m.record["name"]), level="DEBUG")
    try:
        logging.getLogger("Lark").warning("w")
        assert "Lark" in seen
    finally:
        _lz.remove(sink_id)


def test_level_preserved(log_dir):
    """级别语义保留：WARNING 不该被降成 INFO，ERROR 同理。"""
    _install_stdlib_logging_bridge()
    levels: list[str] = []
    sink_id = _lz.add(lambda m: levels.append(m.record["level"].name), level="DEBUG")
    try:
        lg = logging.getLogger("ThirdParty")
        lg.warning("w")
        lg.error("e")
        assert "WARNING" in levels
        assert "ERROR" in levels
    finally:
        _lz.remove(sink_id)


def test_default_handler_replaced_no_double_output(log_dir):
    """接管后 root 只剩转发器：默认 StreamHandler 被摘除，避免重复输出。"""
    logging.getLogger().addHandler(logging.StreamHandler())
    _install_stdlib_logging_bridge()
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert type(handlers[0]).__name__ == "_LoguruForwarder"


def test_bridge_is_idempotent(log_dir):
    """幂等：重复安装不叠加转发器（否则同一条日志会重复落盘）。"""
    _install_stdlib_logging_bridge()
    first = logging.getLogger().handlers
    _install_stdlib_logging_bridge()
    assert logging.getLogger().handlers == first
    # 用 ERROR（root WARNING 阈值之上）确保能落盘，验证不重复
    logging.getLogger("Lark").error("only-once")
    assert _read(log_dir).count("only-once") == 1


def test_exception_info_preserved(log_dir):
    """异常栈经 opt(exception=...) 传递，不被吞掉。"""
    _install_stdlib_logging_bridge()
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("Lark").exception("connect failed")
    text = _read(log_dir)
    assert "connect failed" in text
    # 异常详情随记录透传（Traceback 或异常类型）
    assert "ValueError" in text or "Traceback" in text or "boom" in text


def test_bridge_survives_emit_exception(log_dir, monkeypatch):
    """转发器内部异常不得影响业务调用方（日志链路自身绝不可抛）。"""
    _install_stdlib_logging_bridge()

    def _boom(*a, **kw):
        raise RuntimeError("sink broken")

    monkeypatch.setattr(_lz, "patch", _boom)
    # 本调用若向上抛异常，用例即失败（转发器内部须 catch 并静默）
    logging.getLogger("Lark").error("should not raise")


def test_noise_libs_not_unlocked(log_dir):
    """设计约束：root 保持 WARNING，未设级别的噪音库不得被放行 DEBUG。

    背景：若 root 设 0（NOTSET），urllib3/httpcore/httpx/asyncio 等未显式设级别
    的库其 getEffectiveLevel() 会继承成 NOTSET → DEBUG 全放行，all.log 被连接池
    噪音淹没（实测 100 条 DEBUG 全量落盘）。root 保持 WARNING 维持库原生默认。
    """
    _install_stdlib_logging_bridge()
    noise = logging.getLogger("urllib3.connectionpool")
    assert not noise.isEnabledFor(logging.DEBUG), "噪音库的 DEBUG 不应被放行"

    noise.debug("this must not appear")
    assert "this must not appear" not in _read(log_dir)


def test_explicit_level_still_honored(log_dir):
    """需要 DEBUG 的库/插件自己 setLevel 后照常透传（收目标正是这类）。"""
    _install_stdlib_logging_bridge()
    lark = logging.getLogger("Lark")
    lark.setLevel(logging.DEBUG)  # 模拟 SDK 行为（core/log.py 与 client 均会设）
    lark.debug("connect diag detail")
    assert "connect diag detail" in _read(log_dir)
