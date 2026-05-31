"""ESPN 公開 scoreboard API — 提供即時比分與局數/節數（免費、免登入）。

用來補齊各場滾球的比分（尤其棒球，1xbet 鏡像沒有棒球）。
回傳 {(sport, match_key): {"home","away","hs","as","state","status"}}。
state: 'pre'(未開始) / 'in'(進行中) / 'post'(已結束)。
"""
import re
import requests
from core.normalize import match_key

# (ESPN 路徑, 我們的 sport)
BOARDS = [
    ("baseball/mlb", "baseball"),
    ("baseball/college-baseball", "baseball"),
    ("basketball/nba", "basketball"),
]
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
            out[(sport, match_key(home, away))] = {
                "home": home, "away": away,
                "hs": h.get("score"), "as": a.get("score"),
                "state": st.get("type", {}).get("state"),
                "status": _zh_status(sport, st),
            }
    except Exception as ex:
        print(f"[espn] {path} 失敗: {ex}")
    return out


def fetch_scores():
    scores = {}
    for path, sport in BOARDS:
        scores.update(_board(path, sport))
    n_live = sum(1 for v in scores.values() if v["state"] == "in")
    print(f"[espn] 比分 {len(scores)} 場（進行中 {n_live}）")
    return scores


if __name__ == "__main__":
    sc = fetch_scores()
    for k, v in list(sc.items())[:10]:
        print(v["home"], v["hs"], ":", v["as"], v["away"], "|", v["state"], v["status"])
