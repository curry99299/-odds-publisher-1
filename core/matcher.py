"""跨來源賽事配對 + 最佳賠率/套利計算。

輸入：各 provider 回傳的 MatchOdds 扁平清單。
輸出：合併後的賽事清單，每場含各家賠率、最佳賠率、套利偵測。
"""
from datetime import datetime, timezone
from .normalize import norm_team, teams_similar
from .i18n import zh_team, zh_league
from .models import _line_key


def _agg_lines(lines_map, keys):
    """把 {line: {source: {k1,k2}}} 整理成排序清單，每條線含各家賠率與各邊最佳。"""
    out = []
    for line in sorted(lines_map, key=lambda x: float(x)):
        srcs = lines_map[line]
        best = {}
        for k in keys:
            cand = [(s, v.get(k)) for s, v in srcs.items() if v.get(k)]
            best[k] = ({"source": max(cand, key=lambda x: x[1])[0],
                        "odds": max(cand, key=lambda x: x[1])[1]} if cand else None)
        out.append({"line": line, "sources": srcs, "best": best, "n": len(srcs)})
    return out


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _same_event(a, b, window_hours=14):
    """兩筆來源不同的 MatchOdds 是否為同一場賽事。"""
    if a.sport != b.sport:
        return False
    # 主客可能對調，兩種排列都試
    direct = teams_similar(norm_team(a.home), norm_team(b.home)) and \
        teams_similar(norm_team(a.away), norm_team(b.away))
    swap = teams_similar(norm_team(a.home), norm_team(b.away)) and \
        teams_similar(norm_team(a.away), norm_team(b.home))
    if not (direct or swap):
        return False
    # 時間相近（容許各家排程時間略有差異）
    da, db = _parse_dt(a.start), _parse_dt(b.start)
    if da and db:
        if abs((da - db).total_seconds()) > window_hours * 3600:
            return False
    return True


def _flip_score(score):
    """把 '65:73 · 第三節 · 30'' 的比分部分翻轉成 '73:65 · ...'。"""
    if not score:
        return score
    parts = score.split(" · ")
    if ":" in parts[0]:
        ab = parts[0].split(":")
        if len(ab) == 2:
            parts[0] = f"{ab[1].strip()}:{ab[0].strip()}"
    return " · ".join(parts)


# 賽事排序優先序（league_zh 為主、英文關鍵字備援）
def _priority(e):
    lz = e.get("league_zh") or ""
    le = (e.get("league") or "").lower()
    sport = e.get("sport")
    if sport == "basketball" and ("美國職籃" in lz or "nba" in le):
        return 0
    if sport == "baseball":
        if "美國職棒大聯盟" in lz or "mlb" in le:
            return 1
        if "中華職棒" in lz or "cpb" in le:
            return 2
        if "日本職棒" in lz or "npb" in le or "nippon" in le:
            return 3
        if "韓國職棒" in lz or "kbo" in le or "korea" in le:
            return 4
    if sport == "soccer" and (any(k in lz for k in ("英超", "西甲", "義甲", "德甲", "法甲"))
                              or any(k in le for k in ("premier league", "la liga", "serie a",
                                                       "bundesliga", "ligue 1"))):
        return 5
    return 6


def _best(values):
    """values = [(source, odds)]，回傳賠率最高者（對下注者最有利）。"""
    vals = [(s, o) for s, o in values if o]
    if not vals:
        return None
    s, o = max(vals, key=lambda x: x[1])
    return {"source": s, "odds": o}


def _best_nonpm(sources, leg):
    """某腳的最佳「非 Polymarket」賠率與來源。"""
    cand = [(s, v[leg]) for s, v in sources.items()
            if s != "polymarket" and v.get(leg)]
    if not cand:
        return None
    s, o = max(cand, key=lambda x: x[1])
    return {"source": s, "odds": o}


def _arb(sources, best, legs):
    """以 Polymarket 為對沖腳的可實現套利偵測（與前端計算器一致）。

    枚舉每個「非 PM 固定腳」：固定腳用該腳最佳非 PM 賠率，其餘腳優先用
    Polymarket（無則用最佳其他平台）對沖、把各結果回報拉齊。
    取 1/賠率 加總最小（ROI 最大）的組合；加總 < 1 即有套利。
    """
    pm = sources.get("polymarket", {})
    best_roi, best_margin, exists = None, None, False
    for anchor_leg in legs:
        a = _best_nonpm(sources, anchor_leg)
        if not a:
            continue
        inv = 1.0 / a["odds"]
        ok = True
        for leg in legs:
            if leg == anchor_leg:
                continue
            if pm.get(leg):
                inv += 1.0 / pm[leg]
            elif best.get(leg):
                inv += 1.0 / best[leg]["odds"]
            else:
                ok = False
                break
        if not ok:
            continue
        margin = round((1.0 - inv) * 100, 2)
        if best_margin is None or margin > best_margin:
            best_margin = margin
            if inv < 1.0:
                exists = True
    return {"exists": exists, "margin": best_margin}


def merge(rows):
    """把扁平 MatchOdds 清單合併成賽事清單。"""
    events = []  # list of {anchor, rows:[MatchOdds]}
    for r in rows:
        placed = False
        for ev in events:
            if _same_event(ev["anchor"], r):
                # 同來源同場只留先到的
                if any(x.source == r.source for x in ev["rows"]):
                    break
                ev["rows"].append(r)
                placed = True
                break
        if not placed:
            events.append({"anchor": r, "rows": [r]})

    out = []
    for ev in events:
        anchor = ev["anchor"]
        has_draw = anchor.sport == "soccer" or any(x.draw_odds for x in ev["rows"])
        is_live = any(x.live for x in ev["rows"])
        # 比分對齊到 anchor 主客方向（來源若主客相反則翻轉 "a:b"）
        score = ""
        for x in ev["rows"]:
            if x.live and x.score:
                swap = not teams_similar(norm_team(anchor.home), norm_team(x.home))
                score = _flip_score(x.score) if swap else x.score
                break
        sources = {}
        for r in ev["rows"]:
            # 以 anchor 主客方向為準，若該來源主客對調則翻轉
            swap = not (teams_similar(norm_team(anchor.home), norm_team(r.home)))
            ho, ao = (r.away_odds, r.home_odds) if swap else (r.home_odds, r.away_odds)
            sources[r.source] = {
                "home": ho, "draw": r.draw_odds, "away": ao,
                "url": r.url, "league": r.league,
                "live": r.live, "score": r.score,
            }
        best_home = _best([(s, v["home"]) for s, v in sources.items()])
        best_draw = _best([(s, v["draw"]) for s, v in sources.items()]) if has_draw else None
        best_away = _best([(s, v["away"]) for s, v in sources.items()])
        best_map = {"home": best_home, "draw": best_draw, "away": best_away}
        leg_list = ["home", "draw", "away"] if has_draw else ["home", "away"]

        # 讓分 / 大小分：對齊 anchor 主客方向（來源翻轉時讓分線變號、主客對調；大小分不受影響）
        spread_lines, total_lines = {}, {}
        for r in ev["rows"]:
            swap = not teams_similar(norm_team(anchor.home), norm_team(r.home))
            for line, d in (r.spreads or {}).items():
                try:
                    lv = float(line)
                except (TypeError, ValueError):
                    continue
                if swap:
                    spread_lines.setdefault(_line_key(-lv), {})[r.source] = {"home": d.get("away"), "away": d.get("home")}
                else:
                    spread_lines.setdefault(_line_key(lv), {})[r.source] = {"home": d.get("home"), "away": d.get("away")}
            for line, d in (r.totals or {}).items():
                total_lines.setdefault(_line_key(line), {})[r.source] = {"over": d.get("over"), "under": d.get("under")}

        out.append({
            "sport": anchor.sport,
            "home": anchor.home,
            "away": anchor.away,
            "home_zh": zh_team(anchor.home),
            "away_zh": zh_team(anchor.away),
            "start": anchor.start,
            "league": anchor.league,
            "league_zh": zh_league(anchor.league),
            "live": is_live,
            "score": score,
            "has_draw": has_draw,
            "sources": sources,
            "best": best_map,
            "arb": _arb(sources, best_map, leg_list),
            "source_count": len(sources),
            "spread": _agg_lines(spread_lines, ("home", "away")),
            "total": _agg_lines(total_lines, ("over", "under")),
        })

    # 排序：聯盟優先序(NBA>MLB>中職>日職>韓職>五大聯賽>其他) → 滾球優先 → 來源數多 → 開賽時間
    out.sort(key=lambda e: (_priority(e), not e["live"],
                            -e["source_count"], e["start"] or "9999"))
    return out
