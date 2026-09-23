"""FastAPI 接口集成测试：使用临时 SQLite 文件。"""

import os
import tempfile
import unittest

# 必须在导入 app.* 之前设置数据库路径（app.database 在导入时读取该变量）
_TMPDIR = tempfile.mkdtemp(prefix="beat-align-test-")
os.environ["ALIGN_DB_PATH"] = os.path.join(_TMPDIR, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

REF = [0.0, 0.5, 1.0, 1.5, 2.0]
PERF = [0.0, 0.48, 1.52, 2.0]  # 漏第 3 拍(1.0)，且有轻微抢/拖


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._cm = TestClient(app)
        cls.client = cls._cm.__enter__()
        cls.addClassCleanup(cls._cm.__exit__, None, None, None)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_full_flow(self):
        # 1. 创建
        r = self.client.post("/jobs", json={
            "name": "take-01",
            "reference_beats": REF,
            "performance_onsets": PERF,
            "params": {"band_width": 2},
        })
        self.assertEqual(r.status_code, 201, r.text)
        job = r.json()
        self.assertEqual(job["status"], "pending")
        job_id = job["id"]

        # 2. 对齐
        r = self.client.post(f"/jobs/{job_id}/align")
        self.assertEqual(r.status_code, 200, r.text)
        done = r.json()
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["result"]["statistics"]["missed_count"], 1)
        self.assertEqual(done["result"]["statistics"]["matched_count"], 4)

        # 3. 查询
        r = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "done")

        r = self.client.get(f"/jobs/{job_id}/result")
        self.assertEqual(r.status_code, 200)
        self.assertIn("matched_beats", r.json()["result"])

        r = self.client.get("/jobs")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(len(r.json()), 1)

        # 4. 重复对齐不重算；force=true 可重算
        r = self.client.post(f"/jobs/{job_id}/align")
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/jobs/{job_id}/align?force=true")
        self.assertEqual(r.status_code, 200)

    def test_404(self):
        r = self.client.get("/jobs/999999")
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/jobs/999999/align")
        self.assertEqual(r.status_code, 404)

    def test_result_before_align_conflicts(self):
        r = self.client.post("/jobs", json={
            "reference_beats": REF, "performance_onsets": REF,
        })
        job_id = r.json()["id"]
        r = self.client.get(f"/jobs/{job_id}/result")
        self.assertEqual(r.status_code, 409)

    def test_band_too_narrow_rejected(self):
        r = self.client.post("/jobs", json={
            "reference_beats": [0.0, 0.5, 1.0, 1.5],
            "performance_onsets": [0.0],
            "params": {"band_width": 1},
        })
        self.assertEqual(r.status_code, 400)

    def test_non_monotonic_rejected(self):
        r = self.client.post("/jobs", json={
            "reference_beats": [0.0, 0.6, 0.5],
            "performance_onsets": [0.0, 0.5],
        })
        self.assertEqual(r.status_code, 422)

    def test_anchor_full_flow(self):
        # 自由对齐会漏掉 ref[1]、ref[2]；锚点强制 (1,1)、(2,2) 匹配
        body = {
            "name": "anchor-take",
            "reference_beats": [0.0, 1.0, 2.0, 3.0],
            "performance_onsets": [0.0, 0.1, 2.9],
            "params": {"band_width": 3},
            "anchors": [
                {"ref_index": 1, "perf_index": 1},
                {"ref_index": 2, "perf_index": 2},
            ],
        }
        r = self.client.post("/jobs", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        job = r.json()
        # 任务查询包含提交的锚点（SQLite 持久化往返）
        self.assertEqual(job["anchors"], body["anchors"])
        job_id = job["id"]

        r = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["anchors"], body["anchors"])

        r = self.client.post(f"/jobs/{job_id}/align")
        self.assertEqual(r.status_code, 200, r.text)
        result = r.json()["result"]
        pairs = [(b["ref_index"], b["perf_index"]) for b in result["matched_beats"]]
        self.assertEqual(pairs, [(0, 0), (1, 1), (2, 2)])
        # result 中包含已采用锚点
        self.assertEqual(
            [(a["ref_index"], a["perf_index"]) for a in result["anchors"]],
            [(1, 1), (2, 2)],
        )
        self.assertEqual(result["anchors"][0]["deviation"], -0.9)

        # /result 端点同样可查到已采用锚点
        r = self.client.get(f"/jobs/{job_id}/result")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["result"]["anchors"]), 2)

        # 重新查询确认结果已从 SQLite 读出
        r = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(len(r.json()["result"]["anchors"]), 2)

    def test_anchor_skipped_within_segment(self):
        # 锚点之间仍允许漏拍
        r = self.client.post("/jobs", json={
            "reference_beats": [0.0, 0.5, 1.0, 1.5, 2.0],
            "performance_onsets": [0.0, 0.5, 1.5, 2.0],
            "params": {"band_width": 3},
            "anchors": [{"ref_index": 0, "perf_index": 0},
                        {"ref_index": 4, "perf_index": 3}],
        })
        self.assertEqual(r.status_code, 201, r.text)
        job_id = r.json()["id"]
        r = self.client.post(f"/jobs/{job_id}/align")
        self.assertEqual(r.status_code, 200, r.text)
        result = r.json()["result"]
        self.assertEqual([b["ref_index"] for b in result["missed_beats"]], [2])

    def test_anchor_out_of_range_400(self):
        r = self.client.post("/jobs", json={
            "reference_beats": [0.0, 0.5, 1.0],
            "performance_onsets": [0.0, 0.5, 1.0],
            "params": {"band_width": 1},
            "anchors": [{"ref_index": 3, "perf_index": 0}],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("越界", r.json()["detail"])

    def test_anchor_not_increasing_400(self):
        r = self.client.post("/jobs", json={
            "reference_beats": [0.0, 0.5, 1.0, 1.5],
            "performance_onsets": [0.0, 0.5, 1.0, 1.5],
            "params": {"band_width": 2},
            "anchors": [{"ref_index": 1, "perf_index": 1},
                        {"ref_index": 1, "perf_index": 2}],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("严格递增", r.json()["detail"])

    def test_anchor_band_unreachable_400(self):
        r = self.client.post("/jobs", json={
            "reference_beats": [0.0, 0.5, 1.0, 1.5],
            "performance_onsets": [0.0, 0.5, 1.0, 1.5],
            "params": {"band_width": 1},
            "anchors": [{"ref_index": 3, "perf_index": 0}],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("带宽", r.json()["detail"])

    def test_anchor_negative_index_400(self):
        r = self.client.post("/jobs", json={
            "reference_beats": [0.0, 0.5, 1.0],
            "performance_onsets": [0.0, 0.5, 1.0],
            "anchors": [{"ref_index": -1, "perf_index": 0}],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能为负", r.json()["detail"])

    def test_job_without_anchors_compatible(self):
        # 未提供 anchors 的任务：输入侧返回 []，结果侧返回 []
        r = self.client.post("/jobs", json={
            "reference_beats": [0.0, 0.5, 1.0],
            "performance_onsets": [0.0, 0.5, 1.0],
        })
        self.assertEqual(r.status_code, 201, r.text)
        job_id = r.json()["id"]
        self.assertEqual(r.json()["anchors"], [])
        r = self.client.post(f"/jobs/{job_id}/align")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"]["anchors"], [])

    def test_legacy_db_without_anchors_column(self):
        # 模拟旧版数据库（jobs 表没有 anchors 列）：迁移后旧行可读、新任务可建
        import sqlite3
        from app import database
        db_path = os.environ["ALIGN_DB_PATH"]

        # 测试库：丢弃当前表并按旧结构重建，写入一行旧数据
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP TABLE IF EXISTS jobs")
            conn.commit()
        finally:
            conn.close()
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    reference_beats TEXT NOT NULL,
                    performance_onsets TEXT NOT NULL,
                    params TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    aligned_at TEXT
                );
                INSERT INTO jobs (name, reference_beats, performance_onsets,
                                  params, status, created_at)
                VALUES ('old', '[0, 1]', '[0, 1]', '{}', 'pending',
                        '2026-01-01T00:00:00+00:00');
                """
            )
            conn.commit()
        finally:
            conn.close()

        database.init_db()  # 触发幂等迁移
        # 旧行可读，anchors 兜底为空列表
        r = self.client.get("/jobs")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(j["name"] == "old" for j in r.json()))

        # 迁移后仍可正常创建带 / 不带锚点的新任务
        r = self.client.post("/jobs", json={
            "reference_beats": [0.0, 1.0],
            "performance_onsets": [0.0, 1.0],
            "anchors": [{"ref_index": 0, "perf_index": 0}],
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["anchors"], [{"ref_index": 0, "perf_index": 0}])

    # ---- 节奏曲线导出 GET /jobs/{id}/tempo-map ----

    def _create_done_job(self, body):
        r = self.client.post("/jobs", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        job_id = r.json()["id"]
        r = self.client.post(f"/jobs/{job_id}/align")
        self.assertEqual(r.status_code, 200, r.text)
        return job_id

    def test_tempo_map_constant_tempo(self):
        job_id = self._create_done_job({
            "reference_beats": [0.5 * i for i in range(8)],
            "performance_onsets": [0.5 * i for i in range(8)],
        })
        r = self.client.get(f"/jobs/{job_id}/tempo-map")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["job_id"], job_id)
        self.assertEqual(data["matched_beat_count"], 8)
        self.assertEqual(data["ignored_ref_indices"], [])
        self.assertEqual(len(data["events"]), 1)
        ev = data["events"][0]
        self.assertAlmostEqual(ev["bpm"], 120.0, places=2)
        self.assertEqual(ev["ref_start_index"], 0)
        self.assertEqual(ev["ref_end_index"], 7)
        self.assertEqual(ev["beat_count"], 7)
        self.assertEqual(ev["gap_count"], 7)
        self.assertEqual(data["parameters"]["window_beats"], 5)

    def test_tempo_map_change_detected_and_params_passthrough(self):
        # 前半 120 BPM、后半 100 BPM；首尾锚点保证 9 拍全部一一匹配
        body = {
            "reference_beats": [0.5 * i for i in range(9)],
            "performance_onsets": [0, .5, 1.0, 1.5, 2.0, 2.6, 3.2, 3.8, 4.4],
            "params": {"band_width": 3},
            "anchors": [{"ref_index": 0, "perf_index": 0},
                        {"ref_index": 8, "perf_index": 8}],
        }
        job_id = self._create_done_job(body)
        r = self.client.get(
            f"/jobs/{job_id}/tempo-map?window_beats=3&min_bpm=50&max_bpm=250"
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["ignored_ref_indices"], [])
        self.assertEqual(len(data["events"]), 2)
        self.assertAlmostEqual(data["events"][0]["bpm"], 120.0, places=1)
        self.assertAlmostEqual(data["events"][1]["bpm"], 100.0, places=1)
        self.assertEqual(data["events"][0]["ref_end_index"],
                         data["events"][1]["ref_start_index"])
        self.assertEqual(data["parameters"]["window_beats"], 3)
        self.assertEqual(data["parameters"]["min_bpm"], 50.0)
        self.assertEqual(data["parameters"]["max_bpm"], 250.0)

    def test_tempo_map_skipped_beat_span_normalized(self):
        # 漏 ref[1]（锚点强制），其余 120 BPM；间隔 0↔2 跨两拍 / 1.0s
        # -> 按参考拍跨度换算仍为 120 BPM，覆盖拍数含被漏的拍
        body = {
            "reference_beats": [0.0, 0.5, 1.0, 1.5, 2.0],
            "performance_onsets": [0.0, 1.0, 1.5, 2.0],
            "params": {"band_width": 3},
            "anchors": [{"ref_index": 0, "perf_index": 0},
                        {"ref_index": 4, "perf_index": 3}],
        }
        job_id = self._create_done_job(body)
        r = self.client.get(f"/jobs/{job_id}/tempo-map?window_beats=3")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["ignored_ref_indices"], [])
        self.assertEqual(len(data["events"]), 1)
        self.assertAlmostEqual(data["events"][0]["bpm"], 120.0, places=1)
        self.assertEqual(data["events"][0]["beat_count"], 4)

    def test_tempo_map_ignored_residual_anomaly(self):
        # 手工持久化一个含残留异常间隔的 done 结果：ref2→ref3 只有 0.15s
        # -> 400 BPM，被中位数窗口与 [40,300] 范围联合判为异常并忽略
        job_id = self._create_done_job({
            "reference_beats": [0.5 * i for i in range(7)],
            "performance_onsets": [0.5 * i for i in range(7)],
        })
        from app import crud, database
        conn = database.connect()
        try:
            job = crud.get_job(conn, job_id)
            result = job["result"]
            result["matched_beats"] = [
                {"ref_index": 0, "perf_index": 0, "ref_time": 0.0,
                 "perf_time": 0.0, "deviation": 0.0, "status": "on_time"},
                {"ref_index": 1, "perf_index": 1, "ref_time": 0.5,
                 "perf_time": 0.5, "deviation": 0.0, "status": "on_time"},
                {"ref_index": 2, "perf_index": 2, "ref_time": 1.0,
                 "perf_time": 1.0, "deviation": 0.0, "status": "on_time"},
                {"ref_index": 3, "perf_index": 3, "ref_time": 1.5,
                 "perf_time": 1.15, "deviation": -0.35, "status": "early"},
                {"ref_index": 4, "perf_index": 4, "ref_time": 2.0,
                 "perf_time": 1.65, "deviation": -0.35, "status": "early"},
                {"ref_index": 5, "perf_index": 5, "ref_time": 2.5,
                 "perf_time": 2.15, "deviation": -0.35, "status": "early"},
                {"ref_index": 6, "perf_index": 6, "ref_time": 3.0,
                 "perf_time": 2.65, "deviation": -0.35, "status": "early"},
            ]
            crud.save_result(conn, job_id, result)
        finally:
            conn.close()

        r = self.client.get(f"/jobs/{job_id}/tempo-map?window_beats=3")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn(2, data["ignored_ref_indices"])
        # 异常间隔不切断事件：前后同速，合并为单个 120 BPM 事件
        self.assertEqual(len(data["events"]), 1)
        self.assertAlmostEqual(data["events"][0]["bpm"], 120.0, places=1)
        self.assertEqual(data["events"][0]["gap_count"], 5)

    def test_tempo_map_error_states(self):
        # 404：任务不存在
        r = self.client.get("/jobs/999999/tempo-map")
        self.assertEqual(r.status_code, 404)

        # 409：任务未对齐
        r = self.client.post("/jobs", json={
            "reference_beats": REF, "performance_onsets": PERF,
        })
        pending_id = r.json()["id"]
        r = self.client.get(f"/jobs/{pending_id}/tempo-map")
        self.assertEqual(r.status_code, 409)

        # 400：上次对齐失败
        from app import crud, database
        conn = database.connect()
        try:
            crud.save_error(conn, pending_id, "boom")
        finally:
            conn.close()
        r = self.client.get(f"/jobs/{pending_id}/tempo-map")
        self.assertEqual(r.status_code, 400)
        self.assertIn("boom", r.json()["detail"])

        # 409：已完成但没有匹配拍（手工持久化空 matched_beats）
        empty_id = self._create_done_job({
            "reference_beats": [0.0, 1.0],
            "performance_onsets": [0.0, 1.0],
        })
        conn = database.connect()
        try:
            job = crud.get_job(conn, empty_id)
            result = job["result"]
            result["matched_beats"] = []
            crud.save_result(conn, empty_id, result)
        finally:
            conn.close()
        r = self.client.get(f"/jobs/{empty_id}/tempo-map")
        self.assertEqual(r.status_code, 409)
        self.assertIn("匹配拍", r.json()["detail"])

    def test_tempo_map_param_validation(self):
        job_id = self._create_done_job({
            "reference_beats": [0.5 * i for i in range(6)],
            "performance_onsets": [0.5 * i for i in range(6)],
        })
        # min_bpm >= max_bpm -> 422
        r = self.client.get(f"/jobs/{job_id}/tempo-map?min_bpm=200&max_bpm=100")
        self.assertEqual(r.status_code, 422)
        self.assertIn("min_bpm", r.json()["detail"])
        # window_beats 越界 -> 422
        r = self.client.get(f"/jobs/{job_id}/tempo-map?window_beats=0")
        self.assertEqual(r.status_code, 422)
        r = self.client.get(f"/jobs/{job_id}/tempo-map?window_beats=102")
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
