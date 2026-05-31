"""1xbet — 透過 LineFeed API 抓 1x2 賠率。

.com 主站對台灣 IP 會 302 轉址；改用可達的鏡像 host（service-api/LineFeed）。
E 陣列中 G==1 為 1x2 主盤，T: 1=主勝 2=和 3=客勝，C=歐式賠率。
"""
import requests
from datetime import datetime, timezone
from core.models import MatchOdds, _line_key
from core.normalize import match_key

# 可用鏡像（依序嘗試，遇到 302/空回應就換下一個）
HOSTS = [
    "https://1xbet.com",     # 主站：從美國(GitHub)通常供應全部運動（含籃球/棒球）
    "https://1xbet.ng",      # 鏡像備援（主要只有足球）
    "https://1x001.com",
]
# 1xbet sport id → sport 標籤（1=足球、3=籃球、5=棒球；注意 66=板球非棒球）
SPORTS = {1: "soccer", 3: "basketball", 5: "baseball"}
HEADERS = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "accept": "application/json"}
# 兩種路徑樣式都試：service-api（新）與直接 LineFeed（舊，1xbet.com 主站，從美國較全）
PATHS = ["{host}/service-api/{feed}/Get1x2_VZip", "{host}/{feed}/Get1x2_VZip"]


def _get_feed(sport_id, feed):
    """feed = 'LineFeed'(賽前) 或 'LiveFeed'(滾球)。依序試 host × 路徑樣式，回第一個有資料者。"""
    for host in HOSTS:
        for tmpl in PATHS:
            url = (f"{tmpl.format(host=host, feed=feed)}?"
                   f"sports={sport_id}&count=100&lng=en&mode=4&getEmpty=true")
            try:
                r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=False)
                if r.status_code != 200 or not r.content:
                    continue
                data = r.json()
                if data.get("Value"):
                    return data["Value"], host
            except Exception:
                continue
    return [], None


# 1xbet 滾球節次英文 → 繁中
_PERIOD_ZH = {
    "1st half": "上半場", "2nd half": "下半場", "half-time": "中場休息",
    "1st quarter": "第一節", "2nd quarter": "第二節", "3rd quarter": "第三節",
    "4th quarter": "第四節", "overtime": "延長賽", "extra time": "延長賽",
    "extra time 1st half": "延長上半", "extra time 2nd half": "延長下半",
    "penalties": "PK 大戰", "break": "暫停", "not started": "即將開始",
    "1st period": "第一節", "2nd period": "第二節", "3rd period": "第三節",
}


def _live_score(e):
    """組出滾球比分字串，如 '1:0 · 下半場 67''。"""
    sc = e.get("SC") or {}
    fs = sc.get("FS") or {}
    s1, s2 = fs.get("S1"), fs.get("S2")
    score = f"{s1}:{s2}" if s1 is not None and s2 is not None else ""
    period = sc.get("CPS", "") or ""
    period = _PERIOD_ZH.get(period.lower().strip(), period)
    ts = sc.get("TS")
    mins = f"{ts // 60}'" if isinstance(ts, int) and ts > 0 else ""
    return " · ".join(p for p in [score, period, mins] if p)


def _odds_from_E(E):
    """從 E 取 1x2 主盤（G==1）。"""
    o = {1: None, 2: None, 3: None}
    for m in E:
        if m.get("G") == 1 and m.get("T") in (1, 2, 3):
            o[m["T"]] = m.get("C")
    return o


def _lines_from_E(E):
    """從 E 取讓分(G==2: T7主/T8客) 與 大小分(G==17: T9大/T10小)。C 即歐式賠率。"""
    sp, to = {}, {}
    for m in E:
        g, t, p, c = m.get("G"), m.get("T"), m.get("P"), m.get("C")
        if p is None or c is None:
            continue
        if g == 2:        # 讓分（主隊讓分線為 key）
            if t == 7:
                sp.setdefault(_line_key(p), {})["home"] = c        # 主隊讓 P
            elif t == 8:
                sp.setdefault(_line_key(-p), {})["away"] = c       # 客隊讓 P → 主隊線 = -P
        elif g == 17:     # 大小分
            if t == 9:
                to.setdefault(_line_key(p), {})["over"] = c
            elif t == 10:
                to.setdefault(_line_key(p), {})["under"] = c
    return sp, to


def fetch():
    # 1xbet LiveFeed 同一場會拆成多筆：有些只有「正確比分」沒賠率(E空)、有些有賠率但比分是子盤垃圾(如 3:3)。
    # 故同場合併：賠率取「有賠率(且滾球優先)」那筆、比分取「各節比分(PS)最完整」那筆。
    groups = {}
    for sport_id, sport in SPORTS.items():
        for feed, is_live in (("LiveFeed", True), ("LineFeed", False)):
            events, host = _get_feed(sport_id, feed)
            for e in events:
                home, away = e.get("O1", ""), e.get("O2", "")
                if not home or not away:
                    continue
                o = _odds_from_E(e.get("E", []))
                has_odds = bool(o[1] or o[3])
                sc = e.get("SC") or {}
                fs = sc.get("FS") or {}
                sq = (len(sc.get("PS") or []), (fs.get("S1") or 0) + (fs.get("S2") or 0))
                start = (datetime.fromtimestamp(e["S"], tz=timezone.utc).isoformat().replace("+00:00", "Z")
                         if e.get("S") else None)
                key = (sport, match_key(home, away))
                g = groups.setdefault(key, {
                    "home": home, "away": away, "sport": sport, "start": start,
                    "league": e.get("LE", ""), "o": {1: None, 2: None, 3: None},
                    "live": False, "score": "", "sq": (-1, -1), "sp": {}, "to": {},
                    "host": host, "has_odds": False, "odds_live": False})
                # 取賠率：優先「有賠率且為滾球」，否則先有先存
                if has_odds and (not g["has_odds"] or (is_live and not g["odds_live"])):
                    g["o"], g["has_odds"], g["odds_live"] = o, True, is_live
                    g["home"], g["away"] = home, away      # 以賠率筆的主客方向為準（比分同方向）
                    g["start"], g["league"], g["host"] = start, e.get("LE", ""), host
                    g["sp"], g["to"] = _lines_from_E(e.get("E", []))  # 讓分/大小分取同一筆
                # 取比分：滾球中各節最完整者
                if is_live:
                    g["live"] = True
                    score = _live_score(e)
                    if score and sq > g["sq"]:
                        g["score"], g["sq"] = score, sq
                if g["start"] is None and start:
                    g["start"] = start

    rows = []
    for g in groups.values():
        o = g["o"]
        if not (o[1] or o[3]):       # 純計分板(無賠率)不單獨成列
            continue
        rows.append(MatchOdds(
            source="1xbet", sport=g["sport"], home=g["home"], away=g["away"],
            start=g["start"], league=g["league"],
            home_odds=o[1], draw_odds=o[2], away_odds=o[3],
            url=f"{g['host'] or 'https://1xbet.com'}/en/{'live' if g['live'] else 'line'}",
            live=g["live"], score=g["score"],
            spreads=g.get("sp") or {}, totals=g.get("to") or {}))
    live_n = sum(1 for r in rows if r.live)
    print(f"[1xbet] 取得 {len(rows)} 場（滾球 {live_n}）")
    return rows


if __name__ == "__main__":
    for r in fetch()[:6]:
        print(r.to_dict())
