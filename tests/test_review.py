"""复核片段提取单元测试（标准库 unittest）。"""

import unittest

from app.review import ReviewError, extract_review_segments

REF = [0.5 * i for i in range(10)]


def _match(ri, deviation=0.0, status="on_time", pj=None, ref=None):
    rt = ref[ri] if ref is not None else REF[ri]
    return {
        "ref_index": ri, "perf_index": pj if pj is not None else ri,
        "ref_time": round(rt, 6), "perf_time": round(rt + deviation, 6),
        "deviation": deviation, "status": status,
    }


def _miss(ri, ref=None):
    rt = ref[ri] if ref is not None else REF[ri]
    return {"ref_index": ri, "ref_time": round(rt, 6)}


def _ins(pj, t):
    return {"perf_index": pj, "perf_time": round(t, 6)}


def _result(matched=(), missed=(), inserted=()):
    return {
        "matched_beats": list(matched),
        "missed_beats": list(missed),
        "inserted_beats": list(inserted),
    }


class TestExtractReviewSegments(unittest.TestCase):
    def test_no_problems_returns_empty(self):
        result = _result(matched=[_match(i) for i in range(6)])
        out = extract_review_segments(result, REF, deviation_threshold=0.05)
        self.assertEqual(out["segment_count"], 0)
        self.assertEqual(out["segments"], [])
        self.assertEqual(out["total_missed_count"], 0)

    def test_threshold_strict_greater_than(self):
        # 偏差恰好等于阈值不算问题
        result = _result(matched=[_match(0, 0.05, "late"),
                                  _match(1, -0.05, "early")])
        out = extract_review_segments(result, REF, deviation_threshold=0.05)
        self.assertEqual(out["segment_count"], 0)

        result = _result(matched=[_match(0, 0.051, "late")])
        out = extract_review_segments(result, REF, deviation_threshold=0.05)
        self.assertEqual(out["segment_count"], 1)
        issue = out["segments"][0]["issues"][0]
        self.assertEqual(issue["type"], "deviation")
        self.assertEqual(issue["status"], "late")
        self.assertEqual(issue["beat_number"], 1)

    def test_missed_and_late_merged_within_tolerance(self):
        # 漏第 2 拍（索引1），第 3–4 拍拖拍；间距 ≤ 2 拍 -> 一个片段
        matched = [_match(0), _match(2), _match(3, 0.08, "late"),
                   _match(4, 0.09, "late"), _match(5)]
        # 调整 perf_index 以匹配漏拍后的错位
        for b, pj in zip(matched, [0, 1, 2, 3, 4]):
            b["perf_index"] = pj
        result = _result(matched=matched, missed=[_miss(1)])
        out = extract_review_segments(result, REF, deviation_threshold=0.05)
        self.assertEqual(out["segment_count"], 1)
        seg = out["segments"][0]
        self.assertEqual((seg["ref_start_index"], seg["ref_end_index"]), (1, 4))
        self.assertEqual((seg["beat_number_start"], seg["beat_number_end"]),
                         (2, 5))
        self.assertEqual(seg["missed_count"], 1)
        self.assertEqual(seg["over_threshold_count"], 2)
        self.assertEqual(seg["late_count"], 2)
        self.assertEqual(seg["issue_count"], 3)
        self.assertAlmostEqual(seg["max_abs_deviation"], 0.09, places=6)
        self.assertEqual(seg["ref_start_time"], 0.5)
        self.assertEqual(seg["ref_end_time"], 2.0)
        # 事件按参考拍顺序：漏拍（拍 2）在前，拖拍在后
        self.assertEqual([i["type"] for i in seg["issues"]],
                         ["missed", "deviation", "deviation"])
        self.assertIn("漏拍 1 处", seg["summary"])
        self.assertIn("拖拍 2", seg["summary"])

    def test_far_missed_beats_split_by_tolerance(self):
        # 漏第 1 拍与第 8 拍：边缘差 7 拍
        result = _result(missed=[_miss(0), _miss(7)])
        out = extract_review_segments(result, REF, gap_tolerance_beats=2)
        self.assertEqual(out["segment_count"], 2)
        self.assertEqual(
            [(s["beat_number_start"], s["beat_number_end"]) for s in out["segments"]],
            [(1, 1), (8, 8)],
        )
        # 放大可容忍拍间隔后合并为一个片段
        merged = extract_review_segments(
            result, REF, gap_tolerance_beats=7
        )
        self.assertEqual(merged["segment_count"], 1)
        seg = merged["segments"][0]
        self.assertEqual((seg["beat_number_start"], seg["beat_number_end"]),
                         (1, 8))
        self.assertEqual(seg["missed_count"], 2)
        # 间隙 = 7 不超过容差 7 才合并；容差 6 仍分裂
        split = extract_review_segments(
            result, REF, gap_tolerance_beats=6
        )
        self.assertEqual(split["segment_count"], 2)

    def test_inserted_beat_interpolated_position_and_span(self):
        # ref1=0.5 ↔ perf 0.5，ref2=1.0 ↔ perf 1.0；插入音 0.75
        matched = [
            _match(1, pj=0, ref=REF),
            _match(2, pj=2, ref=REF),
        ]
        result = _result(matched=matched, inserted=[_ins(1, 0.75)])
        out = extract_review_segments(result, REF)
        self.assertEqual(out["segment_count"], 1)
        seg = out["segments"][0]
        # 插入音跨度覆盖前后匹配拍 1..2
        self.assertEqual((seg["ref_start_index"], seg["ref_end_index"]), (1, 2))
        self.assertEqual(seg["inserted_count"], 1)
        issue = seg["issues"][0]
        self.assertEqual(issue["type"], "inserted")
        self.assertEqual(issue["perf_index"], 1)
        self.assertIsNone(issue["ref_index"])
        self.assertIn("插入音", issue["description"])
        self.assertIn("第 2–3 拍", issue["description"])

    def test_inserted_near_missed_beat_merges(self):
        # 漏拍索引 3；插入音位于匹配拍 ref4 与 ref6 之间（ref5 漏拍的位置附近）
        matched = [
            _match(4, pj=0),
            _match(6, pj=2),
        ]
        result = _result(
            matched=matched,
            missed=[_miss(3), _miss(5)],
            inserted=[_ins(1, 0.5 * (REF[4] + REF[6]))],
        )
        out = extract_review_segments(result, REF, gap_tolerance_beats=0)
        # 容差 0：漏拍3 与（跨度 4..6 的插入音，含漏拍5）仍分裂；
        # 插入音事件的跨度 4..6 与漏拍 5 重叠，必属同一片段
        ranges = [
            (s["ref_start_index"], s["ref_end_index"])
            for s in out["segments"]
        ]
        self.assertIn((4, 6), ranges)
        merged_seg = next(s for s in out["segments"] if s["ref_start_index"] == 4)
        self.assertEqual(merged_seg["inserted_count"], 1)
        self.assertEqual(merged_seg["missed_count"], 1)

    def test_inserted_before_first_match_falls_back(self):
        matched = [_match(3, pj=1)]
        result = _result(matched=matched, inserted=[_ins(0, REF[3] - 0.2)])
        out = extract_review_segments(result, REF)
        self.assertEqual(out["segment_count"], 1)
        seg = out["segments"][0]
        self.assertEqual((seg["ref_start_index"], seg["ref_end_index"]), (2, 3))

    def test_only_inserted_no_matches(self):
        result = _result(inserted=[_ins(0, 1.0)])
        out = extract_review_segments(result, REF)
        self.assertEqual(out["segment_count"], 1)
        seg = out["segments"][0]
        self.assertEqual(seg["inserted_count"], 1)
        # 无匹配拍兜底：跨度退化为整个参考网格
        self.assertEqual(seg["ref_start_index"], 0)
        self.assertEqual(seg["ref_end_index"], 9)
        self.assertEqual(seg["ref_start_time"], 0.0)
        self.assertEqual(seg["ref_end_time"], 4.5)

    def test_event_ordering_reference_first(self):
        # 插入音在 ref2..ref3 之间，漏拍 ref2，拖拍 ref3：
        # 顺序应为 漏拍(2) -> 插入音(2.x) -> 拖拍(3)
        matched = [
            _match(2, pj=0),
            _match(3, pj=2, deviation=0.1, status="late"),
        ]
        result = _result(
            matched=matched,
            missed=[_miss(2)],
            inserted=[_ins(1, REF[2] + 0.1)],
        )
        out = extract_review_segments(result, REF, gap_tolerance_beats=10)
        self.assertEqual(out["segment_count"], 1)
        types = [i["type"] for i in out["segments"][0]["issues"]]
        self.assertEqual(types, ["missed", "inserted", "deviation"])

    def test_empty_result(self):
        out = extract_review_segments(None, [])
        self.assertEqual(out["segments"], [])
        self.assertEqual(out["segment_count"], 0)
        out = extract_review_segments({})
        self.assertEqual(out["segments"], [])

    def test_parameters_echoed(self):
        out = extract_review_segments(
            _result(), REF,
            deviation_threshold=0.12, gap_tolerance_beats=3.5,
        )
        self.assertEqual(out["parameters"], {
            "deviation_threshold": 0.12,
            "gap_tolerance_beats": 3.5,
        })

    def test_invalid_params_raise(self):
        with self.assertRaises(ReviewError):
            extract_review_segments(_result(), REF, deviation_threshold=0)
        with self.assertRaises(ReviewError):
            extract_review_segments(_result(), REF, deviation_threshold=-1)
        with self.assertRaises(ReviewError):
            extract_review_segments(_result(), REF, gap_tolerance_beats=-0.5)

    def test_unsorted_input_sorted(self):
        result = _result(missed=[_miss(6), _miss(0)])
        out = extract_review_segments(result, REF, gap_tolerance_beats=0)
        self.assertEqual(
            [s["ref_start_index"] for s in out["segments"]],
            [0, 6],
        )

    def test_early_and_late_counts(self):
        matched = [_match(0, -0.1, "early"), _match(1, 0.12, "late")]
        out = extract_review_segments(
            _result(matched=matched), REF, gap_tolerance_beats=0
        )
        # 相邻超阈值拍（索引 0、1，边缘差 1）容差 0 也分裂为两个片段
        self.assertEqual(out["segment_count"], 2)
        early_seg, late_seg = out["segments"]
        self.assertEqual(early_seg["early_count"], 1)
        self.assertEqual(late_seg["late_count"], 1)
        self.assertIn("抢拍 1", early_seg["summary"])
        self.assertIn("拖拍 1", late_seg["summary"])


if __name__ == "__main__":
    unittest.main()
