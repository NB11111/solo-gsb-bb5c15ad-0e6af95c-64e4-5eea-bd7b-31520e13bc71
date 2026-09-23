# 多轨录音节拍对齐 API（FastAPI + SQLite）

客户端提交**参考节拍网格**（reference beat grid）与某条演奏轨的**音符起始时间**
（note onsets）。服务端使用**带宽约束的动态时间规整（Sakoe-Chiba Band DTW）**
在带状窗口内寻找最优对齐路径，显式处理：

- **漏拍（missed beat）**：参考网格里有、演奏中缺失的拍；
- **抢拍（early）/ 拖拍（late）**：演奏相对参考网格的连续时间偏差；
- **插入拍（inserted beat）**：演奏中多出来、参考网格里没有的音。

回溯最优路径后输出：参考拍 ↔ 演奏音的**一一对应映射**、漏拍 / 插入拍清单、
按连续段组织的**偏差区间**及统计指标。结果持久化到 SQLite，可反复查询。

创建任务时还可以提供**锚点（anchors）硬约束**：显式声明某些“参考拍 ↔ 演奏音”
必须配对（例如人工确认的重音点、已知小节线位置）。对齐时 DTW 被拆成相邻锚点
之间的若干独立子段分别寻优，再合并为完整路径——锚点保证被采用，段内仍允许
漏拍、插入拍与连续偏差。

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由
│   ├── alignment.py     # 带宽约束 DTW 核心算法（纯标准库，含锚点分段对齐）
│   ├── tempo.py         # 节奏曲线（tempo map）估计：中位数窗口 + 稳定区间压缩
│   ├── schemas.py       # Pydantic 请求/响应模型
│   ├── database.py      # SQLite 连接、建表与旧库迁移
│   └── crud.py          # 任务读写
├── tests/               # unittest 测试（对齐算法 18 个 + 节奏曲线 10 个 + 接口 20 个）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 服务端口：**`8000`**（容器内 8000，映射到宿主机同端口）
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 只读文档（ReDoc）：<http://localhost:8000/redoc>
- 健康检查：`GET http://localhost:8000/health`
- SQLite 数据文件：容器内 `/data/alignment.db`，由命名卷 `beat-data` 持久化

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据库卷
```

## 本地（无 Docker）运行

需要 Python 3.11：

```bash
pip install -r requirements.txt
ALIGN_DB_PATH=./data/alignment.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

## API 一览

| 方法   | 路径                       | 说明                                   |
| ------ | -------------------------- | -------------------------------------- |
| POST   | `/jobs`                    | 创建对齐任务（可选 `anchors` 锚点，状态 `pending`） |
| POST   | `/jobs/{id}/align`         | 执行（锚点约束）DTW 对齐并保存结果     |
| GET    | `/jobs/{id}`               | 查询任务（含完整输入、锚点与结果）     |
| GET    | `/jobs/{id}/result`        | 只查询对齐结果（含已采用锚点）         |
| GET    | `/jobs/{id}/tempo-map`     | 从 matched_beats 导出节奏曲线（tempo events） |
| GET    | `/jobs`                    | 任务列表（摘要，按创建时间倒序）       |
| GET    | `/health`                  | 健康检查                               |

`POST /jobs/{id}/align` 已完成的任务默认直接返回原结果；加 `?force=true`
可强制重新对齐。

### 请求体（`POST /jobs`）

```json
{
  "name": "take-01",
  "reference_beats": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5],
  "performance_onsets": [-0.02, 0.47, 1.53, 1.76, 2.04, 2.52],
  "anchors": [
    {"ref_index": 0, "perf_index": 0},
    {"ref_index": 5, "perf_index": 5}
  ],
  "params": {
    "band_width": 2,
    "skip_penalty": null,
    "insert_penalty": null,
    "on_time_threshold": 0.03
  }
}
```

- `reference_beats` / `performance_onsets`：单位秒，必须**严格递增**
  （允许略小于 0 的前摇时间，例如演奏音比第 0 拍早几十毫秒）。
- `anchors`（可选）：锚点硬约束数组，每项 `{"ref_index", "perf_index"}`
  为 **0 基**索引，表示“该参考拍必须与该演奏音匹配”。
  - 两个索引都必须**严格递增**（锚点按出现顺序排列，不可重复使用某一拍）；
  - 必须在各自序列范围内（`ref_index < len(reference_beats)`、
    `perf_index < len(performance_onsets)`）且非负；
  - 每个锚点必须在带宽内可达：`|ref_index − perf_index| ≤ band_width`；
  - 相邻锚点之间、首锚点到起点、末锚点到终点的漏拍 / 插入拍数量也受带宽约束，
    服务会对以上所有情况校验，不满足时返回 `400` 并给出具体原因
    （越界 / 非递增 / 超出带宽不可达）。
  - 对齐时每个相邻锚点对之间独立做一段 DTW，再按顺序合并为完整路径；
    **漏拍 / 插入拍仍可发生在锚点之间的段内**。对齐结果的 `anchors` 字段
    回传本次实际采用的锚点（含两侧时间、偏差与 early/on_time/late 状态）。
- `params.band_width`：Sakoe-Chiba 带宽（拍数，默认 3）。要求
  `|len(ref) − len(perf)| ≤ band_width`，否则创建时返回 `400`。
  带宽越小越保守（只允许局部错位），越大越能容忍连续漏拍 / 插入拍。
- `params.skip_penalty` / `insert_penalty`：漏 / 插一拍的代价（**秒²**）。
  默认 `(0.5 × 参考网格中位拍间隔)²`（本例拍间隔 0.5s ⇒ 0.0625）。
- `params.on_time_threshold`：`|偏差| ≤` 该秒数判定为 `on_time`（默认 0.03s）。

偏差符号约定：`deviation = perf_time − ref_time`，负值=**抢拍 early**，
正值=**拖拍 late**。

## 示例请求（curl 完整流程）

```bash
# 1) 创建任务（带两个锚点：开头与结尾必须一一对应）
curl -s -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "take-01",
    "reference_beats": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5],
    "performance_onsets": [-0.02, 0.47, 1.53, 1.76, 2.04, 2.52],
    "anchors": [{"ref_index": 0, "perf_index": 0},
                {"ref_index": 5, "perf_index": 5}],
    "params": {"band_width": 2}
  }'
# => 201，记录返回的 "id"（下文以 $ID 表示）；非法锚点会返回 400

# 2) 执行对齐
curl -s -X POST http://localhost:8000/jobs/$ID/align

# 3) 查询结果
curl -s http://localhost:8000/jobs/$ID/result

# 4) 导出节奏曲线（可带窗口与 BPM 范围参数）
curl -s "http://localhost:8000/jobs/$ID/tempo-map?window_beats=5&min_bpm=40&max_bpm=300"

# 5) 列出全部任务
curl -s http://localhost:8000/jobs
```

Python 示例：

```python
import requests

BASE = "http://localhost:8000"

job = requests.post(f"{BASE}/jobs", json={
    "name": "take-01",
    "reference_beats": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5],
    "performance_onsets": [-0.02, 0.47, 1.53, 1.76, 2.04, 2.52],
    "params": {"band_width": 2},
    # 可选：锚点硬约束（参考拍索引、演奏音索引，均 0 基且严格递增）
    "anchors": [{"ref_index": 0, "perf_index": 0},
                {"ref_index": 5, "perf_index": 5}],
}).json()

result = requests.post(f"{BASE}/jobs/{job['id']}/align").json()["result"]

print("已采用锚点:", [(a["ref_index"], a["perf_index"]) for a in result["anchors"]])
for b in result["matched_beats"]:
    print(b["ref_index"], "->", b["perf_index"],
          f"偏差 {b['deviation']:+.2f}s ({b['status']})")
print("漏拍:", [b["ref_index"] for b in result["missed_beats"]])
print("插入拍:", [b["perf_index"] for b in result["inserted_beats"]])
```

## 结果结构（节选）

上例中参考拍 `1.0s`（索引 2）被漏，演奏在 `1.76s`（索引 3）插入了一个音：

```json
{
  "parameters": { /* 实际使用的带宽与惩罚代价 */ },
  "anchors": [
    {"ref_index": 0, "perf_index": 0, "ref_time": 0.0, "perf_time": -0.02,
     "deviation": -0.02, "status": "on_time"},
    {"ref_index": 5, "perf_index": 5, "ref_time": 2.5, "perf_time": 2.52,
     "deviation": 0.02, "status": "on_time"}
  ],
  "matched_beats": [
    {"ref_index": 0, "perf_index": 0, "ref_time": 0.0,  "perf_time": -0.02, "deviation": -0.02, "status": "on_time"},
    {"ref_index": 1, "perf_index": 1, "ref_time": 0.5,  "perf_time": 0.47,  "deviation": -0.03, "status": "early"},
    {"ref_index": 3, "perf_index": 2, "ref_time": 1.5,  "perf_time": 1.53,  "deviation": 0.03,  "status": "late"},
    {"ref_index": 4, "perf_index": 4, "ref_time": 2.0,  "perf_time": 2.04,  "deviation": 0.04,  "status": "late"},
    {"ref_index": 5, "perf_index": 5, "ref_time": 2.5,  "perf_time": 2.52,  "deviation": 0.02,  "status": "on_time"}
  ],
  "missed_beats":   [{"ref_index": 2, "ref_time": 1.0}],
  "inserted_beats": [{"perf_index": 3, "perf_time": 1.76}],
  "deviation_segments": [ /* 参考索引连续的最大匹配段，含段内逐拍偏差 */ ],
  "signed_intervals":   [ /* 段内按 抢/准/拖 归并的同向连续区间 */ ],
  "statistics": {
    "ref_beat_count": 6, "perf_onset_count": 6,
    "matched_count": 5, "missed_count": 1, "inserted_count": 1,
    "early_count": 1, "on_time_count": 2, "late_count": 2,
    "mean_deviation": 0.008, "mean_absolute_deviation": 0.028,
    "rms_deviation": 0.028983, "max_absolute_deviation": 0.04,
    "total_cost": 0.1292, "normalized_cost": 0.018457
  }
}
```

- `anchors`：本次对齐实际采用的锚点（即创建任务时提交、并作为硬约束
  生效的那些点），未提供锚点时为 `[]`。任务对象上另有同名 `anchors`
  字段，保存创建时提交的锚点，便于复查输入。
- `matched_beats`：一一对应映射，含两侧索引、时间、偏差（秒）与状态。
- `missed_beats` / `inserted_beats`：未配对的参考拍 / 演奏音。
- `deviation_segments`：按**参考拍索引连续**切出的最大匹配段（漏拍 /
  插入拍造成缺口时另起一段）。段内偏差是关于参考时间的连续分段线性函数，
  可直接用于绘制偏差曲线或做时间拉伸补偿；含段的起止时间、逐拍偏差及
  min/max/mean。
- `signed_intervals`：在每个连续段内再按 `early/on_time/late` 归并，得到
  **同向连续偏差区间**（例如“第 3–4 拍连续拖拍，平均 +0.035s”）。
- `statistics`：配对数量、抢/准/拖计数、平均偏差、平均绝对偏差、RMS、
  最大绝对偏差、路径总代价与按路径长度归一化的代价。

## 节奏曲线导出（`GET /jobs/{id}/tempo-map`）

对**已完成**的任务，基于对齐结果里的 `matched_beats` 估计局部节奏（BPM），
输出可直接用于时间拉伸 / 重映射的 tempo events（类似 DAW 中的 tempo map）。

查询参数：

| 参数           | 默认 | 说明                                                       |
| -------------- | ---- | ---------------------------------------------------------- |
| `window_beats` | `5`  | 滑动中位数窗口（拍数，取中心 ±`window_beats//2`，1–101）   |
| `min_bpm`      | `40` | 接受的最小 BPM（必须 > 0）                                 |
| `max_bpm`      | `300`| 接受的最大 BPM（必须 > `min_bpm`，否则 422）               |

处理流程：

1. 对 matched_beats 的每个**相邻匹配对**，按参考拍跨度换算原始局部 BPM：
   `bpm = 60 × (ref_index 差) / (perf_time 差)`。正常的漏拍间隔（跨两拍、
   约两倍时长）与插入拍间隔由此自动归一化，不会直接产生半速 / 倍速值；
2. 用以每个间隔为中心的滑动**中位数**窗口求稳健 BPM；原始 BPM 超出
   `[min_bpm, max_bpm]`，或相对局部中位数偏离超过 25%（半速 / 倍速残留）
   的间隔判定为漏拍 / 插入拍造成的异常间隔，其**起始参考拍索引**记入
   `ignored_ref_indices`；
3. 把剩余间隔按稳健 BPM 压缩：相对差不超过 2% 的连续间隔合并为一个
   tempo event；**被忽略的间隔不参与 BPM 计算，也不切断事件**，因此
   异常两侧的同速区间会合并为同一事件。

响应示例：

```json
{
  "job_id": 7,
  "parameters": {
    "window_beats": 5, "min_bpm": 40.0, "max_bpm": 300.0,
    "outlier_tolerance": 0.25, "stable_tolerance": 0.02
  },
  "matched_beat_count": 9,
  "events": [
    {"ref_start_index": 0, "ref_end_index": 4, "start_time": 0.0,
     "ref_time": 0.0, "bpm": 120.0, "beat_count": 4, "gap_count": 4},
    {"ref_start_index": 4, "ref_end_index": 8, "start_time": 2.0,
     "ref_time": 2.0, "bpm": 100.0, "beat_count": 4, "gap_count": 4}
  ],
  "ignored_ref_indices": []
}
```

- `events` 按**参考拍排序**，自 `ref_start_index`（事件起拍）起以 `bpm`
  生效，到下一事件起拍前结束；`start_time` 为起拍对应的**演奏时间**，
  `ref_time` 为参考网格时间。
- `bpm` 为该稳定区间内稳健 BPM 的中位数；`beat_count` 为覆盖的参考拍数
  （`ref_end_index − ref_start_index`，被忽略间隔跨过的漏拍也计入；
  相邻事件共享边界拍，即前一事件结束拍 = 后一事件起拍）。
- 所有间隔都异常时 `events` 为 `[]`、全部索引进 `ignored_ref_indices`。

错误：任务不存在 `404`；任务尚未对齐 `409`；上次对齐失败 `400`；
匹配拍不足 2 个 `409`；参数非法（越界或 `min_bpm >= max_bpm`）`422`。

## 算法说明

设参考拍 `r₀..rₙ₋₁`、演奏音 `p₀..pₘ₋₁`，代价矩阵单元
`D(i,j)` 表示从起点对齐到“参考前 i 拍 / 演奏前 j 音”的最小代价：

```
                 ⎧ D(i−1,j−1) + (p[j−1] − r[i−1])²   匹配（对角）
D(i,j) = min of  ⎨ D(i−1,j)   + skip_penalty          漏拍（水平）
                 ⎩ D(i,j−1)   + insert_penalty         插入拍（垂直）
```

- 只在 Sakoe-Chiba 带 `|i − j| ≤ band_width` 内展开（端点固定为
  `(0,0) → (n,m)`），时间复杂度约 O((n+m)·band_width)，空间 O(n·m)。
- 匹配代价用**时间偏差平方**；漏 / 插代价为常数惩罚，因此只有在时间偏差
  确实比“判为漏拍 + 相邻配对”更划算时才会选择漏 / 插，避免误配。
- 代价同以“秒²”为单位；默认惩罚取 `(0.5 × 中位拍间隔)²`。
- 回填时记录回溯指针（对角/水平/垂直），还原唯一最优路径；代价相同优先
  取对角匹配，保证结果确定。
- 2000 拍、带宽 13 的典型输入在普通笔记本上约 0.3s、50MB 内存。

### 锚点硬约束下的分段 DTW

提供 `anchors = [(r₁,p₁), …, (rₖ,pₖ)]` 时：

1. 先校验锚点非负、不越界、两侧索引严格递增，且每个锚点满足
   `|r−p| ≤ band_width`（在带内即保证与相邻锚点 / 端点之间存在合法路径）；
2. 匹配 `ref[r] ↔ perf[p]` 的对角步落在 DP 网格单元 `(r+1, p+1)`，把这些
   单元连同虚拟端点 `(0,0)`、`(n,m)` 作为分段边界；
3. 每两个相邻边界之间独立运行上述带状 DTW；真实锚点单元**禁止水平 / 垂直
   进入**（只能对角进入），因此锚点一定是一次匹配，构成硬约束；
4. 各子段回溯出的单元路径按序拼接（段端点不重复），再统一还原
   匹配 / 漏拍 / 插入拍事件——漏拍与插入拍可以出现在任意段内，
   总代价为各段代价之和；
5. 结果 `anchors` 字段从最终路径中提取，回传已采用锚点及其偏差与状态。

## 状态码

- `201` 任务创建成功；`200` 对齐 / 查询成功
- `400` 带宽不足无法对齐（含 `|n−m| > band_width` 预检）、惩罚参数非法，
  或锚点校验失败（索引为负 / 越界 / 非严格递增 / 超出带宽不可达），
  `detail` 中会给出具体锚点与原因
- `404` 任务不存在
- `409` 对尚未对齐（`pending`）的任务调用 `/result` 或 `/tempo-map`；
  或 `/tempo-map` 的匹配拍不足 2 个
- `400` `/tempo-map` 作用于上次对齐失败（`error`）的任务
- `422` 请求体校验失败（空序列、非递增、字段类型错误等），
  或 `/tempo-map` 参数非法（`min_bpm >= max_bpm`、`window_beats` 越界）
