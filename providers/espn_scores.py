"""ESPN 公開 scoreboard API — 提供即時比分與局數/節數（免費、免登入）。

用來補齊各場滾球的比分（尤其棒球，1xbet 鏡像沒有棒球）。
回傳 {(sport, match_key): {"home","away","hs","as","state","status"}}。
state: 'pre'(未開始) / 'in'(進行中) / 'post'(已結束)。
"""
import re
import requests
from core.normalize import match_key
from core import i18n

# (ESPN 路徑, 我們的 sport)
BOARDS = [
    ("baseball/mlb", "baseball"),
    ("baseball/college-baseball", "baseball"),
    ("basketball/nba", "basketball"),
    ("hockey/nhl", "hockey"),
]
# ESPN 路徑 → 英文聯盟標籤（給獨立 live 來源用）
LEAGUE_EN = {"baseball/mlb": "MLB", "baseball/college-baseball": "NCAA Baseball",
             "basketball/nba": "NBA", "hockey/nhl": "NHL"}
BASE = "https://site.api.espn.com/apis/site/v2/sports"
HEADERS = {"user-agent": "Mozilla/5.0", "accept": "application/json"}


def _zh_status(sport, st):
    state = st.get("type", {}).get("state")
    if state == "post":
        return "結束"
    if state == "pre":
        return ""
    short = (st.get("type", {}).get("shortDetail")
             or st.get("type", {}).get("detail") or "").strip()
    if sport == "baseball":
        m = re.match(r"(Top|Bot|Bottom|Mid|End)\s+(\d+)", short, re.I)
        if m:
            half = {"top": "上", "bot": "下", "bottom": "下", "mid": "中", "end": "末"}[m.group(1).lower()]
            return f"{m.group(2)}局{half}"
    elif sport == "hockey":
        # NHL：shortDetail 形如 '17:04 - 3rd' / 'End of 2nd'；period 1-3 正規、4 延長、5 互射
        per = st.get("period")
        clk = st.get("displayClock")
        if re.search(r"end of", short, re.I):
            m = re.search(r"(\d+)", short)
            return f"第{m.group(1)}節結束" if m else "節間休息"
        if per:
            label = "互射" if per >= 5 else "延長賽" if per == 4 else f"第{per}節"
            return label + (f" {clk}" if clk and clk not in ("0:00", "") else "")
        return short
    else:
        if re.search(r"half", short, re.I):
            return "中場"
        m = re.search(r"Q(\d)\s*([\d:]+)?", short)
        if m:
            return f"第{m.group(1)}節" + (f" {m.group(2)}" if m.group(2) else "")
        clk = st.get("displayClock")
        per = st.get("period")
        if per:
            return f"第{per}節" + (f" {clk}" if clk else "")
    return short


def _board(path, sport):
    out = {}
    try:
        r = requests.get(f"{BASE}/{path}/scoreboard", headers=HEADERS, timeout=15)
        r.raise_for_status()
        for e in r.json().get("events", []):
            c = (e.get("competitions") or [{}])[0]
            st = c.get("status", {})
            comp = {x.get("homeAway"): x for x in c.get("competitors", [])}
            h, a = comp.get("home"), comp.get("away")
            if not h or not a:
                continue
            home = h["team"]["displayName"]
            away = a["team"]["displayName"]
            out[(sport, match_key(home, away, sport))] = {
                "home": home, "away": away,
                "hs": h.get("score"), "as": a.get("score"),
                "state": st.get("type", {}).get("state"),
                "status": _zh_status(sport, st),
                "league": LEAGUE_EN.get(path, sport),
                "start": e.get("date"),
            }
    except Exception as ex:
        print(f"[espn] {path} 失敗: {ex}")
    return out


# 終場（給結算用）：各運動 ESPN scoreboard 抓近 N 天 state=post 的比賽。
# 足球：世界盃 + 五大聯賽/歐冠/巴甲/MLS；球類：MLB/NBA/WNBA/NHL（playsport 漏收的場次靠這補齊）。
SOCCER_LEAGUES = ["fifa.world", "eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "uefa.champions", "bra.1", "usa.1"]
FINAL_BOARDS = ([("baseball/mlb", "baseball"), ("basketball/nba", "basketball"),
                 ("basketball/wnba", "basketball"), ("hockey/nhl", "hockey")]
                + [(f"soccer/{lg}", "soccer") for lg in SOCCER_LEAGUES])


def fetch_espn_finals(days=3):
    import datetime as _dt
    out = []
    today = _dt.datetime.now(_dt.timezone.utc)
    seen = set()
    for d in range(days):
        ymd = (today - _dt.timedelta(days=d)).strftime("%Y%m%d")
        for path, sport in FINAL_BOARDS:
            try:
                r = requests.get(f"{BASE}/{path}/scoreboard?dates={ymd}", headers=HEADERS, timeout=15)
                r.raise_for_status()
                for e in r.json().get("events", []):
                    c = (e.get("competitions") or [{}])[0]
                    if (c.get("status") or {}).get("type", {}).get("state") != "post":
                        continue
                    comp = {x.get("homeAway"): x for x in c.get("competitors", [])}
                    h, a = comp.get("home"), comp.get("away")
                    if not h or not a:
                        continue
                    home, away = h["team"]["displayName"], a["team"]["displayName"]
                    iso = e.get("date") or ""  # ESPN date(UTC) → 台灣日期，與 playsport results 對齊
                    try:
                        tpe = (_dt.datetime.fromisoformat(iso.replace("Z", "+00:00")) + _dt.timedelta(hours=8)).strftime("%Y-%m-%d")
                    except Exception:
                        tpe = iso[:10]
                    key = (sport, home, away, tpe)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"sport": sport, "home": home, "away": away,
                                "home_score": int(h.get("score") or 0), "away_score": int(a.get("score") or 0),
                                "score": f'{h.get("score")}:{a.get("score")}', "date": tpe, "league": path, "final": True})
            except Exception:
                pass
    print(f"[espn] 終場 {len(out)} 場（近 {days} 天，含 MLB/NBA/WNBA/NHL/足球）")
    return out


def fetch_soccer_finals(days=3):  # 相容舊名
    return fetch_espn_finals(days)


def fetch_scores():
    scores = {}
    for path, sport in BOARDS:
        scores.update(_board(path, sport))
    n_live = sum(1 for v in scores.values() if v["state"] == "in")
    print(f"[espn] 比分 {len(scores)} 場（進行中 {n_live}）")
    return scores


def live_events_from_scores(scores, sports):
    """把 ESPN 進行中(state=in)的場做成獨立 live 場次 dict（無賠率，純比分＋live 旗標）。
    用途：賠率來源（如 Polymarket 依 volume 分頁）間歇漏抓時，靠 ESPN 穩定維持滾球場不消失。
    僅針對 `sports` 指定的運動（目前冰球），避免與賠率源充足的棒球/籃球重複造列。"""
    out = []
    for (sport, _key), v in scores.items():
        if sport not in sports or v.get("state") != "in":
            continue
        league = v.get("league", sport)
        hs, as_ = v.get("hs"), v.get("as")
        sc = f"{hs}:{as_}" if hs is not None and as_ is not None else ""
        out.append({
            "sport": sport,
            "league": league, "league_zh": i18n.zh_league(league),
            "home": v["home"], "away": v["away"],
            "home_zh": i18n.zh_team(v["home"]), "away_zh": i18n.zh_team(v["away"]),
            "score": " · ".join(x for x in [sc, v.get("status", "")] if x),
            "start": v.get("start"), "live": True, "final": False,
        })
    return out


if __name__ == "__main__":
    sc = fetch_scores()
    for k, v in list(sc.items())[:10]:
        print(v["home"], v["hs"], ":", v["as"], v["away"], "|", v["state"], v["status"])
