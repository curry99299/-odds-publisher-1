"""Pinnacle — 透過 guest Arcadia API 抓 moneyline 賠率。

每個運動只需兩次請求：
  1. /sports/{id}/matchups          → 賽事中繼資料（隊名、開賽時間、聯盟）
  2. /sports/{id}/markets/straight  → 賠率（依 matchupId join）
"""
import datetime
import re
import requests
from core.models import MatchOdds, american_to_decimal, _line_key

# 各運動「比賽進行中」時長（小時）；Pinnacle isLive 在主賽事層不可靠，改用開賽時間推斷
_LIVE_WINDOW = {"soccer": 2.8, "basketball": 3.2, "baseball": 3.9}


def _is_live(sport, start_iso, flag):
    if flag:
        return True
    if not start_iso:
        return False
    try:
        st = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    return st <= now <= st + datetime.timedelta(hours=_LIVE_WINDOW.get(sport, 3.0))

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
HEADERS = {
    "x-api-key": API_KEY,
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "accept": "application/json",
}

# Pinnacle sport id → 我們的 sport 標籤
SPORTS = {29: "soccer", 4: "basketball", 3: "baseball"}


def _get(path):
    r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch():
    rows = []
    for sport_id, sport in SPORTS.items():
        try:
            matchups = _get(f"/sports/{sport_id}/matchups?brandId=0")
            # primaryOnly=false → 含替代盤，讓分/大小分有多條線，才能跨家同線比較與找套利
            markets = _get(f"/sports/{sport_id}/markets/straight?primaryOnly=false&brandId=0")
        except Exception as e:
            print(f"[pinnacle] {sport} 抓取失敗: {e}")
            continue

        # 依 matchupId 收集 moneyline(主盤) / 讓分 / 大小分(含替代線)，period 0 = 全場
        odds_by_id, spreads_by_id, totals_by_id = {}, {}, {}
        for m in markets:
            if m.get("period") != 0:
                continue
            if m.get("type") == "moneyline" and m.get("isAlternate"):
                continue  # moneyline 只要主盤
            mid = m.get("matchupId")
            t = m.get("type")
            if t == "moneyline":
                odds_by_id[mid] = {p.get("designation"): p.get("price") for p in m.get("prices", [])}
            elif t == "spread":
                for p in m.get("prices", []):
                    pts = p.get("points")
                    if pts is None:
                        continue
                    # 以主隊讓分線為 key（主隊 points 即主隊讓分）
                    if p.get("designation") == "home":
                        spreads_by_id.setdefault(mid, {}).setdefault(_line_key(pts), {})["home"] = american_to_decimal(p.get("price"))
                    elif p.get("designation") == "away":
                        spreads_by_id.setdefault(mid, {}).setdefault(_line_key(-pts), {})["away"] = american_to_decimal(p.get("price"))
            elif t == "total":
                # Pinnacle total prices 慣例順序為 [over, under]（或用 designation）
                for i, p in enumerate(m.get("prices", [])):
                    pts = p.get("points")
                    if pts is None:
                        continue
                    dz = p.get("designation")
                    side = dz if dz in ("over", "under") else ("over" if i == 0 else "under")
                    totals_by_id.setdefault(mid, {}).setdefault(_line_key(pts), {})[side] = american_to_decimal(p.get("price"))

        # 正確比分（僅足球）：special.description == "Correct Score"（全場）。
        # matchups 已含 special、markets/straight 已含其賠率(prices 用 participantId)，不必多打 API。
        cs_by_parent = {}
        if sport == "soccer":
            cs_specials = {}  # special_matchup_id -> (parent_id, {participantId: 比分名稱})
            for mu in matchups:
                sp = mu.get("special") or {}
                if mu.get("type") == "special" and sp.get("description") == "Correct Score":
                    par = mu.get("parent")
                    pid = par.get("id") if isinstance(par, dict) else par
                    pmap = {p.get("id"): p.get("name") for p in (mu.get("participants") or [])}
                    cs_specials[mu.get("id")] = (pid, pmap)
            for m in markets:
                mid = m.get("matchupId")
                if mid not in cs_specials or m.get("period") != 0:
                    continue
                parent_id, pmap = cs_specials[mid]
                scores = {}
                for pr in m.get("prices", []):
                    name = pmap.get(pr.get("participantId"))
                    price = pr.get("price")
                    if not name or price is None:
                        continue
                    parts2 = name.rsplit(",", 1)  # "{主} h, {客} a"
                    if len(parts2) != 2:
                        continue
                    mh = re.search(r"(\d+)\s*$", parts2[0])
                    ma = re.search(r"(\d+)\s*$", parts2[1])
                    if not mh or not ma:
                        continue
                    scores[f"{mh.group(1)}-{ma.group(1)}"] = american_to_decimal(price)
                if scores:
                    cs_by_parent[parent_id] = scores

        for mu in matchups:
            if mu.get("parent") is not None:
                continue  # 只要主賽事，跳過衍生盤
            prices = odds_by_id.get(mu.get("id")) or {}  # 無賠率也保留賽程（顯示「—」）
            has_odds = any(prices.get(k) is not None for k in ("home", "away"))
            parts = {p.get("alignment"): p.get("name") for p in mu.get("participants", [])}
            home, away = parts.get("home"), parts.get("away")
            if not home or not away:
                continue
            rows.append(MatchOdds(
                source="pinnacle",
                sport=sport,
                home=home,
                away=away,
                start=mu.get("startTime"),
                league=(mu.get("league") or {}).get("name", ""),
                home_odds=american_to_decimal(prices.get("home")),
                draw_odds=american_to_decimal(prices.get("draw")),
                away_odds=american_to_decimal(prices.get("away")),
                url="https://www.pinnacle.com/",
                live=_is_live(sport, mu.get("startTime"), bool(mu.get("isLive"))) if has_odds
                else bool(mu.get("isLive")),
                spreads=spreads_by_id.get(mu.get("id")) or {},
                totals=totals_by_id.get(mu.get("id")) or {},
                cs=cs_by_parent.get(mu.get("id")) or {},
            ))
    print(f"[pinnacle] 取得 {len(rows)} 場")
    return rows


if __name__ == "__main__":
    for r in fetch()[:5]:
        print(r.to_dict())
