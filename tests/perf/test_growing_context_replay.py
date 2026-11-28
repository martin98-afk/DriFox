"""回放无 usage 时的本地计数；热文本缓存，保留每轮列表增长与旧末条校验。"""
import statistics
import time

import pytest

from app.core.infra import token_estimator as te


@pytest.mark.perf
@pytest.mark.parametrize('tool_count', [0, 35])
def test_growing_context_replay(tool_count):
    data = [{'role': 'user', 'content': str(i) + '上下文' * (i % 19 + 1)} for i in range(1600)]
    extra = [{'role': 'assistant', 'content': '补充推理' * i} for i in range(1, 61)]
    tools = [{'type': 'function', 'function': {'name': f'read_{i}', 'description': '读取本地文件',
              'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}
             for i in range(tool_count)]
    expected = sum(te.per_message_tokens(msg) for msg in data + extra) + te.count_tools_tokens(tools)
    timings = []
    for _ in range(7):
        msgs = data.copy()
        te._get_prefix_cursors().clear()
        te._get_token_cache().clear()
        start = time.perf_counter()
        result = te.count_messages_tokens(msgs, tools=tools)
        for msg in extra:
            msgs.append(msg)
            result = te.count_messages_tokens(msgs, tools=tools)
        timings.append((time.perf_counter() - start) * 1000)
        assert result == expected
    print(f'1600 条历史追加 60 轮，{tool_count} 个工具：中位数 {statistics.median(timings):.2f} ms，范围 {min(timings):.2f}–{max(timings):.2f} ms')
