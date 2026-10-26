"""分舱段购票助手 —— 纯逻辑算法引擎。

不联网、不依赖第三方库，只做「候选枢纽 -> 拆分方案 -> 休息评分 -> 组合 -> 推荐」。

对外入口（api.py 只应该通过这些函数使用引擎）：
  * split.candidate_hubs / split.build_plan / split.build_plans
  * score.effective_rest_h / score.rest_score / score.cost_per_rest_hour
    （以及给界面展示用的 sleep_window_h / night_ratio / rest_detail）
  * optimize.build_combos / optimize.annotate / optimize.recommend
"""
from __future__ import annotations

from .optimize import annotate, build_combos, recommend
from .score import (
    cost_per_rest_hour,
    effective_rest_h,
    night_ratio,
    rest_detail,
    rest_score,
    sleep_window_h,
)
from .split import build_plan, build_plans, candidate_hubs

__all__ = [
    # split
    "candidate_hubs",
    "build_plan",
    "build_plans",
    # score
    "effective_rest_h",
    "rest_score",
    "cost_per_rest_hour",
    "sleep_window_h",
    "night_ratio",
    "rest_detail",
    # optimize
    "build_combos",
    "annotate",
    "recommend",
]
