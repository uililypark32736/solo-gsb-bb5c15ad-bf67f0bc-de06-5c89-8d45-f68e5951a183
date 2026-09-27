"""Pydantic request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from .separation import DEFAULT_MATRIX, Wake


class FlightIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flight_no: str = Field(..., min_length=1, examples=["CA1234"])
    scheduled_off: datetime = Field(
        ..., description="计划离场时刻，ISO 8601，必须带时区，例如 2026-09-18T08:00:00+08:00"
    )
    wake: Wake = Field(..., description="尾流等级: L 轻型 / M 中型 / H 重型 / J 超重型")
    locked: bool = Field(
        default=False,
        strict=True,
        description="是否为不可移动航班：试排中必须保持 scheduled_off，"
        "缺省为 false。validate 仍按原计划校验并回显锁定状态。",
    )

    @field_validator("flight_no")
    @classmethod
    def _strip_flight_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("flight_no 不能为空")
        return v

    @field_validator("scheduled_off")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("scheduled_off 必须携带时区信息（ISO 8601 offset 或 Z）")
        return v


class ClosureWindowIn(BaseModel):
    """A temporary runway closure window ``[start, end)`` (end excluded)."""

    model_config = ConfigDict(extra="forbid")

    start: datetime = Field(
        ..., description="关闭开始时刻，ISO 8601，必须带时区，例如 2026-09-18T08:10:00+08:00"
    )
    end: datetime = Field(
        ..., description="关闭结束时刻（不含），ISO 8601，必须带时区"
    )

    @field_validator("start", "end")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("关闭窗口时刻必须携带时区信息（ISO 8601 offset 或 Z）")
        return v

    @model_validator(mode="after")
    def _end_after_start(self) -> ClosureWindowIn:
        if self.end <= self.start:
            raise ValueError("关闭窗口结束时刻必须晚于开始时刻（[start, end) 左闭右开）")
        return self


class CapacityIn(BaseModel):
    """A per-runway rolling departure-capacity limit.

    In any half-open span of ``window_seconds`` seconds, at most
    ``max_departures`` flights may depart.
    """

    model_config = ConfigDict(extra="forbid")

    window_seconds: int = Field(
        ...,
        ge=1,
        strict=True,
        description="滚动窗口长度（秒），正整数",
    )
    max_departures: int = Field(
        ...,
        ge=1,
        strict=True,
        description="窗口内最多离场架次，正整数",
    )


class RunwayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runway: str = Field(..., min_length=1, examples=["36L"])
    flights: list[FlightIn] = Field(..., min_length=1)
    closures: list[ClosureWindowIn] = Field(
        default_factory=list,
        description="本跑道的临时关闭窗口列表，[start, end) 左闭右开，彼此不得重叠。",
    )
    capacity: CapacityIn | None = Field(
        default=None,
        description="本跑道可选的滚动离场容量约束：任意 window_seconds 秒的半开区间内"
        "离场不得超过 max_departures 架；缺省表示不施加容量约束。",
    )

    @field_validator("runway")
    @classmethod
    def _strip_runway(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("runway 不能为空")
        return v

    @model_validator(mode="after")
    def _validate_runway(self) -> RunwayIn:
        seen: set[str] = set()
        for f in self.flights:
            key = f.flight_no
            if key in seen:
                raise ValueError(f"跑道 {self.runway} 内航班号重复: {key}")
            seen.add(key)
        # closures are half-open [start, end): touching is allowed, overlap is not
        ordered = sorted(self.closures, key=lambda w: (w.start, w.end))
        for prev, nxt in zip(ordered, ordered[1:]):
            if nxt.start < prev.end:
                raise ValueError(
                    f"跑道 {self.runway} 的关闭窗口重叠: "
                    f"[{prev.start.isoformat()}, {prev.end.isoformat()}) 与 "
                    f"[{nxt.start.isoformat()}, {nxt.end.isoformat()})"
                )
        return self


class MatrixEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leader: Wake
    follower: Wake
    seconds: int = Field(..., ge=0, description="前机—后机最小雷达间隔（秒）")


class WakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runways: list[RunwayIn] = Field(..., min_length=1)
    matrix: list[MatrixEntryIn] | None = Field(
        default=None,
        description="前机—后机最小间隔矩阵；缺省使用内置 ICAO 风格默认矩阵。"
        "提供时仅列出的组合生效，未列出的组合视为无最小间隔(0 秒)。",
    )

    @model_validator(mode="after")
    def _validate_request(self) -> WakeRequest:
        runway_names: set[str] = set()
        global_flights: set[str] = set()
        for rw in self.runways:
            if rw.runway in runway_names:
                raise ValueError(f"跑道标识重复: {rw.runway}")
            runway_names.add(rw.runway)
            for f in rw.flights:
                if f.flight_no in global_flights:
                    raise ValueError(f"航班号在整个请求中必须唯一: {f.flight_no}")
                global_flights.add(f.flight_no)
        if self.matrix is not None:
            pairs: set[tuple[str, str]] = set()
            for entry in self.matrix:
                pair = (entry.leader.value, entry.follower.value)
                if pair in pairs:
                    raise ValueError(
                        f"间隔矩阵组合重复: leader={pair[0]} follower={pair[1]}"
                    )
                pairs.add(pair)
        return self


class MutexGroupIn(BaseModel):
    """一组互斥（相交）跑道：组内任一跑道的两次相邻离场之间必须保持
    ``min_interval_seconds`` 秒的最小占用间隔。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        description="组名（可选，仅用于回显与结果标注）；缺省按组在请求中的顺序"
        "命名为 group-1、group-2 …",
    )
    runways: list[str] = Field(
        ...,
        min_length=2,
        description="互斥跑道标识列表，至少 2 条；同一条跑道至多属于一个组。",
    )
    min_interval_seconds: int = Field(
        ...,
        ge=1,
        strict=True,
        description="组内两次相邻离场的最小占用间隔（秒），正整数",
    )

    @field_validator("runways")
    @classmethod
    def _strip_runway_names(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not v for v in cleaned):
            raise ValueError("互斥跑道组的跑道标识不能为空")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError(f"互斥跑道组内跑道标识重复: {cleaned}")
        return cleaned

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class JointTrialRequest(WakeRequest):
    """联合试排请求：在 validate/trial 同一请求结构上增加互斥跑道组。"""

    model_config = ConfigDict(extra="forbid")

    groups: list[MutexGroupIn] | None = Field(
        default=None,
        description="互斥跑道组列表（可选）；组内任意跑道的两次相邻离场之间必须"
        "保持 min_interval_seconds 秒。同一条跑道至多属于一个组。",
    )

    @model_validator(mode="after")
    def _validate_groups(self) -> JointTrialRequest:
        if not self.groups:
            return self
        runway_names = {rw.runway for rw in self.runways}
        assigned: set[str] = set()
        group_names: set[str] = set()
        for index, group in enumerate(self.groups, start=1):
            name = group.name or f"group-{index}"
            if name in group_names:
                raise ValueError(f"互斥跑道组名称重复: {name}")
            group_names.add(name)
            for runway in group.runways:
                if runway not in runway_names:
                    raise ValueError(
                        f"互斥跑道组 {name} 引用了请求中不存在的跑道: {runway}"
                    )
                if runway in assigned:
                    raise ValueError(
                        f"跑道 {runway} 同时属于多个互斥跑道组；"
                        "每条跑道至多可属于一个组"
                    )
                assigned.add(runway)
        return self

    def group_display_names(self) -> list[str]:
        """Canonical group names in request order (missing names auto-filled)."""
        if not self.groups:
            return []
        return [
            group.name or f"group-{index}"
            for index, group in enumerate(self.groups, start=1)
        ]


class CandidateIn(BaseModel):
    """The single candidate flight to insert into a runway's schedule."""

    model_config = ConfigDict(extra="forbid")

    flight_no: str = Field(..., min_length=1, examples=["CA1999"])
    wake: Wake = Field(..., description="候选航班尾流等级: L 轻型 / M 中型 / H 重型 / J 超重型")
    earliest_off: datetime = Field(
        ..., description="候选最早离场时刻（含），ISO 8601，必须带时区且精确到整秒"
    )
    latest_off: datetime = Field(
        ..., description="候选最晚离场时刻（含），ISO 8601，必须带时区且精确到整秒"
    )

    @field_validator("flight_no")
    @classmethod
    def _strip_flight_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("flight_no 不能为空")
        return v

    @field_validator("earliest_off", "latest_off")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("候选最早/最晚离场时刻必须携带时区信息（ISO 8601 offset 或 Z）")
        return v


class SlotInsertRequest(BaseModel):
    """单跑道候选插入请求：现有计划 + 关闭窗口 + 滚动容量 + 间隔矩阵 + 候选航班。

    所有时刻必须精确到整秒；候选可离场区间为
    ``[earliest_off, latest_off]`` 闭区间（含两端）。
    """

    model_config = ConfigDict(extra="forbid")

    runway: RunwayIn = Field(..., description="单条跑道的现有航班、关闭窗口与滚动容量")
    candidate: CandidateIn = Field(..., description="待插入的候选航班")
    matrix: list[MatrixEntryIn] | None = Field(
        default=None,
        description="前机—后机最小间隔矩阵；缺省使用内置 ICAO 风格默认矩阵。"
        "提供时仅列出的组合生效，未列出的组合视为无最小间隔(0 秒)。",
    )

    @model_validator(mode="after")
    def _validate_insert(self) -> SlotInsertRequest:
        existing = {f.flight_no for f in self.runway.flights}
        if self.candidate.flight_no in existing:
            raise ValueError(
                f"候选航班号与现有航班重复: {self.candidate.flight_no}（候选号须唯一）"
            )
        if self.candidate.earliest_off > self.candidate.latest_off:
            raise ValueError("候选最早离场时刻不能晚于最晚离场时刻（区间含两端）")
        for label, moment in self._all_moments():
            if moment.microsecond != 0:
                raise ValueError(f"时刻必须精确到整秒（不允许毫秒/微秒）: {label}")
        if self.matrix is not None:
            pairs: set[tuple[str, str]] = set()
            for entry in self.matrix:
                pair = (entry.leader.value, entry.follower.value)
                if pair in pairs:
                    raise ValueError(
                        f"间隔矩阵组合重复: leader={pair[0]} follower={pair[1]}"
                    )
                pairs.add(pair)
        return self

    def _all_moments(self) -> list[tuple[str, datetime]]:
        moments: list[tuple[str, datetime]] = [
            (f"航班 {f.flight_no} 的 scheduled_off", f.scheduled_off)
            for f in self.runway.flights
        ]
        moments += [
            (f"关闭窗口 {label}", moment)
            for w in self.runway.closures
            for label, moment in (("start", w.start), ("end", w.end))
        ]
        moments.append(("候选 earliest_off", self.candidate.earliest_off))
        moments.append(("候选 latest_off", self.candidate.latest_off))
        return moments


# ---------- response models ----------


class ClosureWindowOut(BaseModel):
    start: datetime
    end: datetime


class CapacityOut(BaseModel):
    window_seconds: int
    max_departures: int


class CapacityViolationOut(BaseModel):
    flight_no: str
    wake: Wake
    scheduled_off: datetime
    position: int
    window_seconds: int
    max_departures: int
    departures_in_window: int
    anchor_flight_no: str
    anchor_position: int
    window_start: datetime
    window_end: datetime
    earliest_compliant_off: datetime


class ClosureConflictOut(BaseModel):
    flight_no: str
    wake: Wake
    scheduled_off: datetime
    position: int
    window_index: int
    window_start: datetime
    window_end: datetime


class ViolationOut(BaseModel):
    leader: str
    follower: str
    leader_wake: Wake
    follower_wake: Wake
    leader_scheduled_off: datetime
    follower_scheduled_off: datetime
    required_seconds: int
    actual_seconds: int
    shortfall_seconds: int
    leader_position: int
    follower_position: int


class LockedFlightOut(BaseModel):
    position: int
    flight_no: str
    scheduled_off: datetime
    wake: Wake


class TrialBlockedOut(BaseModel):
    """First locked flight the trial cannot keep on its scheduled time."""

    flight_no: str
    wake: Wake
    scheduled_off: datetime
    position: int
    constraint: Literal["wake", "closure", "capacity"]
    required_earliest_off: datetime
    # wake detail
    triggered_by: str | None
    required_gap_seconds: int | None
    # closure detail
    closure_hit: bool
    closure_window: ClosureWindowOut | None
    # capacity detail (present only when the runway carried a capacity config)
    has_capacity: bool = Field(default=False, exclude=True)
    capacity_hit: bool = False
    capacity_window_seconds: int | None = None
    capacity_max_departures: int | None = None
    capacity_anchor: str | None = None
    capacity_window: ClosureWindowOut | None = None

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data = {
            "flight_no": self.flight_no,
            "wake": self.wake,
            "scheduled_off": self.scheduled_off,
            "position": self.position,
            "constraint": self.constraint,
            "required_earliest_off": self.required_earliest_off,
            "triggered_by": self.triggered_by,
            "required_gap_seconds": self.required_gap_seconds,
            "closure_hit": self.closure_hit,
            "closure_window": (
                self.closure_window.model_dump()
                if self.closure_window is not None
                else None
            ),
        }
        if self.has_capacity:
            data["capacity_hit"] = self.capacity_hit
            data["capacity_window_seconds"] = self.capacity_window_seconds
            data["capacity_max_departures"] = self.capacity_max_departures
            data["capacity_anchor"] = self.capacity_anchor
            data["capacity_window"] = (
                self.capacity_window.model_dump()
                if self.capacity_window is not None
                else None
            )
        return data


class RunwayReportOut(BaseModel):
    runway: str
    order: list[str]
    total: int
    closures: list[ClosureWindowOut]
    violation_count: int
    violations: list[ViolationOut]
    closure_conflict_count: int
    closure_conflicts: list[ClosureConflictOut]
    # Capacity fields are emitted only when the runway carried a capacity
    # config, so legacy request/response shapes stay byte-compatible.
    has_capacity: bool = Field(default=False, exclude=True)
    capacity: CapacityOut | None = None
    capacity_violation_count: int = 0
    capacity_violations: list[CapacityViolationOut] = Field(default_factory=list)
    # Lock detail is emitted only when the request explicitly carried the
    # ``locked`` field on at least one flight of this runway.
    has_locks: bool = Field(default=False, exclude=True)
    locked_flights: list[LockedFlightOut] = Field(default_factory=list)

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "runway": self.runway,
            "order": self.order,
            "total": self.total,
            "closures": [w.model_dump() for w in self.closures],
            "violation_count": self.violation_count,
            "violations": [v.model_dump(mode="json") for v in self.violations],
            "closure_conflict_count": self.closure_conflict_count,
            "closure_conflicts": [
                c.model_dump(mode="json") for c in self.closure_conflicts
            ],
        }
        if self.has_capacity:
            data["capacity"] = (
                self.capacity.model_dump() if self.capacity is not None else None
            )
            data["capacity_violation_count"] = self.capacity_violation_count
            data["capacity_violations"] = [
                v.model_dump(mode="json") for v in self.capacity_violations
            ]
        if self.has_locks:
            data["locked_flights"] = [
                f.model_dump(mode="json") for f in self.locked_flights
            ]
        return data


class ValidateResponse(BaseModel):
    batch_id: str
    mode: str = "validate"
    matrix: dict[str, dict[str, int]]
    total_runways: int
    total_flights: int
    total_violations: int
    total_closure_conflicts: int
    runways: list[RunwayReportOut]
    # Same conditional rule as the per-runway capacity block: the summary is
    # present only when at least one runway in the batch had a capacity config.
    has_capacity: bool = Field(default=False, exclude=True)
    total_capacity_violations: int = 0
    # Present only when at least one flight in the batch explicitly carried
    # the ``locked`` field.
    has_locks: bool = Field(default=False, exclude=True)
    total_locked: int = 0

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "batch_id": self.batch_id,
            "mode": self.mode,
            "matrix": self.matrix,
            "total_runways": self.total_runways,
            "total_flights": self.total_flights,
            "total_violations": self.total_violations,
            "total_closure_conflicts": self.total_closure_conflicts,
            "runways": [rw.model_dump(mode="json") for rw in self.runways],
        }
        if self.has_capacity:
            data["total_capacity_violations"] = self.total_capacity_violations
        if self.has_locks:
            data["total_locked"] = self.total_locked
        return data


class TrialItemOut(BaseModel):
    position: int
    flight_no: str
    wake: Wake
    scheduled_off: datetime
    adjusted_off: datetime
    delay_seconds: int
    triggered_by: str | None
    closure_hit: bool
    closure_window: ClosureWindowOut | None
    # Echoed only when the request explicitly carried the ``locked`` field.
    has_locks: bool = Field(default=False, exclude=True)
    locked: bool = False
    # Capacity detail is present only when the runway carried a capacity config.
    has_capacity: bool = Field(default=False, exclude=True)
    capacity_hit: bool = False
    capacity_window_seconds: int | None = None
    capacity_max_departures: int | None = None
    capacity_anchor: str | None = None
    capacity_window: ClosureWindowOut | None = None

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "position": self.position,
            "flight_no": self.flight_no,
            "wake": self.wake,
            "scheduled_off": self.scheduled_off,
            "adjusted_off": self.adjusted_off,
            "delay_seconds": self.delay_seconds,
            "triggered_by": self.triggered_by,
            "closure_hit": self.closure_hit,
            "closure_window": (
                self.closure_window.model_dump()
                if self.closure_window is not None
                else None
            ),
        }
        if self.has_capacity:
            data["capacity_hit"] = self.capacity_hit
            data["capacity_window_seconds"] = self.capacity_window_seconds
            data["capacity_max_departures"] = self.capacity_max_departures
            data["capacity_anchor"] = self.capacity_anchor
            data["capacity_window"] = (
                self.capacity_window.model_dump()
                if self.capacity_window is not None
                else None
            )
        if self.has_locks:
            data["locked"] = self.locked
        return data


class RunwayTrialOut(BaseModel):
    runway: str
    order: list[str]
    total: int
    closures: list[ClosureWindowOut]
    delayed_count: int
    total_delay_seconds: int
    max_delay_seconds: int
    items: list[TrialItemOut]
    has_capacity: bool = Field(default=False, exclude=True)
    capacity: CapacityOut | None = None
    has_locks: bool = Field(default=False, exclude=True)
    locked_flights: list[LockedFlightOut] = Field(default_factory=list)
    # Set only when the first locked flight that cannot keep its scheduled
    # time was reached; ``items`` then holds just the feasible prefix.
    blocked: TrialBlockedOut | None = None

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "runway": self.runway,
            "order": self.order,
            "total": self.total,
            "closures": [w.model_dump() for w in self.closures],
            "delayed_count": self.delayed_count,
            "total_delay_seconds": self.total_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "items": [i.model_dump(mode="json") for i in self.items],
        }
        if self.has_capacity:
            data["capacity"] = (
                self.capacity.model_dump() if self.capacity is not None else None
            )
        if self.has_locks:
            data["locked_flights"] = [
                f.model_dump(mode="json") for f in self.locked_flights
            ]
            data["blocked"] = (
                self.blocked.model_dump(mode="json")
                if self.blocked is not None
                else None
            )
        return data


class TrialResponse(BaseModel):
    batch_id: str
    mode: str = "trial"
    matrix: dict[str, dict[str, int]]
    total_runways: int
    total_flights: int
    total_delayed: int
    total_delay_seconds: int
    runways: list[RunwayTrialOut]
    has_locks: bool = Field(default=False, exclude=True)
    total_locked: int = 0

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "batch_id": self.batch_id,
            "mode": self.mode,
            "matrix": self.matrix,
            "total_runways": self.total_runways,
            "total_flights": self.total_flights,
            "total_delayed": self.total_delayed,
            "total_delay_seconds": self.total_delay_seconds,
            "runways": [rw.model_dump(mode="json") for rw in self.runways],
        }
        if self.has_locks:
            data["total_locked"] = self.total_locked
        return data


class SwapChoiceOut(BaseModel):
    """The chosen adjacent swap; positions refer to the baseline order."""

    position: int
    first_flight_no: str
    second_flight_no: str


class BaselineMetricsOut(BaseModel):
    """Delay metrics of the baseline (stable-order) schedule."""

    delayed_count: int
    total_delay_seconds: int
    max_delay_seconds: int


class RunwaySwapOut(BaseModel):
    """One runway's best-adjacent-swap optimisation result."""

    runway: str
    order: list[str]  # optimised departure order; item positions refer to it
    baseline_order: list[str]  # stable order the enumeration started from
    total: int
    closures: list[ClosureWindowOut]
    baseline: BaselineMetricsOut
    chosen_swap: SwapChoiceOut | None  # None -> baseline order already best
    saved_seconds: int
    delayed_count: int
    total_delay_seconds: int
    max_delay_seconds: int
    items: list[TrialItemOut]
    has_capacity: bool = Field(default=False, exclude=True)
    capacity: CapacityOut | None = None

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "runway": self.runway,
            "order": self.order,
            "baseline_order": self.baseline_order,
            "total": self.total,
            "closures": [w.model_dump() for w in self.closures],
            "baseline": self.baseline.model_dump(mode="json"),
            "chosen_swap": (
                self.chosen_swap.model_dump()
                if self.chosen_swap is not None
                else None
            ),
            "saved_seconds": self.saved_seconds,
            "delayed_count": self.delayed_count,
            "total_delay_seconds": self.total_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "items": [i.model_dump(mode="json") for i in self.items],
        }
        if self.has_capacity:
            data["capacity"] = (
                self.capacity.model_dump() if self.capacity is not None else None
            )
        return data


class BestAdjacentSwapResponse(BaseModel):
    batch_id: str
    mode: str = "best_adjacent_swap"
    matrix: dict[str, dict[str, int]]
    total_runways: int
    total_flights: int
    total_baseline_delay_seconds: int
    total_delay_seconds: int
    total_saved_seconds: int
    runways: list[RunwaySwapOut]


class SlotInsertResponse(BaseModel):
    """候选插入结果：可行时给出最早可行 UTC 时刻与插入位次，否则给出无空档原因。"""

    runway: str
    candidate_flight_no: str
    candidate_wake: Wake
    earliest_off: datetime = Field(..., description="候选最早离场时刻（UTC 回显）")
    latest_off: datetime = Field(..., description="候选最晚离场时刻（UTC 回显）")
    feasible: bool
    reason: str
    # 满足全部约束的全局最早时刻；可行时与 assigned_off 相同，
    # 无可行空档时该时刻晚于 latest_off。
    earliest_feasible_off: datetime
    # 以下字段仅在 feasible=true 时给出，否则为 null。
    insert_position: int | None = Field(
        default=None, description="插入后在全序列中的位次（1 基，按 离场时刻, 航班号 排序）"
    )
    assigned_off: datetime | None = Field(
        default=None, description="区间内最早可行的 UTC 离场时刻"
    )
    inserted_order: list[str] | None = Field(
        default=None, description="插入后的完整离场序列（航班号）"
    )
    matrix: dict[str, dict[str, int]]


class MutexGroupOut(BaseModel):
    name: str
    runways: list[str]
    min_interval_seconds: int


class RequiredWakeOut(BaseModel):
    required_off: datetime
    triggered_by: str
    required_gap_seconds: int


class RequiredClosureOut(BaseModel):
    required_off: datetime
    window: ClosureWindowOut


class RequiredCapacityOut(BaseModel):
    required_off: datetime
    anchor_flight_no: str
    window_seconds: int
    max_departures: int
    window: ClosureWindowOut


class RequiredMutexGroupOut(BaseModel):
    required_off: datetime
    triggered_by: str
    group: str
    min_interval_seconds: int
    window: ClosureWindowOut


class JointRequiredTimesOut(BaseModel):
    """Per-constraint earliest times for a blocked flight; inapplicable
    constraints are ``None`` (e.g. no predecessor, no group membership)."""

    wake: RequiredWakeOut | None = None
    closure: RequiredClosureOut | None = None
    capacity: RequiredCapacityOut | None = None
    mutex_group: RequiredMutexGroupOut | None = None


class JointTrialItemOut(BaseModel):
    position: int
    runway_position: int
    runway: str
    flight_no: str
    wake: Wake
    scheduled_off: datetime
    adjusted_off: datetime
    delay_seconds: int
    # First constraint that demanded a push (wake/closure/capacity/mutex_group);
    # null when the flight departs on schedule.
    constraint: Literal["wake", "closure", "capacity", "mutex_group"] | None
    triggered_by: str | None
    closure_hit: bool
    closure_window: ClosureWindowOut | None
    has_locks: bool = Field(default=False, exclude=True)
    locked: bool = False
    has_capacity: bool = Field(default=False, exclude=True)
    capacity_hit: bool = False
    capacity_window_seconds: int | None = None
    capacity_max_departures: int | None = None
    capacity_anchor: str | None = None
    capacity_window: ClosureWindowOut | None = None
    in_group: bool = Field(default=False, exclude=True)
    mutex_group_hit: bool = False
    mutex_group: str | None = None
    mutex_group_anchor: str | None = None
    mutex_group_min_interval_seconds: int | None = None
    mutex_group_window: ClosureWindowOut | None = None

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "position": self.position,
            "runway_position": self.runway_position,
            "runway": self.runway,
            "flight_no": self.flight_no,
            "wake": self.wake,
            "scheduled_off": self.scheduled_off,
            "adjusted_off": self.adjusted_off,
            "delay_seconds": self.delay_seconds,
            "constraint": self.constraint,
            "triggered_by": self.triggered_by,
            "closure_hit": self.closure_hit,
            "closure_window": (
                self.closure_window.model_dump()
                if self.closure_window is not None
                else None
            ),
        }
        if self.has_locks:
            data["locked"] = self.locked
        if self.has_capacity:
            data["capacity_hit"] = self.capacity_hit
            data["capacity_window_seconds"] = self.capacity_window_seconds
            data["capacity_max_departures"] = self.capacity_max_departures
            data["capacity_anchor"] = self.capacity_anchor
            data["capacity_window"] = (
                self.capacity_window.model_dump()
                if self.capacity_window is not None
                else None
            )
        if self.in_group:
            data["mutex_group_hit"] = self.mutex_group_hit
            data["mutex_group"] = self.mutex_group
            data["mutex_group_anchor"] = self.mutex_group_anchor
            data["mutex_group_min_interval_seconds"] = (
                self.mutex_group_min_interval_seconds
            )
            data["mutex_group_window"] = (
                self.mutex_group_window.model_dump()
                if self.mutex_group_window is not None
                else None
            )
        return data


class JointBlockedOut(BaseModel):
    position: int
    runway_position: int
    runway: str
    flight_no: str
    wake: Wake
    scheduled_off: datetime
    constraint: Literal["wake", "closure", "capacity", "mutex_group"]
    required_earliest_off: datetime
    # wake detail
    triggered_by: str | None
    required_gap_seconds: int | None
    # closure detail
    closure_hit: bool
    closure_window: ClosureWindowOut | None
    # per-constraint earliest times
    required_times: JointRequiredTimesOut
    has_capacity: bool = Field(default=False, exclude=True)
    capacity_hit: bool = False
    capacity_window_seconds: int | None = None
    capacity_max_departures: int | None = None
    capacity_anchor: str | None = None
    capacity_window: ClosureWindowOut | None = None
    in_group: bool = Field(default=False, exclude=True)
    mutex_group_hit: bool = False
    mutex_group: str | None = None
    mutex_group_anchor: str | None = None
    mutex_group_min_interval_seconds: int | None = None
    mutex_group_window: ClosureWindowOut | None = None

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "position": self.position,
            "runway_position": self.runway_position,
            "runway": self.runway,
            "flight_no": self.flight_no,
            "wake": self.wake,
            "scheduled_off": self.scheduled_off,
            "constraint": self.constraint,
            "required_earliest_off": self.required_earliest_off,
            "triggered_by": self.triggered_by,
            "required_gap_seconds": self.required_gap_seconds,
            "closure_hit": self.closure_hit,
            "closure_window": (
                self.closure_window.model_dump()
                if self.closure_window is not None
                else None
            ),
            "required_times": self.required_times.model_dump(mode="json"),
        }
        if self.has_capacity:
            data["capacity_hit"] = self.capacity_hit
            data["capacity_window_seconds"] = self.capacity_window_seconds
            data["capacity_max_departures"] = self.capacity_max_departures
            data["capacity_anchor"] = self.capacity_anchor
            data["capacity_window"] = (
                self.capacity_window.model_dump()
                if self.capacity_window is not None
                else None
            )
        if self.in_group:
            data["mutex_group_hit"] = self.mutex_group_hit
            data["mutex_group"] = self.mutex_group
            data["mutex_group_anchor"] = self.mutex_group_anchor
            data["mutex_group_min_interval_seconds"] = (
                self.mutex_group_min_interval_seconds
            )
            data["mutex_group_window"] = (
                self.mutex_group_window.model_dump()
                if self.mutex_group_window is not None
                else None
            )
        return data


class RunwayJointOut(BaseModel):
    runway: str
    total: int
    closures: list[ClosureWindowOut]
    delayed_count: int
    total_delay_seconds: int
    max_delay_seconds: int
    items: list[JointTrialItemOut]
    has_capacity: bool = Field(default=False, exclude=True)
    capacity: CapacityOut | None = None
    in_group: bool = Field(default=False, exclude=True)
    mutex_group: str | None = None
    has_locks: bool = Field(default=False, exclude=True)
    locked_flights: list[LockedFlightOut] = Field(default_factory=list)
    # Set only for the runway holding the first infeasible locked flight;
    # every runway's ``items`` then holds just the feasible global prefix
    # restricted to that runway.
    blocked: JointBlockedOut | None = None

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "runway": self.runway,
            "total": self.total,
            "closures": [w.model_dump() for w in self.closures],
            "delayed_count": self.delayed_count,
            "total_delay_seconds": self.total_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "items": [i.model_dump(mode="json") for i in self.items],
        }
        if self.has_capacity:
            data["capacity"] = (
                self.capacity.model_dump() if self.capacity is not None else None
            )
        if self.in_group:
            data["mutex_group"] = self.mutex_group
        if self.has_locks:
            data["locked_flights"] = [
                f.model_dump(mode="json") for f in self.locked_flights
            ]
            data["blocked"] = (
                self.blocked.model_dump(mode="json")
                if self.blocked is not None
                else None
            )
        return data


class JointTrialResponse(BaseModel):
    batch_id: str
    mode: str = "joint_trial"
    matrix: dict[str, dict[str, int]]
    total_runways: int
    total_flights: int
    groups: list[MutexGroupOut]
    # global stable order as (runway, flight_no) pairs
    order: list[dict]
    total_delayed: int
    total_delay_seconds: int
    max_delay_seconds: int
    runways: list[RunwayJointOut]
    has_locks: bool = Field(default=False, exclude=True)
    total_locked: int = 0

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data: dict = {
            "batch_id": self.batch_id,
            "mode": self.mode,
            "matrix": self.matrix,
            "total_runways": self.total_runways,
            "total_flights": self.total_flights,
            "groups": [g.model_dump() for g in self.groups],
            "order": self.order,
            "total_delayed": self.total_delayed,
            "total_delay_seconds": self.total_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "runways": [rw.model_dump(mode="json") for rw in self.runways],
        }
        if self.has_locks:
            data["total_locked"] = self.total_locked
        return data


class ConflictGroupFlightOut(BaseModel):
    """A flight inside one departure-conflict group."""

    position: int = Field(..., description="航班在本跑道稳定排序中的位次（1 基）")
    flight_no: str
    wake: Wake
    scheduled_off: datetime


class ConflictGroupOut(BaseModel):
    """One connected component of a runway's departure conflicts."""

    group_index: int = Field(..., description="组在本跑道内的序号（1 基，按最早航班排序）")
    flights: list[ConflictGroupFlightOut] = Field(
        ..., description="组内航班，按 (计划时刻, 航班号) 排序"
    )
    wake_conflict_count: int = Field(..., description="组内尾流间隔违规数量")
    capacity_conflict_count: int = Field(..., description="组内容量超限数量")
    closure_conflict_count: int = Field(..., description="组内关闭窗口冲突数量")


class RunwayConflictGroupsOut(BaseModel):
    runway: str
    group_count: int
    groups: list[ConflictGroupOut]


class ConflictGroupsResponse(BaseModel):
    """Departure-conflict groups of a saved validate batch (read-only)."""

    batch_id: str
    mode: str = "validate"
    created_at: datetime
    total_runways: int
    total_groups: int
    runways: list[RunwayConflictGroupsOut]


class MatrixOut(BaseModel):
    matrix: dict[str, dict[str, int]]


class HealthOut(BaseModel):
    status: str


class BatchResponse(BaseModel):
    """Stored batch (either mode) fetched by id; summary fields are mode-specific."""

    batch_id: str
    mode: str
    created_at: datetime
    matrix: dict[str, dict[str, int]]
    total_runways: int
    total_flights: int
    total_violations: int | None = None
    total_closure_conflicts: int | None = None
    total_capacity_violations: int | None = None
    total_delayed: int | None = None
    total_delay_seconds: int | None = None
    total_locked: int | None = None
    total_baseline_delay_seconds: int | None = None
    total_saved_seconds: int | None = None
    # joint_trial only
    max_delay_seconds: int | None = None
    groups: list[dict] | None = None
    order: list[dict] | None = None
    runways: list[dict]

    @model_serializer(mode="plain")
    def _serialize(self) -> dict:
        data = {
            "batch_id": self.batch_id,
            "mode": self.mode,
            "created_at": self.created_at,
            "matrix": self.matrix,
            "total_runways": self.total_runways,
            "total_flights": self.total_flights,
            "runways": self.runways,
        }
        for name in (
            "total_violations",
            "total_closure_conflicts",
            "total_capacity_violations",
            "total_delayed",
            "total_delay_seconds",
            "total_locked",
            "total_baseline_delay_seconds",
            "total_saved_seconds",
            "max_delay_seconds",
            "groups",
            "order",
        ):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data


def matrix_to_nested(flat: dict[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    """Convert the internal flat matrix to a leader->follower nested dict."""
    nested: dict[str, dict[str, int]] = {}
    for (leader, follower), seconds in flat.items():
        nested.setdefault(leader, {})[follower] = seconds
    return nested


def default_matrix_nested() -> dict[str, dict[str, int]]:
    return {k: dict(v) for k, v in DEFAULT_MATRIX.items()}
