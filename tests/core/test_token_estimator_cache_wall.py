# -*- coding: utf-8 -*-
"""回归测试：估算缓存容量墙解除（T2-A，任务 B 实测拐点的防回退）。

背景：_estimate_tokens_cached lru_cache(maxsize=2048) 在 msgs≈2135（长会话
N=800 档）触发 LRU 抖动全 miss——单次全量估算 1.4→91.5ms（65 倍劣化），
pipeline 单次投影被放大到 500ms+。实测见 2026-11-18 任务 B 四档 bench。

防回退双层：
1) 白盒：maxsize==8192（容量调小即 fail，改前必被 CI 拦截）；
2) 行为级：2100 条消息（旧 2048 容量必溢出的规模）两遍全量估算，
   第二遍应全部命中缓存。时间上界给宽松余量（快估算 45µs/条 × 2100 ≈
   95ms 全裸重算；命中路径应 <20ms），超上界=缓存未生效（容量墙回归或
   键设计劣化）。快估算本身 ~100ms 级全量兜底，任何机器上 <20ms 的命中
   断言都不会因慢机误报，无需 skip 条件。
"""

import time

from app.core.infra.token_estimator import _estimate_tokens_cached, count_messages_tokens

def test_estimate_cache_maxsize_is_8192():
    """白盒：容量必须 ≥8192，调回 2048 及以下即容量墙回归。"""
    info = _estimate_tokens_cached.cache_info()
    assert info.maxsize == 8192, f"估算缓存容量被改回 {info.maxsize}，N≥800 档将重现 65 倍劣化"

def test_2100_messages_second_pass_hits_cache():
    """行为级：2100 条（旧容量必溢出）第二遍全量估算须命中缓存（<20ms）。"""
    msgs = [
        {"role": "user", "content": f"问题{i}：" + "分析这段代码的性能瓶颈并给出优化建议。" * 6}
        for i in range(1050)
    ]
    msgs += [
        {"role": "assistant", "content": f"回答{i}：" + "这里有几个优化点需要逐一说明，先看热点函数再看内存分配。" * 12}
        for i in range(1050)
    ]
    # 第一遍：填缓存（旧 2048 容量下后半段已被逐出，本断言在 8192 下应全留）
    count_messages_tokens(list(msgs))
    probe = list(msgs)
    t0 = time.perf_counter()
    count_messages_tokens(probe)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 20.0, (
        f"2100 条消息第二遍全量估算耗时 {elapsed_ms:.1f}ms ≥20ms：估算缓存未生效"
        f"（容量墙回归或键设计劣化），cache_info={_estimate_tokens_cached.cache_info()}"
    )
