"""Pinnacle 去水勝率推薦：每 10 分鐘由 publish.py 呼叫，產出 odds/pinnacle.json。

邏輯（與 bet-tracker 前端原型一致）：
- 比例去水：fair_p_i = (1/odds_i) / Σ(1/odds)；獨贏(1X2)、讓分、大小分各取主盤線較有利一邊。
- 只收主要聯賽；依去水勝率排序 → 前25%專家/次20%標準/次15%入門（共推薦60%）。
- open：未開賽(kickoff>now)的推薦，含 conf_chg（較上一份漲跌）。
- 鎖定：賽前最後一刻（-2~13 分窗）把當下收盤推薦凍結進 locked，之後不再變。
- 結算：完賽抓最終比分寫進 locked（status=settled）；locked 保留 7 天。
- events：較上一份的異動（appeared/removed/promoted/demoted），保留 24 小時。

狀態自帶在「上一份 pinnacle.json」裡（publish.py 會先 GET 回來），雲端無狀態也安全。
"""
import re
from datetime import datetime, timezone, timedelta


def _tpe_date(iso):
    """UTC ISO → 台灣日期(YYYY-MM-DD)。結算用，與賽果來源(playsport, TPE)的 date 對齊。"""
    if not iso:
        return ""
    try:
        return (datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(hours=8)).strftime("%Y-%m-%d")
    except Exception:
        return iso[:10]


import os
import json as _json
try:  # 用專案既有的隊名正規化 + 模糊比對(跨來源隊名不一致時靠它)
    from core.normalize import norm_team, teams_similar
except Exception:  # 隔離測試無 core 時退化
    def norm_team(x, sport=None):
        return (x or "").strip().lower()

    def teams_similar(a, b):
        return bool(a) and a == b

_ZH2EN = None


def _zh2en(zh):
    """舊版 locked 只有中文隊名 → 反查『正規化英文』(translate 快取的 key 即正規化英文)，供結算配對。"""
    global _ZH2EN
    if _ZH2EN is None:
        _ZH2EN = {}
        try:  # 1) i18n 靜態表(國家隊/球隊 en→zh)：反查後正規化(國家隊優先，最乾淨)
            from core.i18n import _TEAMS_EN
            for en, z in _TEAMS_EN.items():
                _ZH2EN.setdefault(z, norm_team(en))
        except Exception:
            pass
        try:  # 2) translate 快取(key 本身即正規化英文)
            p = os.path.join(os.path.dirname(__file__), "data", "team_translations.json")
            for en_key, z in _json.load(open(p, encoding="utf-8")).get("teams", {}).items():
                _ZH2EN.setdefault(z, en_key)
        except Exception:
            pass
    return _ZH2EN.get(zh, "")


def _team_norm(en, zh):
    """隊名 → 正規化英文：有英文用英文，沒有(舊 locked)就用中文反查。"""
    return norm_team(en) if en else _zh2en(zh or "")


HORIZON_MS = 48 * 3600 * 1000     # open 列未來 48 小時
LOCK_MIN, LOCK_MAX = -2.0, 13.0   # 賽前最後一刻鎖定窗（分鐘；與 capture_closings 一致）
LOCKED_KEEP_MS = 7 * 24 * 3600 * 1000   # locked 保留 7 天
EVENT_KEEP_MS = 24 * 3600 * 1000        # 異動保留 24 小時


WIN_BOOST = 6  # 去水勝率全體加成（上限 100）；+6 讓 EV(=勝率×賠率−1) 幾乎全為正（最低約 +1.7）；EV 由前端自動重算
# 純 EV(價值)排序、不設勝率下限 → 高賠低估盤(賠率>2)也會被推薦；推薦側本就取去水較有利那邊，不會出現極端冷門。


def _win(prob):
    return min(100, round(prob * 100) + WIN_BOOST)


def _devig(odds):
    if not odds or any((not o) or o <= 1 for o in odds):
        return None
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv] if s > 0 else None


def _is_major(t):
    if re.search(r'reserve|u-?\d|next pro|youth|friendly|友誼|team vs player|女子', t) and not re.search(r'wnba', t):
        return False
    pats = [r'\bmlb\b|美國職棒大聯盟', r'\bnpb\b|nippon professional baseball|日本職棒',
            r'\bkbo\b|korea professional baseball|韓國職棒', r'中華職棒|\bcpbl\b', r'\bnba\b|美國職籃',
            r'\bwnba\b|女子職業籃球', r'\bnhl\b', r'\bcba\b|中國男子籃球|中國男籃', r'world cup|世界盃|fifa',
            r'england.*premier league|premier league.*england|英超', r'serie a|義甲', r'la ?liga|西甲',
            r'bundesliga|德甲', r'ligue ?1|法甲', r'champions league|歐冠|歐洲冠軍', r'brasileir|巴甲', r'\bmls\b|美國職業足球']
    return any(re.search(p, t) for p in pats)


# 排除佔位/衍生市場事件（隊名是 Home/Away、主場/客隊，或含「得分/主場/客場」等非真實對戰字樣）
_PLACEHOLDER = {"home", "away", "tbd", "team a", "team b", "主場", "客場", "主隊", "客隊", "over", "under"}
def _real_match(e):
    for k in ("home", "away", "home_zh", "away_zh"):
        v = (e.get(k) or "").strip()
        if not v:
            continue  # 缺欄位(如 locked 只有 zh)→ 跳過該欄
        # 佔位/衍生盤名稱：主場/客場/得分、英文 prop(Home Runs (14 Games)、Total Goals…)、含「(N Games)」、含 Runs/Goals/Points 等統計字
        if v.lower() in _PLACEHOLDER or re.search(
            r"得分|主場|客場|主隊|客隊|\(\s*\d+\s*games?\s*\)|\b(runs|goals|points|corners|cards|bookings|hits|sets|games)\b", v, re.I
        ):
            return False
    return True


def _league_code(league, zh):
    t = f"{league} {zh}".lower()
    table = [(r'\bmlb\b|美國職棒大聯盟', 'MLB'), (r'\bnpb\b|nippon|日本職棒', 'NPB'),
             (r'\bkbo\b|korea professional baseball|韓國職棒', 'KBO'), (r'中華職棒|\bcpbl\b', 'CPBL'),
             (r'\bnba\b|美國職籃', 'NBA'), (r'\bwnba\b', 'WNBA'), (r'\bnhl\b', 'NHL'), (r'\bcba\b', 'CBA'),
             (r'world cup|世界盃|fifa', zh or '世界盃'), (r'england.*premier|英超', 'PremierLeague'),
             (r'serie a|義甲', 'SerieA'), (r'la ?liga|西甲', 'LaLiga'), (r'bundesliga|德甲', 'Bundesliga'),
             (r'ligue ?1|法甲', 'Ligue1'), (r'champions|歐冠', 'UefaChampionsLeague'), (r'brasileir|巴甲', 'Serie A_BR1')]
    for pat, code in table:
        if re.search(pat, t):
            return code
    return zh or league


def _main_line(lines):
    """lines: [(line, a_odds, b_odds, best_a, best_b)]；回傳去水後兩邊最接近 50/50 的那條。"""
    best = None
    diff = 1e9
    for ln, a, b, ba, bb in lines:
        p = _devig([a, b])
        if not p:
            continue
        d = abs(p[0] - p[1])
        if d < diff:
            diff = d
            best = (ln, p, ba, bb)
    return best


def _best(ref):
    ref = ref or {}
    return (ref.get("source"), ref.get("odds"))


def _doc_id(e, market):
    sport = e.get("sport")
    home = e.get("home_zh") or e.get("home")
    away = e.get("away_zh") or e.get("away")
    date = (e.get("start") or "")[:10]
    return "|".join(str(x or "") for x in (sport, home, away, date, market)).replace("/", "_")


def _candidate(e, market):
    """回傳該場該盤口的去水推薦 dict（未排序、未分級），無則 None。"""
    home_zh = e.get("home_zh") or e.get("home")
    away_zh = e.get("away_zh") or e.get("away")
    base = {
        "id": _doc_id(e, market), "sport": e.get("sport"), "league": _league_code(e.get("league") or "", e.get("league_zh") or ""),
        "home_zh": home_zh, "away_zh": away_zh, "start": e.get("start"), "market": market,
        # 結算用：英文隊名(跨來源一致) + TPE 日期(與賽果 date 對齊)
        "home_en": (e.get("home") or "").strip(), "away_en": (e.get("away") or "").strip(), "match_date": _tpe_date(e.get("start")),
    }
    if market == "ml":
        pin = (e.get("sources") or {}).get("pinnacle") or {}
        h, d, a = pin.get("home"), pin.get("draw"), pin.get("away")
        if not (h and a and h > 1 and a > 1):
            return None
        three = bool(e.get("has_draw")) and d and d > 1
        p = _devig([h, d, a]) if three else _devig([h, a])
        if not p:
            return None
        bst = e.get("best") or {}
        if three:
            outs = [("home", p[0], _best(bst.get("home"))), ("draw", p[1], _best(bst.get("draw"))), ("away", p[2], _best(bst.get("away")))]
        else:
            outs = [("home", p[0], _best(bst.get("home"))), ("away", p[1], _best(bst.get("away")))]
        # PK盤：用原始去水勝率(未加成 prob)算各邊 EV，在「合理機率」邊裡推 EV 最高那邊（可推價值冷門，但不挑超低機率的雜訊盤）
        ML_FLOOR = 0.30  # 只考慮原始勝率 ≥30% 的邊；都不到(大冷門場)→回推勝率最高邊
        cand = [o for o in outs if o[1] >= ML_FLOOR] or outs
        def _evof(o):
            od = o[2][1]
            return (o[1] * od - 1) if (od and od > 1) else -9
        side, prob, (book, odds) = max(cand, key=_evof)  # 選邊：用原始去水 EV 判斷
        ev = (_win(prob) / 100.0 * odds - 1) * 100 if (odds and odds > 1) else -999  # 排序：用加成勝率 EV，與讓分/大小同基準才公平
        return {**base, "side": side, "line": None, "win": _win(prob), "odds": odds, "book": book, "_ev": ev}
    if market == "sp":
        lines = []
        for s in (e.get("spread") or []):
            pin = (s.get("sources") or {}).get("pinnacle") or {}
            if (pin.get("home") or 0) > 1 and (pin.get("away") or 0) > 1:
                bb = s.get("best") or {}
                lines.append((s.get("line"), pin["home"], pin["away"], _best(bb.get("home")), _best(bb.get("away"))))
        ml = _main_line(lines)
        if not ml:
            return None
        ln, p, ba, bb = ml
        home_fav = p[0] >= p[1]
        try:
            n = float(ln)
        except Exception:
            n = 0.0
        win_v, odds_v = _win(max(p[0], p[1])), (ba if home_fav else bb)[1]
        return {**base, "side": "home" if home_fav else "away", "line": (n if home_fav else -n),
                "win": win_v, "odds": odds_v, "book": (ba if home_fav else bb)[0],
                "_ev": (win_v / 100.0 * odds_v - 1) * 100 if (odds_v and odds_v > 1) else -999}
    if market == "uo":
        lines = []
        for t in (e.get("total") or []):
            pin = (t.get("sources") or {}).get("pinnacle") or {}
            if (pin.get("over") or 0) > 1 and (pin.get("under") or 0) > 1:
                bb = t.get("best") or {}
                lines.append((t.get("line"), pin["over"], pin["under"], _best(bb.get("over")), _best(bb.get("under"))))
        ml = _main_line(lines)
        if not ml:
            return None
        ln, p, bo, bu = ml
        over_fav = p[0] >= p[1]
        try:
            n = float(ln)
        except Exception:
            n = None
        win_v, odds_v = _win(max(p[0], p[1])), (bo if over_fav else bu)[1]
        return {**base, "side": "over" if over_fav else "under", "line": n,
                "win": win_v, "odds": odds_v, "book": (bo if over_fav else bu)[0],
                "_ev": (win_v / 100.0 * odds_v - 1) * 100 if (odds_v and odds_v > 1) else -999}
    if market == "xg":  # 正確比分：去水所有比分→推最高機率那個(預測導向,勝率不加成、無跨家賠率所以以機率排序)
        cs = e.get("cs") or {}
        pairs = [(sc, od) for sc, od in cs.items() if od and od > 1]
        if len(pairs) < 4:
            return None
        probs = _devig([od for _, od in pairs])
        if not probs:
            return None
        bi = max(range(len(pairs)), key=lambda i: probs[i])
        sc, od = pairs[bi]
        win = round(probs[bi] * 100)
        return {**base, "side": sc, "line": None, "win": win, "odds": od, "book": "pinnacle",
                "_ev": win}  # 排序用機率(CS 無跨家最佳賠率,不比 EV)
    return None


def _score_list(events, results):
    """彙整可結算的比分：[{sport, hn(正規化英文主), an(客), date(TPE), hs, as, final}]。
    用模糊隊名比對(teams_similar)而非精確 key，吸收跨來源隊名差異。"""
    out = []

    def add(sport, hen, aen, hzh, azh, hs, as_, date, final):
        hn, an = _team_norm(hen, hzh), _team_norm(aen, azh)
        if not hn or not an or hs is None or as_ is None or not date:
            return
        out.append({"sport": sport, "hn": hn, "an": an, "date": date, "hs": hs, "as": as_, "final": final})

    for e in (events or []):  # 滾球即時
        add(e.get("sport"), e.get("home"), e.get("away"), e.get("home_zh"), e.get("away_zh"),
            e.get("home_score"), e.get("away_score"), _tpe_date(e.get("start")), bool(e.get("final")))
    for e in (results or []):  # 完賽(date 已是 TPE)
        add(e.get("sport"), e.get("home"), e.get("away"), e.get("home_zh"), e.get("away_zh"),
            e.get("home_score"), e.get("away_score"), e.get("date") or _tpe_date(e.get("start")), True)
    return out


def _build_parlays(open_out):
    """用已推薦的單場(open)組串：一場一腳、跨聯賽可。標準2串×2(價值+穩膽)、專家3串×2(價值+穩膽)。"""
    legs_ok = [p for p in open_out if p.get("odds") and p.get("odds") > 1 and p.get("market") != "xg"]  # 正確比分不進串關
    ev = lambda p: p["win"] / 100.0 * p["odds"] - 1
    game = lambda p: (p["home_zh"], p["away_zh"], p["start"])
    VAL_WIN_FLOOR = 50  # 價值串每腳勝率下限：避免疊一堆冷門變成超低命中的樂透
    # 腳池含所有盤口(讓分/大小也算)；不先取每場代表腳，組串時才限「不同場」→ 讓分/大小有機會進串
    val_pool = sorted([p for p in legs_ok if p["win"] >= VAL_WIN_FLOOR], key=lambda p: -ev(p))  # 價值：勝率≥50 中 EV 高
    safe_pool = sorted(legs_ok, key=lambda p: -p["win"])  # 穩膽：勝率高

    def take(pool, count):
        # 優先「不同場 + 不同盤口」(讓分/大小都進得來,不會整串都獨贏)；湊不滿再放寬成只「不同場」
        for diverse in (True, False):
            legs, used_g, used_m = [], set(), set()
            for p in pool:
                g = game(p)
                if g in used_g:
                    continue  # 同場只取一腳(避免相關性)
                if diverse and p["market"] in used_m:
                    continue  # 同盤口不重複(保證多樣)
                used_g.add(g); used_m.add(p["market"]); legs.append(p)
                if len(legs) == count:
                    return legs
        return None

    def mk(legs, plan, stars, strat):
        import functools
        odds = round(functools.reduce(lambda a, l: a * l["odds"], legs, 1.0), 2)
        win = functools.reduce(lambda a, l: a * l["win"] / 100.0, legs, 1.0)
        return {
            "id": strat + "|" + "|".join(l["id"] for l in legs), "strategy": strat, "plan": plan, "stars": stars,
            "legs_count": len(legs), "odds": odds, "win": round(win * 100), "ev": round((win * odds - 1) * 100, 1),
            "dateId": min(l["start"][:10] for l in legs),
            "legs": [{"league": l["league"], "sport": l["sport"], "home_zh": l["home_zh"], "away_zh": l["away_zh"],
                      "market": l["market"], "side": l["side"], "line": l["line"], "odds": l["odds"], "start": l["start"]} for l in legs],
        }

    out = []
    def add(pool, count, plan, stars, strat):
        legs = take(pool, count)
        if legs:
            out.append(mk(legs, plan, stars, strat))

    # 標準 2串：價值 + 穩膽
    add(val_pool, 2, "standard", 2, "價值2串")
    add(safe_pool, 2, "standard", 2, "穩膽2串")
    # 專家 3串：價值 + 穩膽
    add(val_pool, 3, "pro", 3, "價值3串")
    add(safe_pool, 3, "pro", 3, "穩膽3串")
    return out


def build(events, results, prev, now=None):
    """產出新的 pinnacle.json 結構。prev=上一份（dict）；now=datetime(UTC)。"""
    now = now or datetime.now(timezone.utc)
    now_ms = now.timestamp() * 1000
    prev = prev or {}
    prev_open = {p["id"]: p for p in (prev.get("open") or [])}
    locked = {p["id"]: p for p in (prev.get("locked") or [])}
    prev_events = prev.get("events") or []

    # 1) 算全部主流候選（含已開賽，用於結算分數對應；排序池只取未開賽）
    cands = []
    for e in (events or []):
        if not _is_major(f"{e.get('league') or ''} {e.get('league_zh') or ''}".lower()):
            continue
        if not _real_match(e):  # 跳過佔位/衍生市場（Home/Away、主場/客隊、得分…）
            continue
        start = e.get("start")
        if not start:
            continue
        try:
            ms = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            continue
        markets_list = ("ml", "sp", "uo", "xg") if e.get("cs") else ("ml", "sp", "uo")  # xg=正確比分(僅足球有 cs 時)
        for market in markets_list:
            c = _candidate(e, market)
            if c:
                c["_ms"] = ms
                cands.append(c)

    # 2) 排序池＝未開賽且 48h 內；分級前25/20/15%
    # 推薦池：未開賽 48h 內 + 有賠率；依 EV(價值) 由高到低排序（不設勝率下限 → 高賠值盤也進得來）。
    # _ev 已在 _candidate 各自算好：PK盤=原始去水 EV(推 EV 最高邊)、讓分/大小=加成勝率 EV(推 favored)。
    pool = [c for c in cands if c["_ms"] > now_ms and c["_ms"] <= now_ms + HORIZON_MS and c.get("odds")]
    # 分級：每個「聯盟×盤口」各自排序分級，避免某盤口霸佔專家、某盤口完全沒專家（分布平均、UX 佳）。
    #   PK盤(ml)依 EV(_ev)；讓分/大小(sp/uo)依勝率高。各取前 25/20/15% = 專家/標準/入門；小桶保底至少 1 場專家。
    from collections import defaultdict
    grouped = defaultdict(list)
    for c in pool:
        grouped[(c["league"], c["market"])].append(c)
    tier_of = {}
    for (_lg, mkt), grp in grouped.items():
        if mkt == "ml":
            grp.sort(key=lambda c: -(c.get("_ev") if c.get("_ev") is not None else -999))
        else:
            grp.sort(key=lambda c: (-c["win"], -(c.get("_ev") if c.get("_ev") is not None else -999)))
        n = len(grp)
        nE, nS, nB = round(n * 0.25), round(n * 0.20), round(n * 0.15)
        if n >= 1 and nE + nS + nB == 0:
            nE = 1  # 小桶(1~2場)保底：至少最佳 1 場進專家
        for i, c in enumerate(grp):
            if i < nE:
                tier_of[c["id"]] = ("pro", 3)
            elif i < nE + nS:
                tier_of[c["id"]] = ("standard", 2)
            elif i < nE + nS + nB:
                tier_of[c["id"]] = ("basic", 1)

    # 3) open（推薦的未開賽）＋ conf_chg（較上一份）
    open_out = []
    for c in pool:
        tp = tier_of.get(c["id"])
        if not tp:
            continue
        plan, units = tp
        pv = prev_open.get(c["id"])
        chg = 0
        if pv and pv.get("win") is not None:
            chg = 1 if c["win"] > pv["win"] else (-1 if c["win"] < pv["win"] else 0)
        open_out.append({k: c[k] for k in ("id", "sport", "league", "home_zh", "away_zh", "home_en", "away_en", "match_date", "start", "market", "side", "line", "win", "odds", "book")}
                        | {"plan": plan, "units": units, "conf_chg": chg, "status": "open"})

    # 4) 鎖定：賽前最後一刻窗內、且為推薦者 → 凍結進 locked（同 id 覆寫＝收盤最後一份）
    cand_by_id = {c["id"]: c for c in cands}
    for c in pool:
        mins = (c["_ms"] - now_ms) / 60000.0
        if LOCK_MIN <= mins <= LOCK_MAX and c["id"] in tier_of:
            plan, units = tier_of[c["id"]]
            locked[c["id"]] = {k: c[k] for k in ("id", "sport", "league", "home_zh", "away_zh", "home_en", "away_en", "match_date", "start", "market", "side", "line", "win", "odds", "book")} | {
                "plan": plan, "units": units, "status": "locked", "home_score": None, "away_score": None, "locked_at": now.isoformat()}

    # 5) 結算：locked 用「正規化英文兩隊(模糊比對) + TPE日期」配對賽果。
    #    新 locked 有 home_en；舊 locked 只有中文→反查英文。teams_similar 吸收跨來源隊名差異、並自動判主客方向。
    sl = _score_list(events, results)
    for lid, lp in locked.items():
        if lp.get("status") == "settled":
            continue
        ph = _team_norm(lp.get("home_en"), lp.get("home_zh"))
        pa = _team_norm(lp.get("away_en"), lp.get("away_zh"))
        date = lp.get("match_date") or _tpe_date(lp.get("start"))
        if not ph or not pa or not date:
            continue
        match = None
        for s in sl:
            if s["sport"] != lp.get("sport") or s["date"] != date:
                continue
            direct = teams_similar(ph, s["hn"]) and teams_similar(pa, s["an"])
            swap = teams_similar(ph, s["an"]) and teams_similar(pa, s["hn"])
            if direct or swap:
                match = (s, direct)
                if s["final"]:
                    break  # 優先採完賽結果
        if match:
            s, direct = match
            lp["home_score"] = s["hs"] if direct else s["as"]
            lp["away_score"] = s["as"] if direct else s["hs"]
            if s["final"]:
                lp["status"] = "settled"

    # 6) 清理：locked 保留 7 天、events 保留 24h
    def ms_of(iso):
        try:
            return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            return 0
    locked = {k: v for k, v in locked.items() if ms_of(v.get("start")) >= now_ms - LOCKED_KEEP_MS and _real_match(v)}

    # 7) 異動（open 較上一份）
    new_events = []
    rank = {"pro": 0, "standard": 1, "basic": 2}
    cur_open = {p["id"]: p for p in open_out}
    at = now.isoformat()
    first_run = not prev_open and not prev.get("locked")  # 首次無上一份 → 不灌「全部出現」
    for pid, p in (cur_open.items() if not first_run else []):
        pv = prev_open.get(pid)
        if not pv:
            new_events.append({"id": f"{pid}|{at}|appeared", "event": "appeared", "pick": p, "from_plan": None, "to_plan": p["plan"], "at": at})
        elif p["plan"] != pv.get("plan"):
            ev = "promoted" if rank.get(p["plan"], 9) < rank.get(pv.get("plan"), 9) else "demoted"
            new_events.append({"id": f"{pid}|{at}|{ev}", "event": ev, "pick": p, "from_plan": pv.get("plan"), "to_plan": p["plan"], "at": at})
    for pid, pv in (prev_open.items() if not first_run else []):
        if pid not in cur_open:
            new_events.append({"id": f"{pid}|{at}|removed", "event": "removed", "pick": pv, "from_plan": pv.get("plan"), "to_plan": None, "at": at})
    events_out = (new_events + prev_events)
    events_out = [e for e in events_out if ms_of(e.get("at")) >= now_ms - EVENT_KEEP_MS][:500]

    parlays = _build_parlays(open_out)
    return {"updated_at": at, "open": open_out, "locked": list(locked.values()), "events": events_out, "parlays": parlays}
