"""节奏曲线（tempo map）估计：从 DTW 对齐结果提取局部 BPM。

漏拍 / 插入拍会让相邻匹配点之间出现“跨多拍”或“拍内异常靠近”的演奏间隔，
直接换算 BPM 会得到一半 / 两倍的异常值。处理流程：

1. 对 matched_beats 的每个相邻对，按**参考拍跨度**换算原始局部 BPM
   （``60 × 参考拍跨度 / 演奏时间差``），正常的漏 / 插间隔因此自动归一化；
2. 用以每个间隔为中心、半径约 ``window_beats // 2`` 的滑动**中位数**窗口得到
   稳健 BPM；原始 BPM 超出 ``[min_bpm, max_bpm]`` 或相对局部中位数偏离超过
   ``_OUTLIER_TOLERANCE`` 的间隔判定为漏拍 / 插入拍残留异常，记入忽略索引；
3. 把剩余间隔按稳健 BPM 压缩：相对差不超过 ``_STABLE_TOLERANCE`` 的连续间隔
   合并为一个 tempo event（被忽略的间隔不切断事件，覆盖拍数按参考拍跨度计）。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

# 原始间隔 BPM 相对局部中位数偏离该比例以上即视为异常（半速/倍速为 ±50%/100%）
_OUTLIER_TOLERANCE = 0.25
# 相邻间隔稳健 BPM 相对差不超过该比例则并入同一 tempo event
_STABLE_TOLERANCE = 0.02
_BPM_DECIMALS = 2
_TIME_DECIMALS = 6


class TempoMapError(ValueError):
    """matched_beats 不足以估计节奏曲线。"""


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def build_tempo_map(
    matched_beats: Sequence[dict[str, Any]],
    window_beats: int = 5,
    min_bpm: float = 40.0,
    max_bpm: float = 300.0,
) -> dict[str, Any]:
    """从匹配拍序列构造节奏曲线。

    matched_beats 中每项至少含 ``ref_index`` 与 ``perf_time``（与对齐结果的
    matched_beats 元素结构一致）。返回 ``events`` / ``ignored_ref_indices`` /
    ``matched_beat_count`` / ``parameters``。
    """
    beats = sorted(matched_beats, key=lambda b: b["ref_index"])
    if len(beats) < 2:
        raise TempoMapError("至少需要 2 个匹配拍才能估计节奏曲线")

    # ---- 1. 相邻匹配间隔的原始局部 BPM（按参考拍跨度归一化）----
    gap_starts: list[int] = []
    raw_bpms: list[float] = []
    for prev, cur in zip(beats, beats[1:]):
        ref_span = cur["ref_index"] - prev["ref_index"]
        perf_dt = cur["perf_time"] - prev["perf_time"]
        gap_starts.append(prev["ref_index"])
        if ref_span <= 0 or perf_dt <= 0:
            # 非正时间差（异常数据）：置 inf，交给范围 / 中位数检查忽略
            raw_bpms.append(math.inf)
        else:
            raw_bpms.append(60.0 * ref_span / perf_dt)

    # ---- 2. 滑动中位数窗口 + 异常间隔抑制 ----
    radius = max(0, (window_beats - 1) // 2)
    robust_bpms: list[float | None] = []
    for i in range(len(raw_bpms)):
        lo = max(0, i - radius)
        hi = min(len(raw_bpms), i + radius + 1)
        window = [raw_bpms[j] for j in range(lo, hi) if math.isfinite(raw_bpms[j])]
        robust_bpms.append(_median(window) if window else None)

    valid_flags: list[bool] = []
    ignored: list[int] = []
    for i, bpm in enumerate(raw_bpms):
        in_range = math.isfinite(bpm) and min_bpm <= bpm <= max_bpm
        med = robust_bpms[i]
        near_median = (
            med is not None
            and math.isfinite(bpm)
            and abs(bpm - med) <= _OUTLIER_TOLERANCE * med
        )
        is_valid = in_range and near_median
        valid_flags.append(is_valid)
        if not is_valid:
            ignored.append(gap_starts[i])

    # ---- 3. 稳定区间压缩为 tempo events ----
    events: list[dict[str, Any]] = []
    run_gap_idx: list[int] = []
    run_robust: list[float] = []

    def flush_run() -> None:
        if not run_gap_idx:
            return
        first_gap, last_gap = run_gap_idx[0], run_gap_idx[-1]
        start_beat, end_beat = beats[first_gap], beats[last_gap + 1]
        events.append({
            "ref_start_index": start_beat["ref_index"],
            "ref_end_index": end_beat["ref_index"],
            "start_time": round(start_beat["perf_time"], _TIME_DECIMALS),
            "ref_time": round(start_beat["ref_time"], _TIME_DECIMALS),
            "bpm": round(_median(run_robust), _BPM_DECIMALS),
            # 覆盖拍数按参考拍跨度计：被忽略间隔跨过的漏拍也包含在内；
            # 相邻事件共享边界拍（前一事件结束拍 = 后一事件起拍）
            "beat_count": end_beat["ref_index"] - start_beat["ref_index"],
            "gap_count": len(run_gap_idx),
        })

    for i, is_valid in enumerate(valid_flags):
        if not is_valid:
            # 异常间隔不参与 BPM，也不切断事件，允许两侧同速区间合并
            continue
        bpm = robust_bpms[i]
        assert bpm is not None
        if run_robust:
            representative = _median(run_robust)
            if abs(bpm - representative) > _STABLE_TOLERANCE * representative:
                flush_run()
                run_gap_idx, run_robust = [], []
        run_gap_idx.append(i)
        run_robust.append(bpm)
    flush_run()

    return {
        "parameters": {
            "window_beats": window_beats,
            "min_bpm": min_bpm,
            "max_bpm": max_bpm,
            "outlier_tolerance": _OUTLIER_TOLERANCE,
            "stable_tolerance": _STABLE_TOLERANCE,
        },
        "matched_beat_count": len(beats),
        "events": events,
        "ignored_ref_indices": ignored,
    }
