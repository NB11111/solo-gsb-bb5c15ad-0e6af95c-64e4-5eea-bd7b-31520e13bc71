"""节奏曲线估计单元测试（标准库 unittest）。"""

import unittest

from app.tempo import TempoMapError, build_tempo_map


def _matched(pairs):
    """[(ref_index, perf_time), ...] -> matched_beats 片段。"""
    return [
        {"ref_index": ri, "perf_index": j, "ref_time": float(ri),
         "perf_time": pt, "deviation": 0.0, "status": "on_time"}
        for j, (ri, pt) in enumerate(pairs)
    ]


class TestBuildTempoMap(unittest.TestCase):
    def test_constant_tempo_single_event(self):
        # 120 BPM：演奏间隔恒为 0.5s
        beats = _matched([(i, 0.5 * i) for i in range(8)])
        out = build_tempo_map(beats)
        self.assertEqual(out["matched_beat_count"], 8)
        self.assertEqual(out["ignored_ref_indices"], [])
        self.assertEqual(len(out["events"]), 1)
        ev = out["events"][0]
        self.assertEqual((ev["ref_start_index"], ev["ref_end_index"]), (0, 7))
        self.assertEqual(ev["beat_count"], 7)
        self.assertEqual(ev["gap_count"], 7)
        self.assertAlmostEqual(ev["bpm"], 120.0, places=2)
        self.assertAlmostEqual(ev["start_time"], 0.0)
        self.assertAlmostEqual(ev["ref_time"], 0.0)

    def test_tempo_change_compressed_into_two_events(self):
        # 前 4 间隔 120 BPM(0.5s)，后 4 间隔 60 BPM(1.0s)
        times = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0]
        beats = _matched(list(enumerate(times)))
        out = build_tempo_map(beats)
        self.assertEqual(out["ignored_ref_indices"], [])
        self.assertEqual(len(out["events"]), 2)
        first, second = out["events"]
        self.assertAlmostEqual(first["bpm"], 120.0, places=2)
        self.assertEqual(first["ref_start_index"], 0)
        self.assertEqual(first["ref_end_index"], 4)
        self.assertEqual(first["beat_count"], 4)
        self.assertAlmostEqual(second["bpm"], 60.0, places=2)
        self.assertEqual(second["ref_start_index"], 4)
        self.assertEqual(second["ref_end_index"], 8)
        self.assertEqual(second["beat_count"], 4)
        self.assertAlmostEqual(second["start_time"], 2.0)
        # 覆盖拍数之和：相邻事件共享边界拍
        self.assertEqual(sum(e["beat_count"] for e in out["events"]), 8)

    def test_missed_beat_within_tolerance_ignored(self):
        # 120 BPM 稳定演奏；ref[1] 被漏，间隔 0↔2 的演奏差为 1.0s
        # （跨度 2 拍）-> 按参考拍跨度换算仍为 120 BPM，不忽略
        beats = _matched([(0, 0.0), (2, 1.0), (3, 1.5),
                          (4, 2.0), (5, 2.5), (6, 3.0)])
        out = build_tempo_map(beats, window_beats=3)
        self.assertEqual(out["ignored_ref_indices"], [])
        self.assertEqual(len(out["events"]), 1)
        self.assertAlmostEqual(out["events"][0]["bpm"], 120.0, places=0)
        # 覆盖拍数按参考拍跨度：漏拍索引 1 也被计入覆盖范围
        self.assertEqual(out["events"][0]["beat_count"], 6)

    def test_inserted_beat_residual_anomaly_ignored(self):
        # 正常间隔 0.5s(120 BPM)；ref2→ref3 之间多塞了一个匹配，
        # 该间隔演奏时间差 0.15s（跨度 1 拍）-> 400 BPM 异常值，被忽略
        beats = _matched([
            (0, 0.0), (1, 0.5), (2, 1.0),
            (3, 1.15), (4, 1.65), (5, 2.15), (6, 2.65),
        ])
        out = build_tempo_map(beats, window_beats=3)
        self.assertIn(2, out["ignored_ref_indices"])
        # 异常间隔不切断事件：前后同为 120 BPM，仍压缩为单个事件
        self.assertEqual(len(out["events"]), 1)
        ev = out["events"][0]
        self.assertAlmostEqual(ev["bpm"], 120.0, places=1)
        self.assertEqual(ev["gap_count"], 5)
        self.assertEqual(ev["beat_count"], 6)

    def test_missed_beat_residual_anomaly_ignored(self):
        # ref2 被漏，对齐把 ref1↔ref3 跨两拍配到约 1.08s 的演奏间隔上
        # -> 111 BPM 不算异常（前一例覆盖）；这里构造错位使跨拍间隔仍含
        # 残留：ref1=0.5s、ref3=1.0s，跨度 2 拍 -> 240 BPM，必须被忽略
        beats = _matched([
            (0, 0.0), (1, 0.5), (3, 1.0),
            (4, 1.5), (5, 2.0), (6, 2.5),
        ])
        out = build_tempo_map(beats, window_beats=3)
        self.assertIn(1, out["ignored_ref_indices"])
        self.assertEqual(len(out["events"]), 1)
        self.assertAlmostEqual(out["events"][0]["bpm"], 120.0, places=1)

    def test_min_max_bpm_clamps(self):
        # 90 BPM 的稳定演奏；限制 [100, 200] -> 全部间隔超范围
        beats = _matched([(i, (2.0 / 3.0) * i) for i in range(6)])
        out = build_tempo_map(beats, min_bpm=100.0, max_bpm=200.0)
        self.assertEqual(len(out["ignored_ref_indices"]), 5)
        self.assertEqual(out["events"], [])
        # 默认范围内不忽略
        out_default = build_tempo_map(beats)
        self.assertEqual(out_default["ignored_ref_indices"], [])
        self.assertAlmostEqual(out_default["events"][0]["bpm"], 90.0, places=1)

    def test_window_beats_one_uses_self_median(self):
        # 半径 0：中位数即自身，异常间隔无法被“邻居”拉回，
        # 但范围外仍会被忽略
        beats = _matched([
            (0, 0.0), (1, 0.5), (2, 1.0),
            (3, 1.15), (4, 1.65), (5, 2.15),
        ])
        out = build_tempo_map(beats, window_beats=1)
        self.assertIn(2, out["ignored_ref_indices"])

    def test_fewer_than_two_beats_raises(self):
        with self.assertRaises(TempoMapError):
            build_tempo_map([])
        with self.assertRaises(TempoMapError):
            build_tempo_map(_matched([(0, 0.0)]))

    def test_unsorted_input_sorted_by_ref_index(self):
        beats = _matched([(i, 0.5 * i) for i in range(4)])
        shuffled = [beats[2], beats[0], beats[3], beats[1]]
        out = build_tempo_map(shuffled)
        self.assertEqual(
            [(e["ref_start_index"], e["ref_end_index"]) for e in out["events"]],
            [(0, 3)],
        )

    def test_non_positive_time_delta_ignored(self):
        beats = _matched([(0, 0.0), (1, 0.0), (2, 0.5), (3, 1.0)])
        out = build_tempo_map(beats, window_beats=3)
        self.assertIn(0, out["ignored_ref_indices"])
        # 后两个间隔 120 BPM 正常
        self.assertEqual(len(out["events"]), 1)
        self.assertEqual(out["events"][0]["ref_start_index"], 1)


if __name__ == "__main__":
    unittest.main()
