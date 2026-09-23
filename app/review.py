"""复核片段（review segment）提取：从已完成的对齐结果聚合待人工复核的问题。

对齐结果中的三类问题先统一为带“参考拍位置”的事件，再按参考拍顺序聚合：

* ``missed``    —— 漏拍（参考网格有、演奏缺失），锚定在该参考拍索引；
* ``inserted``  —— 插入音（演奏多出的音），按其演奏时间线性内插到前后两个
  匹配拍之间的分数参考位置，跨度覆盖这两个匹配拍（位于所有匹配拍之前 /
  之后时退到最近的匹配拍）；
* ``deviation`` —— 匹配拍的 ``|偏差|`` **严格大于**偏差阈值，锚定在该参考拍。

相邻事件的最近边缘参考拍索引差不超过 ``gap_tolerance_beats``（可容忍的拍
间隔）时并入同一个复核片段，否则另起片段。每个片段返回 1 基拍号范围（同时
保留 0 基参考索引）、问题计数、逐问题明细与中文摘要。
"""

from __future__ import annotations

import bisect
from typing import Any, Sequence

_DECIMALS = 6

# 同位置事件的排序优先级：漏拍 / 偏差锚定在整拍，插入音位于整拍之间
_TYPE_RANK = {"missed": 0, "deviation": 1, "inserted": 2}


class ReviewError(ValueError):
    """复核片段提取参数非法。"""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, _DECIMALS)


def _inserted_position(
    beat: dict[str, Any],
    perf_indices: Sequence[int],
    matched_by_perf: Sequence[dict[str, Any]],
    n_ref: int,
) -> tuple[float, int, int]:
    """计算插入音的分数参考位置及其覆盖跨度 (lo, hi)（均为参考拍索引）。"""
    pj = beat["perf_index"]
    k = bisect.bisect_left(perf_indices, pj)
    prev = matched_by_perf[k - 1] if k > 0 else None
    nxt = matched_by_perf[k] if k < len(matched_by_perf) else None

    if prev is not None and nxt is not None:
        lo, hi = prev["ref_index"], nxt["ref_index"]
        dt = nxt["perf_time"] - prev["perf_time"]
        if dt > 0:
            frac = _clamp(
                (beat["perf_time"] - prev["perf_time"]) / dt, 0.0, 1.0
            )
        else:
            frac = 0.5
        return lo + frac * (hi - lo), lo, hi

    if prev is not None:
        lo = prev["ref_index"]
        hi = min(lo + 1, max(0, n_ref - 1))
        return 0.5 * (lo + hi), lo, hi

    if nxt is not None:
        hi = nxt["ref_index"]
        lo = max(0, hi - 1)
        return 0.5 * (lo + hi), lo, hi

    return 0.0, 0, max(0, n_ref - 1)


def _issue_missed(beat: dict[str, Any]) -> dict[str, Any]:
    ri = beat["ref_index"]
    return {
        "type": "missed",
        "ref_index": ri,
        "beat_number": ri + 1,
        "perf_index": None,
        "ref_time": beat["ref_time"],
        "perf_time": None,
        "deviation": None,
        "status": None,
        "description": f"漏拍：第 {ri + 1} 拍（参考时间 {beat['ref_time']:.3f}s）"
        "未检测到对应的演奏音",
    }


def _issue_deviation(beat: dict[str, Any], threshold: float) -> dict[str, Any]:
    ri = beat["ref_index"]
    dev = beat["deviation"]
    label = "抢拍" if dev < 0 else "拖拍"
    return {
        "type": "deviation",
        "ref_index": ri,
        "beat_number": ri + 1,
        "perf_index": beat["perf_index"],
        "ref_time": beat["ref_time"],
        "perf_time": beat["perf_time"],
        "deviation": _round(dev),
        "status": beat["status"],
        "description": (
            f"{label}：第 {ri + 1} 拍偏差 {dev:+.3f}s，"
            f"超过阈值 ±{threshold:.3f}s"
        ),
    }


def _issue_inserted(
    beat: dict[str, Any], lo: int, hi: int
) -> dict[str, Any]:
    pj = beat["perf_index"]
    if lo == hi:
        location = f"靠近参考第 {lo + 1} 拍"
    else:
        location = f"位于参考第 {lo + 1}–{hi + 1} 拍之间"
    return {
        "type": "inserted",
        "ref_index": None,
        "beat_number": None,
        "perf_index": pj,
        "ref_time": None,
        "perf_time": beat["perf_time"],
        "deviation": None,
        "status": None,
        "description": (
            f"插入音：演奏音 #{pj}（{beat['perf_time']:.3f}s）{location}，"
            "参考网格中无对应拍"
        ),
    }


def _build_summary(seg: dict[str, Any]) -> str:
    parts: list[str] = []
    if seg["missed_count"]:
        parts.append(f"漏拍 {seg['missed_count']} 处")
    if seg["inserted_count"]:
        parts.append(f"插入音 {seg['inserted_count']} 处")
    if seg["over_threshold_count"]:
        detail = f"超阈值偏差 {seg['over_threshold_count']} 拍"
        sub = []
        if seg["early_count"]:
            sub.append(f"抢拍 {seg['early_count']}")
        if seg["late_count"]:
            sub.append(f"拖拍 {seg['late_count']}")
        if sub:
            detail += f"（{'、'.join(sub)}）"
        parts.append(detail)
    if seg["max_abs_deviation"] is not None:
        parts.append(f"最大绝对偏差 {seg['max_abs_deviation']:.3f}s")
    text = "，".join(parts) if parts else "无问题"
    return (
        f"{text}；拍号范围 第 {seg['beat_number_start']}–"
        f"{seg['beat_number_end']} 拍"
    )


def extract_review_segments(
    result: dict[str, Any] | None,
    reference_beats: Sequence[float] | None = None,
    deviation_threshold: float = 0.05,
    gap_tolerance_beats: float = 2.0,
) -> dict[str, Any]:
    """把对齐结果中的问题聚合为复核片段。

    ``result`` 为 ``align_beats`` 的返回字典（或经 SQLite JSON 往返后的同构
    字典）；``reference_beats`` 为任务的参考节拍网格，用于片段边界时间与插入
    音跨度兜底。返回 ``segments`` 及汇总计数与本次参数。
    """
    if deviation_threshold <= 0:
        raise ReviewError("偏差阈值 deviation_threshold 必须为正数")
    if gap_tolerance_beats < 0:
        raise ReviewError("可容忍拍间隔 gap_tolerance_beats 不能为负")

    result = result or {}
    reference_beats = reference_beats or []
    matched = sorted(result.get("matched_beats") or [],
                     key=lambda b: b["ref_index"])
    missed = sorted(result.get("missed_beats") or [],
                    key=lambda b: b["ref_index"])
    inserted = sorted(result.get("inserted_beats") or [],
                      key=lambda b: b["perf_index"])

    # 参考拍时间索引：优先用任务输入，结果中的时间作为兜底
    ref_times: dict[int, float] = dict(enumerate(reference_beats))
    for b in matched:
        ref_times.setdefault(b["ref_index"], b["ref_time"])
    for b in missed:
        ref_times.setdefault(b["ref_index"], b["ref_time"])
    n_ref = max(
        len(reference_beats),
        max((b["ref_index"] + 1 for b in matched), default=0),
        max((b["ref_index"] + 1 for b in missed), default=0),
    )

    matched_by_perf = sorted(matched, key=lambda b: b["perf_index"])
    perf_indices = [b["perf_index"] for b in matched_by_perf]

    # ---- 1. 生成带参考位置 (pos) 与覆盖跨度 [lo, hi] 的问题事件 ----
    events: list[dict[str, Any]] = []
    for beat in missed:
        ri = beat["ref_index"]
        events.append({
            "pos": float(ri), "lo": ri, "hi": ri,
            "issue": _issue_missed(beat),
        })
    for beat in matched:
        if abs(beat["deviation"]) > deviation_threshold:
            ri = beat["ref_index"]
            events.append({
                "pos": float(ri), "lo": ri, "hi": ri,
                "issue": _issue_deviation(beat, deviation_threshold),
            })
    for beat in inserted:
        pos, lo, hi = _inserted_position(
            beat, perf_indices, matched_by_perf, n_ref
        )
        events.append({
            "pos": pos, "lo": lo, "hi": hi,
            "issue": _issue_inserted(beat, lo, hi),
        })

    events.sort(key=lambda e: (
        e["pos"], _TYPE_RANK[e["issue"]["type"]],
        e["issue"]["ref_index"] if e["issue"]["ref_index"] is not None
        else e["issue"]["perf_index"],
    ))

    # ---- 2. 间隔不超过可容忍拍间隔的相邻事件聚合为同一片段 ----
    raw_segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for event in events:
        if current is None or event["lo"] - current["hi"] > gap_tolerance_beats:
            current = {"lo": event["lo"], "hi": event["hi"],
                       "issues": [event["issue"]]}
            raw_segments.append(current)
        else:
            # 事件按位置排序，但其跨度左缘可能早于当前片段左缘（例如插入音
            # 覆盖前一个匹配拍，而该拍在当前锚点事件之前），两侧都要扩张
            current["lo"] = min(current["lo"], event["lo"])
            current["hi"] = max(current["hi"], event["hi"])
            current["issues"].append(event["issue"])

    # ---- 3. 整理片段输出 ----
    segments: list[dict[str, Any]] = []
    for index, seg in enumerate(raw_segments):
        lo, hi = seg["lo"], seg["hi"]
        issues = seg["issues"]
        deviations = [
            i["deviation"] for i in issues if i["type"] == "deviation"
        ]
        out = {
            "segment_index": index,
            "ref_start_index": lo,
            "ref_end_index": hi,
            "beat_number_start": lo + 1,
            "beat_number_end": hi + 1,
            "ref_start_time": _round(ref_times.get(lo)),
            "ref_end_time": _round(ref_times.get(hi)),
            "missed_count": sum(1 for i in issues if i["type"] == "missed"),
            "inserted_count": sum(
                1 for i in issues if i["type"] == "inserted"
            ),
            "over_threshold_count": len(deviations),
            "early_count": sum(
                1 for i in issues if i["type"] == "deviation"
                and i["status"] == "early"
            ),
            "late_count": sum(
                1 for i in issues if i["type"] == "deviation"
                and i["status"] == "late"
            ),
            "issue_count": len(issues),
            "max_abs_deviation": _round(
                max((abs(d) for d in deviations), default=None)
            ),
            "issues": issues,
        }
        out["summary"] = _build_summary(out)
        segments.append(out)

    return {
        "parameters": {
            "deviation_threshold": deviation_threshold,
            "gap_tolerance_beats": gap_tolerance_beats,
        },
        "segment_count": len(segments),
        "total_missed_count": sum(s["missed_count"] for s in segments),
        "total_inserted_count": sum(s["inserted_count"] for s in segments),
        "total_over_threshold_count": sum(
            s["over_threshold_count"] for s in segments
        ),
        "segments": segments,
    }
