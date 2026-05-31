"""共用資料模型。各 provider 統一回傳 MatchOdds。"""
from dataclasses import dataclass, field, asdict
from typing import Optional


def american_to_decimal(price) -> Optional[float]:
    """美式賠率 → 歐式小數賠率（Curry 偏好歐式）。"""
    if price is None:
        return None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p == 0:
        return None
    if p > 0:
        return round(p / 100.0 + 1.0, 3)
    return round(100.0 / abs(p) + 1.0, 3)


def prob_to_decimal(prob) -> Optional[float]:
    """隱含機率 → 歐式賠率（Polymarket 用）。"""
    if prob is None:
        return None
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    if p <= 0 or p > 1:
        return None
    return round(1.0 / p, 3)


@dataclass
class MatchOdds:
    """單一來源、單一賽事的賠率（1X2 / moneyline）。"""
    source: str                     # pinnacle / polymarket / 1xbet / tsl / panda
    sport: str                      # soccer / basketball ...
    home: str
    away: str
    start: Optional[str] = None     # ISO8601 UTC
    league: str = ""
    # 歐式小數賠率
    home_odds: Optional[float] = None
    draw_odds: Optional[float] = None
    away_odds: Optional[float] = None
    url: str = ""
    live: bool = False          # 是否為滾球（進行中）
    score: str = ""             # 滾球即時比分/節次，如 "1:0 · 下半場 67'"

    def to_dict(self):
        return asdict(self)
