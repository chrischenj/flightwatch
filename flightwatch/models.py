"""共享数据模型（Lead 独占维护，其他人不要改这个文件的公共字段）。

约定:
  * cabin 用字符串: ECONOMY / PREMIUM_ECONOMY / BUSINESS / FIRST_CLASS
  * 价格是单程、单人、含税总价（float，单位见 currency）
  * 时间用带时区的 ISO 字符串（各段为本地时间），保证 JSON 可序列化
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

CABINS = ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST_CLASS")
CABIN_CN = {
    "ECONOMY": "经济舱",
    "PREMIUM_ECONOMY": "超级经济舱",
    "BUSINESS": "公务舱",
    "FIRST_CLASS": "头等舱",
}
# 舒适度权重（用于合意度计算）
CABIN_COMFORT = {"ECONOMY": 0.0, "PREMIUM_ECONOMY": 0.45, "BUSINESS": 1.0, "FIRST_CLASS": 1.25}


def parse_dt(s: str) -> datetime:
    """解析 'YYYY-MM-DDTHH:MM:SS'（无时区）或带偏移的 ISO 时间。"""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def minutes_between(a: str, b: str) -> float:
    return (parse_dt(b) - parse_dt(a)).total_seconds() / 60.0


@dataclass
class Segment:
    carrier: str
    flight_no: str
    origin: str            # 机场 IATA
    destination: str
    depart_local: str      # 本地时间
    arrive_local: str
    duration_min: int
    cabin: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Leg:
    """一个「票段」：一张票覆盖 1..n 个航段（可能是自助中转联程）。"""
    origin: str                 # 城市或机场代码
    destination: str
    depart_local: str
    arrive_local: str
    duration_min: int
    price: float
    currency: str
    cabin: str                  # 该票整体的最低舱位
    segments: list[Segment] = field(default_factory=list)
    provider: str = ""
    booking_url: Optional[str] = None
    stops: int = 0
    self_transfer: bool = False
    booking_hint: Optional[str] = None
    baggage_note: str = ""

    @property
    def cabins(self) -> set[str]:
        return {s.cabin for s in self.segments} or {self.cabin}

    @property
    def departure_hour(self) -> int:
        try:
            return parse_dt(self.depart_local).hour
        except Exception:  # noqa: BLE001
            return 12

    def to_dict(self) -> dict:
        d = asdict(self)
        d["segments"] = [s.to_dict() for s in self.segments]
        return d


@dataclass
class LegChoice:
    """拆分方案里的一段：给出每种舱位的候选报价（已按价格排序）。"""
    index: int
    origin: str
    destination: str
    distance_km: float = 0.0
    tz_shift_h: float = 0.0
    options: dict[str, Optional[Leg]] = field(default_factory=dict)

    def best(self, cabin: str) -> Optional[Leg]:
        return self.options.get(cabin)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "origin": self.origin,
            "destination": self.destination,
            "distance_km": round(self.distance_km, 1),
            "tz_shift_h": round(self.tz_shift_h, 1),
            "options": {k: (v.to_dict() if v else None) for k, v in self.options.items()},
        }


@dataclass
class SplitPlan:
    """一个拆分方案：在某个枢纽把整程切成 N 段，每段单独购票。"""
    hub: str
    label: str
    legs: list[LegChoice]
    layovers_min: list[float] = field(default_factory=list)   # legs[i] -> legs[i+1]
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "hub": self.hub,
            "label": self.label,
            "note": self.note,
            "layovers_min": [round(x) for x in self.layovers_min],
            "legs": [l.to_dict() for l in self.legs],
        }


@dataclass
class Combo:
    """一条完整行程：每段选定一个舱位后的组合结果。"""
    plan_hub: str
    cabins: list[str]
    total_price: float
    currency: str
    total_duration_min: float
    total_ground_min: float
    comfort_score: float
    price_delta: float = 0.0        # 相对「全经济舱」的差价
    rest_gain: float = 0.0          # 相对「全经济舱」多拿到的有效休息（小时）
    value_per_rest_hour: Optional[float] = None
    legs: list[Leg] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plan_hub": self.plan_hub,
            "cabins": self.cabins,
            "cabins_cn": [CABIN_CN.get(c, c) for c in self.cabins],
            "total_price": round(self.total_price, 2),
            "currency": self.currency,
            "total_duration_min": round(self.total_duration_min),
            "total_ground_min": round(self.total_ground_min),
            "comfort_score": round(self.comfort_score, 2),
            "price_delta": round(self.price_delta, 2),
            "rest_gain": round(self.rest_gain, 2),
            "value_per_rest_hour": (
                round(self.value_per_rest_hour, 2) if self.value_per_rest_hour is not None else None
            ),
            "legs": [l.to_dict() for l in self.legs],
        }


@dataclass
class Query:
    origin: str
    destination: str
    date: str                       # YYYY-MM-DD（出发日）
    date_window: int = 0            # 前后各浮动几天
    adults: int = 1
    currency: str = "CNY"
    hubs: list[str] = field(default_factory=list)
    max_ground_min: int = 600       # 单段行程最长总时长（分钟）
    max_stops: int = 1              # 每个票段最多中转几次
    allow_self_transfer: bool = True
    return_date: str = ""           # 返程日期；空 = 单程

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RoundTrip:
    """一张真正的往返票（去程+回程绑在一张票里，价格不可按方向拆分）。"""
    origin: str
    destination: str
    outbound: Leg
    inbound: Leg
    price: float                    # 往返总价
    currency: str
    cabin: str
    provider: str = ""
    booking_url: Optional[str] = None
    note: str = "整程同一舱位，不能只升其中一段"

    @property
    def duration_min(self) -> int:
        return int(self.outbound.duration_min) + int(self.inbound.duration_min)

    def to_dict(self) -> dict:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "price": round(self.price, 2),
            "currency": self.currency,
            "cabin": self.cabin,
            "cabin_cn": CABIN_CN.get(self.cabin, self.cabin),
            "note": self.note,
            "duration_min": self.duration_min,
            "outbound": self.outbound.to_dict(),
            "inbound": self.inbound.to_dict(),
        }

