"""粗估只能加速，不能悄悄改变上下文预算。"""
import random

import pytest

from app.core.infra import token_estimator as te


def _reference(text):
    n = len(text)
    if not n:
        return 0
    sampled = n > te._FAST_SAMPLE_THRESHOLD
    if sampled:
        w = te._FAST_SAMPLE_WINDOW
        starts = (i * (n - w) // 8 for i in range(9))
        sample = ''.join(text[start:start + w] for start in starts)
    else:
        sample = text
    count = sum('\u4e00' <= char <= '\u9fff' for char in sample)
    if sampled:
        count = int(n * (count / len(sample)))
    return max(1, int((count * 1.2 + (n - count) / 4.0) * (te._FAST_SAMPLE_SAFETY if sampled else 1.0)))


@pytest.mark.parametrize('unit', ['a', '中', 'a中', '😀éα', '\u4dff\u4e00\u9fff\ua000', '中文，print(x)\n'])
@pytest.mark.parametrize('n', [0, 1, 23, 51199, 51200, 51201, 250003])
def test_distribution_and_sample_threshold(unit, n):
    text = (unit * (n // len(unit) + 1))[:n]
    assert te._fast_estimate_tokens(text) == _reference(text)


def test_uneven_runs_with_unicode_edges():
    rng = random.Random(1138)
    for _ in range(30):
        text = ''.join(rng.choice(['x', '中', '😀', '\ud800', '𠀀', '﨑']) * rng.randrange(6000) for _ in range(24))
        assert te._fast_estimate_tokens(text) == _reference(text)
