"""带宽约束的动态时间规整（DTW）节拍对齐核心算法。

参考节拍网格 ref 与演奏起始时间 perf 两个单调序列在 Sakoe-Chiba
带状窗口内做对齐，显式建模三种事件：

* match    —— 参考拍 i 与演奏拍 j 一一对应，代价为时间偏差平方；
* skip     —— 漏拍（演奏缺失参考拍 i），代价为惩罚项；
* insert   —— 插入拍（多出来的演奏音 j），代价为惩罚项。

回溯后输出一一对应映射、漏拍 / 插入拍列表、连续偏差区间及统计指标。

锚点约束：调用方可提供若干 ``(参考拍索引, 演奏音索引)`` 锚点作为硬约束，
对齐路径必须以对角（匹配）步经过这些点。此时 DTW 被拆成相邻锚点之间的
若干独立子段（首尾补齐虚拟端点 ``(0,0)`` 与 ``(n,m)``），各子段在同一
Sakoe-Chiba 带内独立寻优后再合并为完整路径。
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from typing import Any, Sequence

# 回溯指针：1=对角(匹配) 2=水平(漏拍) 3=垂直(插入拍)
_PTR_MATCH = 1
_PTR_SKIP = 2
_PTR_INSERT = 3

# 锚点：(参考拍索引, 演奏音索引)，均为 0 基
Anchor = tuple[int, int]


class AlignmentError(ValueError):
    """输入数据无法在给定带宽 / 锚点约束下完成对齐。"""


@dataclass(frozen=True)
class AlignmentParams:
    """对齐参数。

    band_width: Sakoe-Chiba 带宽，允许 |i-j| > band_width 的路径被禁止，
        同时要求 |len(ref)-len(perf)| <= band_width，否则无法对齐。
    skip_penalty: 漏一拍的代价（秒的平方）；None 表示取
        0.5 * 中位参考拍间隔 的平方。
    insert_penalty: 插入一拍的代价（秒的平方）；None 时与漏拍相同。
    on_time_threshold: |偏差| 小于等于该秒数视为“准时”（仅用于分类）。
    """

    band_width: int = 3
    skip_penalty: float | None = None
    insert_penalty: float | None = None
    on_time_threshold: float = 0.03

    @classmethod
    def from_payload(cls, payload: Any | None) -> "AlignmentParams":
        if payload is None:
            return cls()
        return cls(
            band_width=int(payload.band_width),
            skip_penalty=payload.skip_penalty,
            insert_penalty=payload.insert_penalty,
            on_time_threshold=float(payload.on_time_threshold),
        )


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def _default_penalty(ref: Sequence[float]) -> float:
    """默认漏拍 / 插入代价：(0.5 × 中位参考拍间隔)^2。"""
    if len(ref) < 2:
        return 0.25
    gaps = [ref[i] - ref[i - 1] for i in range(1, len(ref))]
    half = 0.5 * _median(gaps)
    return half * half


def _classify(dev: float, threshold: float) -> str:
    if dev <= -threshold:
        return "early"  # 抢拍（演奏早于参考）
    if dev >= threshold:
        return "late"  # 拖拍（演奏晚于参考）
    return "on_time"  # 准时


def _as_anchor(value: Any, pos: int) -> Anchor:
    """把元组 / 映射 / Pydantic 模型等统一成 (ref_index, perf_index)。"""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        ri, pj = value
    elif hasattr(value, "ref_index") and hasattr(value, "perf_index"):
        ri, pj = value.ref_index, value.perf_index
    elif isinstance(value, dict) and "ref_index" in value and "perf_index" in value:
        ri, pj = value["ref_index"], value["perf_index"]
    else:
        raise AlignmentError(
            f"第 {pos + 1} 个锚点格式非法，需为 [参考拍索引, 演奏音索引]"
        )
    if (
        not isinstance(ri, int) or isinstance(ri, bool)
        or not isinstance(pj, int) or isinstance(pj, bool)
    ):
        raise AlignmentError(f"第 {pos + 1} 个锚点的索引必须为整数")
    return ri, pj


def validate_anchors(
    anchors: Sequence[Anchor], n: int, m: int, width: int
) -> None:
    """校验锚点：非负、不越界、严格递增且在带宽可达范围内。

    带宽为 ``|i − j| ≤ width`` 时，任意两个带内且严格递增的网格点之间都存在
    合法路径（沿对角步推进，多余的水平/垂直步可在任一端消化）；首锚点相对
    虚拟起点 (0,0)、末锚点相对虚拟终点 (n,m) 同理。因此可达性只需逐点检查
    锚点自身是否在带内（配合端点固定的 |n−m| ≤ width 预检）。
    """
    prev_ri, prev_pj = 0, 0
    for k, (ri, pj) in enumerate(anchors):
        if ri < 0 or pj < 0:
            raise AlignmentError(f"第 {k + 1} 个锚点 ({ri}, {pj}) 索引不能为负")
        if ri >= n or pj >= m:
            raise AlignmentError(
                f"第 {k + 1} 个锚点 ({ri}, {pj}) 越界："
                f"参考拍索引需在 [0, {n - 1}]、演奏音索引需在 [0, {m - 1}] 内"
            )
        if k > 0 and (ri <= prev_ri or pj <= prev_pj):
            raise AlignmentError(
                "锚点必须按参考拍索引与演奏音索引严格递增："
                f"第 {k} 个锚点 ({prev_ri}, {prev_pj}) 之后出现了 ({ri}, {pj})"
            )
        if abs(ri - pj) > width:
            raise AlignmentError(
                f"第 {k + 1} 个锚点 ({ri}, {pj}) 超出带宽 {width}："
                f"|参考索引−演奏索引|={abs(ri - pj)}，带宽内不可达，请增大 band_width"
            )
        prev_ri, prev_pj = ri, pj


def _dtw_segment(
    ref: Sequence[float],
    perf: Sequence[float],
    start: Anchor,
    end: Anchor,
    width: int,
    skip_cost: float,
    insert_cost: float,
    force_diagonal_end: bool,
) -> tuple[list[tuple[int, int]], float]:
    """在网格单元 ``start -> end`` 之间做一段带宽约束 DTW。

    使用局部网格 (a,b)，对应全局单元 (start_i+a, start_j+b)；带宽按全局
    坐标判定。``force_diagonal_end=True`` 时 end 必须由对角步进入，即
    end 锚点必须是一次匹配（硬约束）。

    返回不含 start 的全局单元路径（含 end）及该段代价。
    """
    si, sj = start
    ei, ej = end
    ni, mj = ei - si, ej - sj
    if ni == 0 and mj == 0:
        return [], 0.0

    size = (ni + 1) * (mj + 1)
    dp = array("d", [math.inf]) * size
    ptr = array("b", bytes(size))
    dp[0] = 0.0

    def idx(a: int, b: int) -> int:
        return a * (mj + 1) + b

    def in_band(gi: int, gj: int) -> bool:
        return -width <= gi - gj <= width

    offset = si - sj
    for a in range(0, ni + 1):
        # 全局条件 |(si-sj)+a−b| ≤ width  ⇒  a+offset−width ≤ b ≤ a+offset+width
        b_lo = max(0, a + offset - width)
        b_hi = min(mj, a + offset + width)
        for b in range(b_lo, b_hi + 1):
            if a == 0 and b == 0:
                continue
            best = math.inf
            best_ptr = 0

            # 锚点单元必须由对角步进入（锚点 = 强制匹配）
            is_anchor_cell = force_diagonal_end and a == ni and b == mj

            # 对角：ref[si+a-1] <-> perf[sj+b-1]（匹配）
            if a > 0 and b > 0 and in_band(si + a - 1, sj + b - 1):
                d = perf[sj + b - 1] - ref[si + a - 1]
                cand = dp[idx(a - 1, b - 1)] + d * d
                if cand < best:
                    best, best_ptr = cand, _PTR_MATCH
            # 水平：跳过参考拍（漏拍）；锚点单元禁止水平/垂直进入
            if not is_anchor_cell and a > 0 and in_band(si + a - 1, sj + b):
                cand = dp[idx(a - 1, b)] + skip_cost
                if cand < best:
                    best, best_ptr = cand, _PTR_SKIP
            # 垂直：插入演奏拍（插入拍）
            if not is_anchor_cell and b > 0 and in_band(si + a, sj + b - 1):
                cand = dp[idx(a, b - 1)] + insert_cost
                if cand < best:
                    best, best_ptr = cand, _PTR_INSERT

            if best_ptr:
                dp[idx(a, b)] = best
                ptr[idx(a, b)] = best_ptr

    seg_cost = dp[idx(ni, mj)]
    if not math.isfinite(seg_cost):
        # 理论上 validate_anchors 已排除该情况
        raise AlignmentError(
            f"锚点 {start} -> {end} 之间在带宽 {width} 内不存在可行路径"
        )

    # 回溯（不含 start，含 end）
    cells: list[tuple[int, int]] = []
    a, b = ni, mj
    while a > 0 or b > 0:
        p = ptr[idx(a, b)]
        cells.append((si + a, sj + b))
        if p == _PTR_MATCH:
            a -= 1
            b -= 1
        elif p == _PTR_SKIP:
            a -= 1
        elif p == _PTR_INSERT:
            b -= 1
        else:  # pragma: no cover - 理论不可达
            raise AlignmentError("回溯失败：遇到不可达状态")
    cells.reverse()
    return cells, seg_cost


def align_beats(
    ref: Sequence[float],
    perf: Sequence[float],
    params: AlignmentParams | None = None,
    anchors: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """执行带宽约束 DTW 对齐，返回完整结果字典。

    ``anchors`` 为可选的 ``(参考拍索引, 演奏音索引)`` 硬约束序列；
    也接受带 ``ref_index`` / ``perf_index`` 属性或同名键的对象。
    """
    params = params or AlignmentParams()
    n = len(ref)
    m = len(perf)
    if n == 0 or m == 0:
        raise AlignmentError("参考网格与演奏起始均不能为空")
    if abs(n - m) > params.band_width:
        raise AlignmentError(
            f"序列长度差 {abs(n - m)} 超过带宽 {params.band_width}，"
            "请增大 band_width 或检查输入"
        )

    norm_anchors = [_as_anchor(a, k) for k, a in enumerate(anchors or [])]
    validate_anchors(norm_anchors, n, m, params.band_width)

    skip_cost = (
        params.skip_penalty if params.skip_penalty is not None else _default_penalty(ref)
    )
    insert_cost = (
        params.insert_penalty
        if params.insert_penalty is not None
        else skip_cost
    )
    if skip_cost < 0 or insert_cost < 0:
        raise AlignmentError("惩罚代价不能为负")

    width = params.band_width

    # 首尾补虚拟端点 (0,0) 与 (n,m)，逐段 DTW 后合并为完整路径。
    # 注意网格单元 (i,j) 表示“前 i 拍/前 j 音”，匹配 ref[ri]↔perf[pj] 的
    # 对角步落在单元 (ri+1, pj+1)，因此锚点单元坐标需各 +1。
    # 除最后一段（终点为虚拟端点）外，每段终点是真实锚点单元，必须对角进入。
    boundaries: list[Anchor] = [
        (0, 0),
        *[(ri + 1, pj + 1) for ri, pj in norm_anchors],
        (n, m),
    ]
    cells: list[tuple[int, int]] = []
    total_cost = 0.0
    for k in range(len(boundaries) - 1):
        seg_cells, seg_cost = _dtw_segment(
            ref, perf, boundaries[k], boundaries[k + 1],
            width, skip_cost, insert_cost,
            force_diagonal_end=k < len(norm_anchors),
        )
        cells.extend(seg_cells)
        total_cost += seg_cost

    # 各段回溯产出不含段起点；统一在路径前补上虚拟起点 (0,0)（首段长度为 0
    # 时——例如第一个锚点就是 (0,0) 匹配——尤其需要），再逐步重建事件。
    full_cells: list[tuple[int, int]] = [(0, 0), *cells]

    # 从合并后的完整路径还原三类事件（首格 (0,0) 是起点，跳过）
    pairs: list[tuple[int, int]] = []
    missed: list[int] = []
    extra: list[int] = []
    prev_i = prev_j = 0
    for ci, cj in full_cells[1:]:
        if ci == prev_i + 1 and cj == prev_j + 1:
            pairs.append((ci - 1, cj - 1))
        elif ci == prev_i + 1 and cj == prev_j:
            missed.append(ci - 1)
        elif ci == prev_i and cj == prev_j + 1:
            extra.append(cj - 1)
        else:  # pragma: no cover - 段路径只由单步构成
            raise AlignmentError("分段路径合并失败")
        prev_i, prev_j = ci, cj
    missed.sort()
    extra.sort()

    matched = [
        {
            "ref_index": ri,
            "perf_index": pj,
            "ref_time": round(ref[ri], 6),
            "perf_time": round(perf[pj], 6),
            "deviation": round(perf[pj] - ref[ri], 6),
            "status": _classify(perf[pj] - ref[ri], params.on_time_threshold),
        }
        for ri, pj in pairs
    ]

    missed_beats = [
        {"ref_index": ri, "ref_time": round(ref[ri], 6)} for ri in missed
    ]
    inserted_beats = [
        {"perf_index": pj, "perf_time": round(perf[pj], 6)} for pj in extra
    ]

    anchor_set = set(norm_anchors)
    adopted_anchors = [
        {
            "ref_index": beat["ref_index"],
            "perf_index": beat["perf_index"],
            "ref_time": beat["ref_time"],
            "perf_time": beat["perf_time"],
            "deviation": beat["deviation"],
            "status": beat["status"],
        }
        for beat in matched
        if (beat["ref_index"], beat["perf_index"]) in anchor_set
    ]

    segments, signed_intervals = _build_intervals(matched, params.on_time_threshold)
    stats = _build_stats(
        matched, missed_beats, inserted_beats, total_cost,
        n, m, skip_cost, insert_cost,
    )

    return {
        "parameters": {
            "band_width": params.band_width,
            "skip_penalty": skip_cost,
            "insert_penalty": insert_cost,
            "on_time_threshold": params.on_time_threshold,
        },
        "anchors": adopted_anchors,
        "matched_beats": matched,
        "missed_beats": missed_beats,
        "inserted_beats": inserted_beats,
        "deviation_segments": segments,
        "signed_intervals": signed_intervals,
        "statistics": stats,
    }


def _build_intervals(
    matched: list[dict[str, Any]], threshold: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """生成两类连续偏差区间。

    deviation_segments：按参考拍索引连续的最大匹配段。每段内偏差可视为
        关于参考时间的连续分段线性函数；漏拍 / 插入拍造成缺口时另起一段。
    signed_intervals：在连续段内再按偏差符号（抢/准/拖）归并，给出每个
        同向连续区间的时间跨度与偏差统计。
    """
    segments: list[dict[str, Any]] = []
    signed_intervals: list[dict[str, Any]] = []

    run: list[dict[str, Any]] = []

    def flush_run(run_beats: list[dict[str, Any]]) -> None:
        if not run_beats:
            return
        devs = [b["deviation"] for b in run_beats]
        seg_idx = len(segments)
        segment = {
            "segment_index": seg_idx,
            "ref_start_index": run_beats[0]["ref_index"],
            "ref_end_index": run_beats[-1]["ref_index"],
            "ref_start_time": run_beats[0]["ref_time"],
            "ref_end_time": run_beats[-1]["ref_time"],
            "beat_count": len(run_beats),
            "min_deviation": round(min(devs), 6),
            "max_deviation": round(max(devs), 6),
            "mean_deviation": round(sum(devs) / len(devs), 6),
            "beats": run_beats,
        }
        segments.append(segment)

        # 段内按 status 归并出同向连续区间
        group: list[dict[str, Any]] = []

        def flush_group(beats: list[dict[str, Any]]) -> None:
            if not beats:
                return
            g_devs = [b["deviation"] for b in beats]
            signed_intervals.append({
                "segment_index": seg_idx,
                "status": beats[0]["status"],
                "ref_start_index": beats[0]["ref_index"],
                "ref_end_index": beats[-1]["ref_index"],
                "ref_start_time": beats[0]["ref_time"],
                "ref_end_time": beats[-1]["ref_time"],
                "beat_count": len(beats),
                "min_deviation": round(min(g_devs), 6),
                "max_deviation": round(max(g_devs), 6),
                "mean_deviation": round(sum(g_devs) / len(g_devs), 6),
            })

        for beat in run_beats:
            if group and beat["status"] != group[-1]["status"]:
                flush_group(group)
                group = []
            group.append(beat)
        flush_group(group)

    for beat in matched:
        if run and beat["ref_index"] != run[-1]["ref_index"] + 1:
            flush_run(run)
            run = []
        run.append(beat)
    flush_run(run)

    return segments, signed_intervals


def _build_stats(
    matched: list[dict[str, Any]],
    missed: list[dict[str, Any]],
    inserted: list[dict[str, Any]],
    total_cost: float,
    n: int,
    m: int,
    skip_cost: float,
    insert_cost: float,
) -> dict[str, Any]:
    devs = [b["deviation"] for b in matched]
    if devs:
        mean = sum(devs) / len(devs)
        rms = math.sqrt(sum(d * d for d in devs) / len(devs))
        abs_mean = sum(abs(d) for d in devs) / len(devs)
        max_abs = max(abs(d) for d in devs)
    else:
        mean = rms = abs_mean = max_abs = 0.0

    early = sum(1 for b in matched if b["status"] == "early")
    late = sum(1 for b in matched if b["status"] == "late")
    on_time = sum(1 for b in matched if b["status"] == "on_time")

    path_len = len(matched) + len(missed) + len(inserted)
    normalized_cost = total_cost / path_len if path_len else total_cost

    return {
        "ref_beat_count": n,
        "perf_onset_count": m,
        "matched_count": len(matched),
        "missed_count": len(missed),
        "inserted_count": len(inserted),
        "early_count": early,
        "on_time_count": on_time,
        "late_count": late,
        "mean_deviation": round(mean, 6),
        "mean_absolute_deviation": round(abs_mean, 6),
        "rms_deviation": round(rms, 6),
        "max_absolute_deviation": round(max_abs, 6),
        "total_cost": round(total_cost, 6),
        "normalized_cost": round(normalized_cost, 6),
    }
