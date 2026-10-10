# -*- coding: utf-8 -*-
"""回归测试：打字机 DOM 替换闸门必须按序执行全部挂起任务（不得覆盖丢弃）

背景（用户反馈）
----------------
流式生成过程中消息卡片**偶发丢段**：某段正文先随打字机显示出来，随后某一轮
渲染把它抹掉且不再恢复（最终态又正常，因为终渲染整页重建）。

根因（2026-10-10 定位）
----------------------
`window._twGate`（v40 引入的打字机 DOM 替换闸门）原为**单槽**设计：

    st._gateFn = fn;   // 单槽覆盖：新任务含更多文本，旧任务语义被包含

这条「新任务语义包含旧任务」的注释依据是 `updateTailHtml`——它每轮传入的是
**当前全文尾部**的行内渲染，单调增长，后者确实包含前者。

但闸门同时被 `updateContentAppend(newHtml, tailHtml)` 使用，而它的 `newHtml`
**只含本轮新闭合段**（Python 侧每轮把已消费段推进进 `_stable_md_len`，后续轮次
不再包含早前段）。追加语义的多次调用互不包含 → 单槽覆盖即永久丢失那一段的
格式化 HTML；而紧随执行的替换又会 `remove` 全部 `[data-incremental="true"]`
节点（其中正有承载该段纯文本的节点）→ 该段从屏上消失，且此后无人补回。

实测（tests/debug/stream_content_loss_live_probe.py，真实 QWebEngineView，
40 段 × 30ms 喂入）：闸门覆盖 3 次 → 流式态缺 7 个段落 token；
`--gate-off` 对照组覆盖 0 次、缺失 0。

修复
----
`_gateFn`（单槽）→ `_gateQueue`（FIFO 队列），`_twDrainGate` 按序执行全部挂起
任务，逐任务独立 try（单个失败不阻断其余）。

本测试在 **node 中真实执行 `_TYPEWRITER_JS` 资产**（该资产不依赖 DOM，仅用到
performance / setInterval / rAF，可用桩替代），断言队列语义而非源码字符串，
防止将来再退回"覆盖面丢弃"实现。

运行：
    python -m pytest tests/widgets/test_typewriter_gate_queue.py -v
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_render_core.py"
_ASSET_NAME = "_TYPEWRITER_JS"

# node 侧驱动脚本：加载被测 JS 资产，跑三组场景并输出 JSON
_DRIVER = r"""
const fs = require('fs');
const assetPath = process.argv[2];
const src = fs.readFileSync(assetPath, 'utf8');

global.window = global;
global.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};
global.performance = { now: () => 0 };
global.document = { body: null };

eval(src);

const out = {};
const tw = window._tw;

// 场景 1：缓冲积压 → 三轮挂起 → 放行后必须全部执行（原实现只执行最后一轮）
tw.buf = 'XXXXX';
const applied = [];
window._twGate(() => applied.push('A'));
window._twGate(() => applied.push('B'));
window._twGate(() => applied.push('C'));
out.queuedDepth = tw._gateQueue.length;
out.appliedBeforeDrain = applied.slice();
tw.buf = '';
window._twDrainGate();
out.appliedAfterDrain = applied.slice();
out.queueEmptyAfterDrain = tw._gateQueue.length === 0;

// 场景 2：单个任务抛错不得阻断其余任务，且 _force 必须复位
tw.buf = 'YYYYY';
const applied2 = [];
window._twGate(() => { throw new Error('boom'); });
window._twGate(() => applied2.push('B2'));
tw.buf = '';
window._twDrainGate();
out.appliedAfterThrow = applied2.slice();
out.forceResetAfterThrow = tw._force === false;

// 场景 3：缓冲在水位内 → 不入队，立即执行（闸门原语义保持）
tw.buf = 'ab';
const applied3 = [];
const held = window._twGate(() => applied3.push('C3'));
out.gateReturnedFalseInWater = held === false;
out.appliedInWater = applied3.slice();
out.queuedInWater = tw._gateQueue.length;

// 场景 4：_twReset 必须清空队列（整体替换场景下残留任务会回灌旧 HTML）
tw.buf = 'ZZZZZ';
window._twGate(() => applied.push('D'));
out.queuedBeforeReset = tw._gateQueue.length;
window._twReset();
out.queuedAfterReset = tw._gateQueue.length;

console.log(JSON.stringify(out));
"""


def _extract_asset() -> str:
    text = _SRC.read_text(encoding="utf-8")
    start = text.find(f'{_ASSET_NAME} = """')
    assert start != -1, f"未找到 {_ASSET_NAME} 资产定义"
    body_start = start + len(f'{_ASSET_NAME} = """')
    end = text.find('"""', body_start)
    assert end != -1, f"{_ASSET_NAME} 资产未闭合"
    return text[body_start:end]


@pytest.fixture(scope="module")
def gate_result(tmp_path_factory) -> dict:
    """在 node 中真实执行 _TYPEWRITER_JS，返回各场景观测结果。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("未找到 node，跳过闸门 JS 行为测试")

    asset = _extract_asset()
    # 资产内含注释中的 ``{{`` 转义仅为 Python f-string 用途；JS 本体无 format 占位。
    # 若将来引入占位符，这里会以语法错误暴露，属预期（需改用渲染后产物测试）。
    tmp = tmp_path_factory.mktemp("typewriter_gate")
    (tmp / "asset.js").write_text(asset, encoding="utf-8")
    (tmp / "driver.js").write_text(_DRIVER, encoding="utf-8")

    proc = subprocess.run(
        [node, str(tmp / "driver.js"), str(tmp / "asset.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if proc.returncode != 0:
        pytest.fail(f"node 执行闸门资产失败：\nstdout={proc.stdout}\nstderr={proc.stderr}")
    assert proc.stdout.strip(), f"node 无输出：stderr={proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestGateQueueSemantics:
    """闸门必须按序执行全部挂起任务（回归：单槽覆盖导致流式丢段）"""

    def test_all_queued_tasks_execute_on_drain(self, gate_result: dict):
        """三轮挂起 → 放行后三轮都必须执行（原实现仅最后一轮生效，前两轮永久丢失）"""
        assert gate_result["queuedDepth"] == 3, "三次调用应全部入队（不得覆盖）"
        assert gate_result["appliedBeforeDrain"] == [], "挂起期间不应执行"
        assert gate_result["appliedAfterDrain"] == ["A", "B", "C"], (
            "放行后必须按序执行全部挂起任务；只执行最后一个 = 先前各段格式化 HTML 永久丢失（丢段 bug）"
        )
        assert gate_result["queueEmptyAfterDrain"] is True

    def test_one_failing_task_does_not_block_others(self, gate_result: dict):
        """单个任务抛错不得阻断其余任务（否则后续段同样永久丢失）"""
        assert gate_result["appliedAfterThrow"] == ["B2"], "抛错任务之后的挂起任务仍须执行"
        assert gate_result["forceResetAfterThrow"] is True, "_force 标记必须复位（否则误放行下一次）"

    def test_in_water_executes_immediately(self, gate_result: dict):
        """缓冲在水位内 → 返回 false（不予挂起），由调用方同步执行，不入队"""
        assert gate_result["gateReturnedFalseInWater"] is True, "水位内必须返回 false 让调用方立即执行"
        assert gate_result["queuedInWater"] == 0, "水位内不得入队（否则替换被无谓延后）"
        assert gate_result["appliedInWater"] == [], (
            "闸门只负责裁决不放行，实际执行由调用方负责（水位内 lambda 不应由闸门代跑）"
        )

    def test_reset_clears_queue(self, gate_result: dict):
        """_twReset 必须清空队列（整体替换后残留任务回灌旧 HTML）"""
        assert gate_result["queuedBeforeReset"] == 1
        assert gate_result["queuedAfterReset"] == 0


@pytest.fixture(scope="module")
def gate_src_text() -> str:
    return _SRC.read_text(encoding="utf-8")


class TestGateWiringSource:
    """源码级接线：单槽符号应已绝迹，队列符号齐备。"""

    def test_no_single_slot_left(self, gate_src_text: str):
        """单槽实现必须彻底移除（残留 = 又有一条路径在丢弃任务）

        只检代码体（去注释）：注释里会出现旧符号名用于说明根因。
        """
        code_only = "\n".join(l for l in gate_src_text.splitlines() if not l.lstrip().startswith("#"))
        assert "_gateFn" not in code_only, "单槽挂起字段应已由 _gateQueue 取代"

    def test_queue_api_present(self, gate_src_text: str):
        for token in ("_gateQueue: []", "st._gateQueue.push(fn)", "var queue = st._gateQueue;"):
            assert token in gate_src_text, f"缺少队列接线：{token}"

    def test_skeleton_version_bumped(self, gate_src_text: str):
        """骨架 JS 行为变更必须递增缓存版本（否则旧骨架仍走单槽覆盖）"""
        start = gate_src_text.find("_SKELETON_CACHE_VERSION = ")
        assert start != -1
        line = gate_src_text[start : gate_src_text.find("\n", start)]
        version = int(line.split("=")[1].strip())
        assert version >= 42, f"闸门队列改造后 _SKELETON_CACHE_VERSION 必须 >=42，实际 {version}"
