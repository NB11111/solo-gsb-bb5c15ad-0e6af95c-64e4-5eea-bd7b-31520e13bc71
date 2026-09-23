"""请求 / 响应 Pydantic 模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

BeatStatus = Literal["early", "on_time", "late"]


class AlignmentParamsPayload(BaseModel):
    band_width: int = Field(
        default=3, ge=1, le=10000,
        description="Sakoe-Chiba 带宽：允许 |参考索引-演奏索引| 偏离的最大拍数",
    )
    skip_penalty: float | None = Field(
        default=None, ge=0,
        description="漏一拍的代价（秒²）；默认 (0.5×中位拍间隔)²",
    )
    insert_penalty: float | None = Field(
        default=None, ge=0,
        description="插入一拍的代价（秒²）；默认与漏拍代价相同",
    )
    on_time_threshold: float = Field(
        default=0.03, gt=0,
        description="|偏差|≤该秒数判定为准时",
    )


class AnchorPair(BaseModel):
    ref_index: int = Field(
        ..., description="参考拍索引（0 基），锚点表示该参考拍必须与该演奏音匹配"
    )
    perf_index: int = Field(..., description="演奏音索引（0 基）")


class JobCreate(BaseModel):
    name: str | None = Field(default=None, max_length=200, description="任务名称")
    reference_beats: list[float] = Field(
        ..., min_length=1, description="参考节拍网格时间（秒），需严格递增"
    )
    performance_onsets: list[float] = Field(
        ..., min_length=1, description="演奏音符起始时间（秒），需严格递增"
    )
    anchors: list[AnchorPair] | None = Field(
        default=None,
        description=(
            "锚点硬约束（按索引一一对应）：对齐路径必须经过这些 参考拍↔演奏音 匹配点；"
            "参考拍索引与演奏音索引均需严格递增、不越界且在带宽内可达"
        ),
    )
    params: AlignmentParamsPayload = Field(default_factory=AlignmentParamsPayload)

    @field_validator("reference_beats", "performance_onsets")
    @classmethod
    def _strictly_increasing(cls, v: list[float]) -> list[float]:
        # 允许负值（演奏音可能略早于第 0 拍，即前摇），但必须严格递增
        if any(v[i] <= v[i - 1] for i in range(1, len(v))):
            raise ValueError("时间序列必须严格递增")
        return v


class BeatPair(BaseModel):
    ref_index: int
    perf_index: int
    ref_time: float
    perf_time: float
    deviation: float
    status: BeatStatus


class MissedBeat(BaseModel):
    ref_index: int
    ref_time: float


class InsertedBeat(BaseModel):
    perf_index: int
    perf_time: float


class DeviationSegment(BaseModel):
    segment_index: int
    ref_start_index: int
    ref_end_index: int
    ref_start_time: float
    ref_end_time: float
    beat_count: int
    min_deviation: float
    max_deviation: float
    mean_deviation: float
    beats: list[BeatPair]


class SignedInterval(BaseModel):
    segment_index: int
    status: BeatStatus
    ref_start_index: int
    ref_end_index: int
    ref_start_time: float
    ref_end_time: float
    beat_count: int
    min_deviation: float
    max_deviation: float
    mean_deviation: float


class AdoptedAnchor(BaseModel):
    ref_index: int
    perf_index: int
    ref_time: float
    perf_time: float
    deviation: float
    status: BeatStatus


class AlignmentResult(BaseModel):
    parameters: dict[str, Any]
    anchors: list[AdoptedAnchor] = Field(
        default_factory=list,
        description="本次对齐实际采用的锚点（与请求中给出的硬约束一致）",
    )
    matched_beats: list[BeatPair]
    missed_beats: list[MissedBeat]
    inserted_beats: list[InsertedBeat]
    deviation_segments: list[DeviationSegment]
    signed_intervals: list[SignedInterval]
    statistics: dict[str, Any]


class TempoEvent(BaseModel):
    ref_start_index: int = Field(..., description="事件起拍：第一个匹配参考拍的 0 基索引")
    ref_end_index: int = Field(
        ..., description="事件结束拍（含）：区间内最后一个匹配参考拍的 0 基索引"
    )
    start_time: float = Field(..., description="事件起拍对应的演奏时间（秒）")
    ref_time: float = Field(..., description="事件起拍对应的参考时间（秒）")
    bpm: float = Field(..., description="该稳定区间的估计 BPM（区间内稳健 BPM 的中位数）")
    beat_count: int = Field(
        ...,
        description=(
            "覆盖拍数（ref_end_index − ref_start_index），被忽略间隔跨过的漏拍也计入；"
            "相邻事件共享边界拍"
        ),
    )
    gap_count: int = Field(
        ..., description="并入本事件的相邻匹配间隔数（被忽略的异常间隔不计）"
    )


class TempoMapOut(BaseModel):
    job_id: int
    parameters: dict[str, Any]
    matched_beat_count: int = Field(..., description="本次估计使用的匹配拍数")
    events: list[TempoEvent] = Field(
        default_factory=list,
        description="按参考拍排序的 tempo events（稳定 BPM 区间，自该事件起拍生效）",
    )
    ignored_ref_indices: list[int] = Field(
        default_factory=list,
        description="被忽略的异常间隔起始参考拍索引（漏拍 / 插入拍残留或超 BPM 范围）",
    )


class JobSummary(BaseModel):
    id: int
    name: str | None
    status: str
    created_at: str
    aligned_at: str | None
    ref_beat_count: int
    perf_onset_count: int


class JobOut(BaseModel):
    id: int
    name: str | None
    reference_beats: list[float]
    performance_onsets: list[float]
    anchors: list[AnchorPair] = Field(
        default_factory=list, description="任务创建时指定的锚点硬约束"
    )
    params: dict[str, Any]
    status: str
    error: str | None
    created_at: str
    aligned_at: str | None
    result: AlignmentResult | None = None
