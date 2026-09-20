# -*- coding: utf-8 -*-
"""Job 配额：LimitFlags 组装 + 限额失败降级 KILL_ON_JOB_CLOSE"""
import sys

import pytest

from app.tools import process_job as pj


def _expected_flags(memory_mb, active, cpu_ms):
    flags = pj.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if memory_mb > 0:
        flags |= pj.JOB_OBJECT_LIMIT_PROCESS_MEMORY
    if active > 0:
        flags |= pj.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    if cpu_ms > 0:
        flags |= pj.JOB_OBJECT_LIMIT_JOB_TIME
    return flags


def test_flag_constants_defined():
    assert pj.JOB_OBJECT_LIMIT_PROCESS_MEMORY == 0x00000100
    assert pj.JOB_OBJECT_LIMIT_ACTIVE_PROCESS == 0x00000008
    assert pj.JOB_OBJECT_LIMIT_JOB_TIME == 0x00000004


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_init_with_limits_sets_flags():
    job = pj.ProcessJob(memory_mb=512, active_process=8, cpu_time_ms=0)
    try:
        assert job._last_limit_flags == _expected_flags(512, 8, 0)
        assert job._info.BasicLimitInformation.ActiveProcessLimit == 8
        assert job._info.ProcessMemoryLimit == 512 * 1024 * 1024
    finally:
        job.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_init_without_limits_keeps_kill_on_close_only():
    job = pj.ProcessJob()
    try:
        assert job._last_limit_flags == pj.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    finally:
        job.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_cpu_time_ms_converted_to_100ns():
    job = pj.ProcessJob(cpu_time_ms=1000)
    try:
        assert job._info.BasicLimitInformation.PerJobUserTimeLimit == 10_000_000
    finally:
        job.close()


def test_signature_defaults_backward_compatible():
    # 不传限额参数时签名兼容（现有调用点零改动）
    import inspect

    sig = inspect.signature(pj.ProcessJob.__init__)
    assert sig.parameters["memory_mb"].default == 0
    assert sig.parameters["active_process"].default == 0
    assert sig.parameters["cpu_time_ms"].default == 0
