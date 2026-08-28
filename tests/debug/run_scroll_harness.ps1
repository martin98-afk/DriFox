# -*- coding: utf-8 -*-
# 批量跑全部滚动 harness 场景（每场景独立进程，规避 QtWebEngine 连续 runJavaScript 崩溃）
$names = @(
    "body_bottom_follows",
    "body_middle_untouched",
    "body_return_bottom_resumes",
    "body_far_from_bottom_untouched",
    "dock_cp_bottom_follows",
    "dock_cp_middle_untouched",
    "dock_cp_middle_bodyonly_no_touch",
    "tool_bottom_follows",
    "tool_middle_untouched",
    "nearbottom_edge_cases"
)
$failed = @()
foreach ($n in $names) {
    $out = & uv run python tests/debug/scroll_harness.py --only=$n 2>$null
    $code = $LASTEXITCODE
    $line = ($out | Where-Object { $_ -match "^(PASS|FAIL|ERROR)" }) -join "; "
    Write-Output "[$n] exit=$code $line"
    if ($code -ne 0) { $failed += $n }
}
Write-Output "===="
if ($failed.Count -eq 0) { Write-Output "ALL PASS" } else { Write-Output ("FAILED: " + ($failed -join ", ")) }
