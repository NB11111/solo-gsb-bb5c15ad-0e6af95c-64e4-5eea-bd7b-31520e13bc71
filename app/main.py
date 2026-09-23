"""多轨录音节拍对齐 API。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from . import crud
from .alignment import (
    AlignmentError,
    AlignmentParams,
    align_beats,
    validate_anchors,
)
from .database import get_db, init_db
from .review import ReviewError, extract_review_segments
from .schemas import (
    JobCreate,
    JobOut,
    JobSummary,
    ReviewSegmentsOut,
    TempoMapOut,
)
from .tempo import TempoMapError, build_tempo_map


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="多轨录音节拍对齐 API",
    version="1.3.0",
    description=(
        "提交参考节拍网格与演奏轨音符起始时间，服务使用带宽约束的动态时间规整"
        "（Sakoe-Chiba DTW）处理漏拍、抢拍与插入拍，回溯生成一一对应映射及"
        "连续偏差区间。创建任务时可选提供 anchors 锚点（参考拍索引、演奏音索引），"
        "锚点作为硬约束把 DTW 限制在相邻锚点之间分段寻优，合并为完整对齐路径。"
        "对齐完成后可通过 /review-segments 按偏差阈值与可容忍拍间隔把漏拍、"
        "插入音及超阈值偏差聚合成人工复核片段。"
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED, tags=["jobs"])
def create_job(payload: JobCreate, db: sqlite3.Connection = Depends(get_db)):
    """创建对齐任务（创建后状态为 pending，需调用 /align 执行对齐）。

    可提供 ``anchors`` 锚点硬约束：锚点必须严格递增、不越界且在带宽内可达，
    否则返回 400。
    """
    n, m = len(payload.reference_beats), len(payload.performance_onsets)
    if abs(n - m) > payload.params.band_width:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"序列长度差 {abs(n - m)} 超过带宽 {payload.params.band_width}，"
                "请增大 params.band_width"
            ),
        )
    anchor_pairs = (
        [(a.ref_index, a.perf_index) for a in payload.anchors]
        if payload.anchors
        else []
    )
    try:
        validate_anchors(anchor_pairs, n, m, payload.params.band_width)
    except AlignmentError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )
    return crud.create_job(
        db,
        name=payload.name,
        reference_beats=payload.reference_beats,
        performance_onsets=payload.performance_onsets,
        params=payload.params.model_dump(),
        anchors=[
            {"ref_index": a.ref_index, "perf_index": a.perf_index}
            for a in (payload.anchors or [])
        ],
    )


@app.post("/jobs/{job_id}/align", response_model=JobOut, tags=["jobs"])
def align_job(
    job_id: int,
    force: bool = Query(default=False, description="已完成时是否强制重新对齐"),
    db: sqlite3.Connection = Depends(get_db),
):
    """执行 DTW 对齐并持久化结果。"""
    job = crud.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if job["status"] == "done" and not force:
        return job

    params = AlignmentParams(
        band_width=job["params"].get("band_width", 3),
        skip_penalty=job["params"].get("skip_penalty"),
        insert_penalty=job["params"].get("insert_penalty"),
        on_time_threshold=job["params"].get("on_time_threshold", 0.03),
    )
    try:
        result = align_beats(
            job["reference_beats"],
            job["performance_onsets"],
            params,
            anchors=job.get("anchors") or [],
        )
    except AlignmentError as exc:
        crud.save_error(db, job_id, str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    crud.save_result(db, job_id, result)
    return crud.get_job(db, job_id)


@app.get("/jobs", response_model=list[JobSummary], tags=["jobs"])
def list_jobs(
    limit: int = Query(default=100, ge=1, le=500),
    db: sqlite3.Connection = Depends(get_db),
):
    """列出任务摘要。"""
    jobs = crud.list_jobs(db, limit)
    return [
        {
            "id": j["id"],
            "name": j["name"],
            "status": j["status"],
            "created_at": j["created_at"],
            "aligned_at": j["aligned_at"],
            "ref_beat_count": len(j["reference_beats"]),
            "perf_onset_count": len(j["performance_onsets"]),
        }
        for j in jobs
    ]


@app.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
def get_job(job_id: int, db: sqlite3.Connection = Depends(get_db)):
    """查询单个任务（含完整对齐结果）。"""
    job = crud.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return job


@app.get("/jobs/{job_id}/result", tags=["jobs"])
def get_job_result(job_id: int, db: sqlite3.Connection = Depends(get_db)):
    """只查询对齐结果；未对齐或失败时返回 409/400 提示。"""
    job = crud.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if job["status"] == "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="任务尚未对齐")
    if job["status"] == "error":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"上次对齐失败：{job['error']}",
        )
    return {"job_id": job_id, "result": job["result"]}


@app.get(
    "/jobs/{job_id}/tempo-map",
    response_model=TempoMapOut,
    tags=["jobs"],
)
def get_job_tempo_map(
    job_id: int,
    window_beats: int = Query(
        default=5, ge=1, le=101,
        description="局部 BPM 的滑动中位数窗口半径（拍数，取中心±(window_beats//2)）",
    ),
    min_bpm: float = Query(default=40.0, gt=0, description="接受的最小 BPM"),
    max_bpm: float = Query(default=300.0, gt=0, description="接受的最大 BPM"),
    db: sqlite3.Connection = Depends(get_db),
):
    """从已完成任务的 matched_beats 导出节奏曲线（tempo map）。

    先按相邻匹配拍的参考拍跨度与演奏间隔估计局部 BPM，用滑动中位数窗口
    抑制漏拍 / 插入拍造成的异常间隔（半速 / 倍速），再把相对差不超过 2%
    的连续稳定区间压缩为按参考拍排序的 tempo events；异常间隔的起始参考拍
    索引在 ``ignored_ref_indices`` 中返回（不切断事件）。

    - 任务不存在：404
    - 任务尚未对齐：409；上次对齐失败：400
    - 匹配拍不足 2 个：409
    - ``min_bpm >= max_bpm`` 或参数越界：422
    """
    job = crud.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if job["status"] == "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="任务尚未对齐")
    if job["status"] == "error":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"上次对齐失败：{job['error']}",
        )
    if min_bpm >= max_bpm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="min_bpm 必须小于 max_bpm",
        )

    matched = (job["result"] or {}).get("matched_beats", [])
    try:
        tempo_map = build_tempo_map(
            matched,
            window_beats=window_beats,
            min_bpm=min_bpm,
            max_bpm=max_bpm,
        )
    except TempoMapError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {"job_id": job_id, **tempo_map}


@app.get(
    "/jobs/{job_id}/review-segments",
    response_model=ReviewSegmentsOut,
    tags=["jobs"],
)
def get_job_review_segments(
    job_id: int,
    deviation_threshold: float = Query(
        default=0.03, gt=0,
        description="偏差阈值（秒）：|偏差| 严格大于该值的匹配拍列为超阈值偏差问题",
    ),
    gap_beats: float = Query(
        default=1.0, ge=0,
        description=(
            "可容忍的拍间隔（拍数）：按参考拍顺序相邻的两个问题坐标差不超过该值时"
            "聚入同一复核片段，否则断开另起一片段"
        ),
    ),
    db: sqlite3.Connection = Depends(get_db),
):
    """从已完成任务的对齐结果提取人工复核片段。

    将漏拍、插入音与超阈值偏差按参考拍顺序投影到统一的参考拍坐标，
    再用 ``gap_beats`` 聚合成复核片段，返回各片段的拍号范围（1 基、含端点）
    与问题摘要。没有任何问题时返回空 ``segments``。

    - 任务不存在：404
    - 任务尚未对齐：409（明确提示缺少对齐结果）
    - 上次对齐失败：400
    - 参数越界（``deviation_threshold <= 0``、``gap_beats < 0``）：422
    """
    job = crud.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if job["status"] == "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="任务尚未对齐，无对齐结果可供提取复核片段，请先调用 /jobs/{id}/align",
        )
    if job["status"] == "error":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"上次对齐失败：{job['error']}",
        )
    if not job["result"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="任务缺少对齐结果，无法提取复核片段",
        )

    try:
        review = extract_review_segments(
            job["result"],
            job["reference_beats"],
            deviation_threshold=deviation_threshold,
            gap_beats=gap_beats,
        )
    except ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {"job_id": job_id, **review}
