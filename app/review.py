"""复核片段（review segment）提取：把对齐结果里需要人工复核的问题聚合成片段。

输入一次 DTW 对齐结果（``matched_beats`` / ``missed_beats`` /
``inserted_beats``）与参考节拍网格，挑出三类问题：

* missed          —— 漏拍：参考网格里有、演奏中缺失；
* inserted        —— 插入音：演奏中多出来、参考网格里没有的音；
* over_threshold  —— 超阈值偏差：``|perf_time − ref_time|`` 严格大于给定
  ``deviation_threshold`` 的匹配拍。

所有问题先投影到统一的“参考拍坐标”（漏拍 / 偏差取该参考拍的整数索引；
插入音取其在相邻匹配拍之间的中点位置，可为半拍坐标，端点外的插入音挂在
首 / 末匹配拍外侧半拍），按参考拍顺序排序，再用可容忍的拍间隔
``gap_beats`` 聚合：相邻问题的坐标差不超过 ``gap_beats`` 即并入同一复核片段，
否则另起一片段。每个片段返回拍号范围（1 基、含端点）与问题摘要。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any, Sequence

_TIME_DECIMALS = 6

# 同一坐标上的问题排序优先级：漏拍 -> 超阈值偏差 -> 插入音
_RANK = {"missed": 0, "over_threshold": 1, "inserted": 2}


class ReviewError(ValueError):
    """对齐结果缺少复核所需的字段，无法提取复核片段。"""


def _round(value: float) -> float:
    return round(value, _TIME_DECIMALS)


def _build_issues(
    result: dict[str, Any],
    deviation_threshold: float,
) -> list[dict[str, Any]]:
    """把三类问题统一成带参考拍坐标 ``pos`` 的有序列表。"""
    matched = sorted(
        result.get("matched_beats", []), key=lambda b: b["perf_index"]
    )
    perf_indices = [b["perf_index"] for b in matched]
    ref_positions = [b["ref_index"] for b in matched]

    issues: list[dict[str, Any]] = []

    for beat in result.get("missed_beats", []):
        ri = beat["ref_index"]
        issues.append({
            "type": "missed",
            "pos": float(ri),
            "beat_no": ri + 1,
            "ref_index": ri,
            "ref_time": beat.get("ref_time"),
            "perf_index": None,
            "perf_time": None,
            "deviation": None,
            "status": None,
            "description": (
                f"漏拍：第 {ri + 1} 拍（参考时间 {beat.get('ref_time', 0.0):.3f}s）"
                "在演奏中缺失"
            ),
        })

    for beat in result.get("inserted_beats", []):
        pj = beat["perf_index"]
        # 路径事件按 perf 序交错，插入音必落在两个匹配音之间（或两端之外）
        k = bisect_right(perf_indices, pj)
        if not matched:
            pos = 0.0
        elif k == 0:
            pos = ref_positions[0] - 0.5
        elif k == len(matched):
            pos = ref_positions[-1] + 0.5
        else:
            pos = 0.5 * (ref_positions[k - 1] + ref_positions[k])
        issues.append({
            "type": "inserted",
            "pos": _round(pos),
            "beat_no": None,
            "ref_index": None,
            "ref_time": None,
            "perf_index": pj,
            "perf_time": beat.get("perf_time"),
            "deviation": None,
            "status": None,
            "description": (
                f"插入音：演奏音 {pj}（{beat.get('perf_time', 0.0):.3f}s）"
                "在参考网格中无对应拍"
            ),
        })

    for beat in matched:
        dev = beat["deviation"]
        if abs(dev) <= deviation_threshold:
            continue
        ri = beat["ref_index"]
        status = "early" if dev < 0 else "late"
        direction = "抢拍（演奏偏早）" if status == "early" else "拖拍（演奏偏晚）"
        issues.append({
            "type": "over_threshold",
            "pos": float(ri),
            "beat_no": ri + 1,
            "ref_index": ri,
            "ref_time": beat.get("ref_time"),
            "perf_index": beat["perf_index"],
            "perf_time": beat.get("perf_time"),
            "deviation": _round(dev),
            "status": status,
            "description": (
                f"{direction}：第 {ri + 1} 拍偏差 {dev:+.3f}s，"
                f"超过阈值 {deviation_threshold:.3f}s"
            ),
        })

    issues.sort(key=lambda it: (it["pos"], _RANK[it["type"]]))
    return issues


def _build_summary(
    beat_start: int,
    beat_end: int,
    counts: dict[str, int],
    early: int,
    late: int,
    max_abs: float,
) -> str:
    span = f"第 {beat_start} 拍" if beat_start == beat_end else f"第 {beat_start}–{beat_end} 拍"
    parts: list[str] = []
    if counts["missed"]:
        parts.append(f"漏拍 {counts['missed']} 处")
    if counts["inserted"]:
        parts.append(f"插入音 {counts['inserted']} 处")
    if counts["over_threshold"]:
        parts.append(
            f"超阈值偏差 {counts['over_threshold']} 处"
            f"（{late} 拖 / {early} 抢，最大 |偏差| {max_abs:.3f}s）"
        )
    return f"{span}：" + "，".join(parts)


def extract_review_segments(
    result: dict[str, Any] | None,
    reference_beats: Sequence[float],
    deviation_threshold: float = 0.03,
    gap_beats: float = 1.0,
) -> dict[str, Any]:
    """从对齐结果提取复核片段。

    deviation_threshold: 偏差阈值（秒，>0），匹配拍的 |偏差| 严格大于该值
        即列为超阈值偏差问题。
    gap_beats: 可容忍的拍间隔（拍数，≥0）；按参考拍顺序相邻的两个问题，
        坐标差不超过该值时聚入同一复核片段，否则断开另起一片段。

    返回 ``segments`` 与总体计数；没有任何问题时 ``segments`` 为空列表。
    """
    if deviation_threshold <= 0:
        raise ReviewError("偏差阈值必须为正数")
    if gap_beats < 0:
        raise ReviewError("可容忍的拍间隔不能为负")
    if not isinstance(result, dict):
        raise ReviewError("缺少对齐结果")
    for key in ("matched_beats", "missed_beats", "inserted_beats"):
        if not isinstance(result.get(key), list):
            raise ReviewError(f"对齐结果缺少 {key} 列表")

    n_ref = len(reference_beats)
    issues = _build_issues(result, deviation_threshold)

    # ---- 按可容忍拍间隔聚合成片段 ----
    clusters: list[list[dict[str, Any]]] = []
    for issue in issues:
        if clusters and issue["pos"] - clusters[-1][-1]["pos"] <= gap_beats:
            clusters[-1].append(issue)
        else:
            clusters.append([issue])

    segments: list[dict[str, Any]] = []
    for idx, cluster in enumerate(clusters):
        counts = {"missed": 0, "inserted": 0, "over_threshold": 0}
        early = late = 0
        max_abs = 0.0
        issue_types: list[str] = []
        for issue in cluster:
            t = issue["type"]
            counts[t] += 1
            if t not in issue_types:
                issue_types.append(t)
            if t == "over_threshold":
                if issue["status"] == "early":
                    early += 1
                else:
                    late += 1
                max_abs = max(max_abs, abs(issue["deviation"]))

        # 半拍坐标（插入音）向两侧取整拍，再夹到参考网格范围内
        eps = 1e-9
        lo = max(0, math.floor(cluster[0]["pos"] + eps))
        hi = min(n_ref - 1, math.ceil(cluster[-1]["pos"] - eps)) if n_ref else 0
        lo = min(lo, hi)  # n_ref==0 等退化情况兜底

        public_issues = [
            {k: v for k, v in issue.items() if k != "pos"} for issue in cluster
        ]
        summary = _build_summary(lo + 1, hi + 1, counts, early, late, max_abs)
        segments.append({
            "segment_index": idx,
            "beat_start_no": lo + 1,
            "beat_end_no": hi + 1,
            "ref_start_index": lo,
            "ref_end_index": hi,
            "ref_start_time": _round(reference_beats[lo]) if n_ref else None,
            "ref_end_time": _round(reference_beats[hi]) if n_ref else None,
            "issue_count": len(cluster),
            "missed_count": counts["missed"],
            "inserted_count": counts["inserted"],
            "over_threshold_count": counts["over_threshold"],
            "issue_types": issue_types,
            "max_absolute_deviation": _round(max_abs),
            "summary": summary,
            "issues": public_issues,
        })

    total = {"missed": 0, "inserted": 0, "over_threshold": 0}
    early_total = late_total = 0
    overall_max = 0.0
    for issue in issues:
        total[issue["type"]] += 1
        if issue["type"] == "over_threshold":
            if issue["status"] == "early":
                early_total += 1
            else:
                late_total += 1
            overall_max = max(overall_max, abs(issue["deviation"]))

    return {
        "parameters": {
            "deviation_threshold": deviation_threshold,
            "gap_beats": gap_beats,
        },
        "ref_beat_count": n_ref,
        "segment_count": len(segments),
        "issue_count": len(issues),
        "missed_count": total["missed"],
        "inserted_count": total["inserted"],
        "over_threshold_count": total["over_threshold"],
        "early_count": early_total,
        "late_count": late_total,
        "max_absolute_deviation": _round(overall_max),
        "segments": segments,
    }
