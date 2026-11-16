# -*- coding: utf-8 -*-
"""app.core.infra.telemetry — 进程内观测埋点的启动挂载集合（T11 观测基建）。

当前成员：
- ``startup``：主程序启动期的埋点挂载（thread-dumper / gc-gen2 统计）。

约定：埋点实现各自携带 env 门控与异常吞咽，挂载失败一律静默，
不得反噬主功能；perf_jsonl 本体亦在本子包（``telemetry.perf_jsonl``），
落点与格式见其模块注释。
"""

from app.core.infra.telemetry.startup import start_gc_tracer, start_thread_dumper

__all__ = ["start_gc_tracer", "start_thread_dumper"]
