"""复核片段提取单元测试（标准库 unittest）。"""

import unittest

from app.review import ReviewError, extract_review_segments

REF = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]


def _match(ri, dev=0.0, pj=None, status=None):
    pj = ri if pj is None else pj
    return {
        "ref_index": ri,
        "perf_index": pj,
        "ref_time": REF[ri],
        "perf_time": REF[ri] + dev,
        "deviation": dev,
        "status": status or "on_time",
    }


def _miss(ri):
    return {"ref_index": ri, "ref_time": REF[ri]}


def _insert(pj, t):
    return {"perf_index": pj, "perf_time": t}


def _result(matched=None, missed=None, inserted=None):
    return {
        "matched_beats": matched or [],
        "missed_beats": missed or [],
        "inserted_beats": inserted or [],
    }


class TestExtractReviewSegments(unittest.TestCase):
    def test_no_issues_empty_segments(self):
        out = extract_review_segments(
            _result(matched=[_match(i) for i in range(6)]), REF
        )
        self.assertEqual(out["segment_count"], 0)
        self.assertEqual(out["segments"], [])
        self.assertEqual(out["issue_count"], 0)
        self.assertEqual(out["ref_beat_count"], 6)
        self.assertEqual(out["parameters"]["deviation_threshold"], 0.03)
        self.assertEqual(out["parameters"]["gap_beats"], 1.0)

    def test_deviation_strictly_greater_than_threshold(self):
        # 偏差恰好等于阈值不算问题
        res = _result(matched=[_match(0, 0.03), _match(1, -0.031)])
        out = extract_review_segments(res, REF, deviation_threshold=0.03)
        self.assertEqual(out["issue_count"], 1)
        issue = out["segments"][0]["issues"][0]
        self.assertEqual(issue["type"], "over_threshold")
        self.assertEqual(issue["status"], "early")
        self.assertEqual(issue["beat_no"], 2)

    def test_single_missed_beat_segment(self):
        # 漏第 3 拍（索引 2），其余匹配且准时
        res = _result(
            matched=[_match(0), _match(1), _match(3, pj=2), _match(4, pj=3)],
            missed=[_miss(2)],
        )
        out = extract_review_segments(res, REF)
        self.assertEqual(out["missed_count"], 1)
        self.assertEqual(len(out["segments"]), 1)
        seg = out["segments"][0]
        self.assertEqual((seg["beat_start_no"], seg["beat_end_no"]), (3, 3))
        self.assertEqual((seg["ref_start_index"], seg["ref_end_index"]), (2, 2))
        self.assertAlmostEqual(seg["ref_start_time"], 1.0)
        self.assertEqual(seg["issue_count"], 1)
        self.assertEqual(seg["issue_types"], ["missed"])
        self.assertEqual(seg["missed_count"], 1)
        self.assertIn("第 3 拍", seg["summary"])
        self.assertIn("漏拍 1 处", seg["summary"])

    def test_inserted_beat_between_matches_spans_two_beats(self):
        # 演奏在 beat1 与 beat2 之间（perf 索引 2，1.25s）多了一个音
        res = _result(
            matched=[_match(0, pj=0), _match(1, pj=1),
                     _match(2, pj=3), _match(3, pj=4)],
            inserted=[_insert(2, 1.25)],
        )
        out = extract_review_segments(res, REF)
        self.assertEqual(out["inserted_count"], 1)
        seg = out["segments"][0]
        # 插入位置为参考拍 1 与 2 的中点 1.5，拍号范围向两侧取整 -> 第 2–3 拍
        self.assertEqual((seg["beat_start_no"], seg["beat_end_no"]), (2, 3))
        self.assertEqual((seg["ref_start_index"], seg["ref_end_index"]), (1, 2))
        self.assertAlmostEqual(seg["ref_start_time"], 0.5)
        self.assertAlmostEqual(seg["ref_end_time"], 1.0)
        issue = seg["issues"][0]
        self.assertEqual(issue["type"], "inserted")
        self.assertIsNone(issue["beat_no"])
        self.assertEqual(issue["perf_index"], 2)
        self.assertIn("插入音", issue["description"])

    def test_inserted_before_first_and_after_last(self):
        res = _result(
            matched=[_match(0, pj=1), _match(1, pj=2), _match(2, pj=3)],
            inserted=[_insert(0, -0.4), _insert(4, 2.6)],
        )
        out = extract_review_segments(res, REF, gap_beats=1.0)
        # 两个端点插入音相距 3 拍，超过容忍间隔，拆成两段
        self.assertEqual(len(out["segments"]), 2)
        self.assertEqual(
            (out["segments"][0]["beat_start_no"],
             out["segments"][0]["beat_end_no"]),
            (1, 1),
        )
        self.assertEqual(
            (out["segments"][1]["beat_start_no"],
             out["segments"][1]["beat_end_no"]),
            (3, 4),
        )
        # 放宽容忍间隔后合并为同一片段，拍号 1–4
        out = extract_review_segments(res, REF, gap_beats=3.0)
        self.assertEqual(len(out["segments"]), 1)
        seg = out["segments"][0]
        self.assertEqual((seg["beat_start_no"], seg["beat_end_no"]), (1, 4))
        self.assertEqual(seg["inserted_count"], 2)

    def test_gap_splits_and_merges_segments(self):
        # 漏拍在索引 1 和 4：坐标差 3
        res = _result(
            matched=[_match(0), _match(2, pj=1), _match(3, pj=2),
                     _match(5, pj=3)],
            missed=[_miss(1), _miss(4)],
        )
        out = extract_review_segments(res, REF, gap_beats=1.0)
        self.assertEqual(len(out["segments"]), 2)

        out = extract_review_segments(res, REF, gap_beats=2.0)
        self.assertEqual(len(out["segments"]), 2)

        out = extract_review_segments(res, REF, gap_beats=3.0)
        self.assertEqual(len(out["segments"]), 1)
        seg = out["segments"][0]
        self.assertEqual((seg["beat_start_no"], seg["beat_end_no"]), (2, 5))
        self.assertEqual(seg["missed_count"], 2)

    def test_adjacent_missed_and_deviation_merged(self):
        # 漏第 3 拍 + 第 4 拍超阈值拖拍，相邻问题默认合并
        res = _result(
            matched=[_match(0), _match(1),
                     _match(3, pj=2, dev=0.08, status="late"),
                     _match(4, pj=3)],
            missed=[_miss(2)],
        )
        out = extract_review_segments(res, REF)
        self.assertEqual(len(out["segments"]), 1)
        seg = out["segments"][0]
        self.assertEqual((seg["beat_start_no"], seg["beat_end_no"]), (3, 4))
        self.assertEqual(seg["missed_count"], 1)
        self.assertEqual(seg["over_threshold_count"], 1)
        self.assertEqual(seg["issue_types"], ["missed", "over_threshold"])
        self.assertAlmostEqual(seg["max_absolute_deviation"], 0.08)
        self.assertIn("漏拍 1 处", seg["summary"])
        self.assertIn("超阈值偏差 1 处", seg["summary"])
        self.assertIn("1 拖", seg["summary"])
        # 问题按参考拍顺序：漏拍（第 3 拍）在前
        self.assertEqual([i["type"] for i in seg["issues"]],
                         ["missed", "over_threshold"])

    def test_early_late_counts_and_overall_stats(self):
        res = _result(matched=[
            _match(0, dev=0.05, status="late"),
            _match(1, dev=-0.06, status="early"),
            _match(2),
            _match(3, dev=0.10, status="late"),
        ])
        out = extract_review_segments(res, REF, gap_beats=1.0)
        # 偏差问题坐标 0/1 相邻（差 1 ≤ 容忍）合并，坐标 3 相隔 2 拍另起一段 -> 2 段
        self.assertEqual(len(out["segments"]), 2)
        self.assertEqual(out["over_threshold_count"], 3)
        self.assertEqual(out["early_count"], 1)
        self.assertEqual(out["late_count"], 2)
        self.assertAlmostEqual(out["max_absolute_deviation"], 0.10)

    def test_ordering_by_reference_position(self):
        # 插入与漏拍混合，输出严格按参考拍坐标排序
        res = _result(
            matched=[_match(0, pj=0), _match(4, pj=4)],
            missed=[_miss(2)],
            inserted=[_insert(1, 0.6)],
        )
        out = extract_review_segments(res, REF, gap_beats=10.0)
        self.assertEqual(len(out["segments"]), 1)
        types = [i["type"] for i in out["segments"][0]["issues"]]
        # 插入位置 (0+4)/2=2.0 与漏拍索引 2 同坐标：漏拍优先于插入
        self.assertEqual(types, ["missed", "inserted"])

    def test_invalid_params_raise(self):
        res = _result(matched=[_match(0)])
        with self.assertRaises(ReviewError):
            extract_review_segments(res, REF, deviation_threshold=0)
        with self.assertRaises(ReviewError):
            extract_review_segments(res, REF, deviation_threshold=-0.1)
        with self.assertRaises(ReviewError):
            extract_review_segments(res, REF, gap_beats=-1)

    def test_malformed_result_raises(self):
        with self.assertRaises(ReviewError):
            extract_review_segments(None, REF)
        with self.assertRaises(ReviewError):
            extract_review_segments({}, REF)
        with self.assertRaises(ReviewError):
            extract_review_segments(
                {"matched_beats": [], "missed_beats": []}, REF
            )


if __name__ == "__main__":
    unittest.main()
