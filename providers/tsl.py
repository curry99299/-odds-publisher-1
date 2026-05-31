"""台灣運彩 — 透過 JBot 運動數據 API 抓賠率。

文件： https://sportsbot.tech/api/sportslottery/
端點： GET https://api.sportsbot.tech/v2/odds
參數： sport（NBA/MLB/CPB/NPB/KBO/FTB/ICE/TNS/...）、date(YYYY-MM-DD)、mode(close/open/both/all)
標頭： X-JBot-Token: <密鑰>

重要限制（務必遵守，否則封 IP）：
  • 所有呼叫間隔必須 > 5 秒
  • 免費試用密鑰「禁止用於定期排程」→ 本系統 10 分鐘自動更新請務必用「付費密鑰」（300 次/日）
本 provider 內建：≥5.2 秒間隔、每日額度上限、快取（預設每 20 分鐘才真正打 API，其餘回快取），
以把每日用量壓在 300 次以內。需設環境變數 JBOT_TOKEN 才啟用。
"""
import os
import time
import json
import datetime
import requests
from core.models import MatchOdds
from core.i18n import en_from_zh

API = "https://api.sportsbot.tech/v2/odds"
TOKEN = os.environ.get("JBOT_TOKEN", "")

# 我們的 sport → JBot sport 代碼（可用 TSL_SPORTS 覆寫，逗號分隔的我方 sport）
# 棒球含 美職MLB、中職CPB、日職NPB、韓職KBO
SPORT_MAP = {"soccer": ["FTB"], "basketball": ["NBA"], "baseball": ["MLB", "CPB", "NPB", "KBO"]}
# JBot sport 代碼 → 我們的 sport（反查，給多碼用）
JBOT_TO_SPORT = {"FTB": "soccer", "NBA": "basketball", "BSK": "basketball",
                 "MLB": "baseball", "CPB": "baseball", "NPB": "baseball",
                 "KBO": "baseball", "BSE": "baseball"}

MIN_GAP = 5.2                      # 單次呼叫間隔（>5 秒規定）
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
STATE = os.path.join(DATA_DIR, "tsl_state.json")
CACHE = os.path.join(DATA_DIR, "tsl_cache.json")
CACHE_TTL = int(os.environ.get("TSL_CACHE_TTL", "1800"))   # 30 分鐘才真正打 API（聯盟多、控用量）
DAILY_CAP = int(os.environ.get("TSL_DAILY_CAP", "280"))    # 每日額度上限（< 300）


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


def _budget_ok(n_calls):
    """檢查今日是否還有額度，扣除後回傳是否可呼叫。"""
    today = datetime.date.today().isoformat()
    st = _load(STATE, {})
    if st.get("date") != today:
        st = {"date": today, "count": 0}
    if st["count"] + n_calls > DAILY_CAP:
        return False, st
    return True, st


def _sport_codes():
    env = os.environ.get("TSL_SPORTS")
    if env:
        codes = []
        for s in env.split(","):
            codes += SPORT_MAP.get(s.strip(), [s.strip().upper()])
        return codes
    return [c for codes in SPORT_MAP.values() for c in codes]


def _to_float(v):
    try:
        f = float(v)
        return f if f > 1.0 else None     # 歐式賠率必 > 1
    except (TypeError, ValueError):
        return None


def _taipei_to_utc(s):
    """JBot time 為台灣時間（無時區），如 '2026-05-31T00:00' → 轉 UTC ISO。"""
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(s)
        dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _parse(sport, payload):
    """解析 JBot /v2/odds 回應。

    結構：{"data":[{home,away,league,time,odds:[{normal:{h,a,t,...}, handi, total,...}]}],...}
    normal = 不讓分/獨贏盤：h=主勝、a=客勝、t=和局（僅足球）。隊名為繁中，轉英文標準名以利配對。
    """
    games = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(games, list):
        return []
    rows = []
    for g in games:
        if not isinstance(g, dict):
            continue
        home_zh, away_zh = g.get("home"), g.get("away")
        odds_list = g.get("odds") or []
        if not home_zh or not away_zh or not odds_list:
            continue
        normal = (odds_list[0] or {}).get("normal") or {}
        ho = _to_float(normal.get("h"))
        ao = _to_float(normal.get("a"))
        do = _to_float(normal.get("t")) if sport == "soccer" else None
        if ho is None and ao is None:
            continue
        rows.append(MatchOdds(
            source="tsl", sport=sport,
            home=en_from_zh(str(home_zh)), away=en_from_zh(str(away_zh)),
            start=_taipei_to_utc(g.get("time")),
            league=str(g.get("league", "")),
            home_odds=ho, draw_odds=do, away_odds=ao,
            url="https://www.sportslottery.com.tw/",
        ))
    return rows


def fetch():
    if not TOKEN:
        print("[tsl] 未設定 JBOT_TOKEN（台灣運彩 API 需付費密鑰用於排程；免費密鑰禁止排程會封 IP）")
        return []

    # 快取：未過 TTL 直接回上次結果，控制每日用量
    cache = _load(CACHE, {})
    now = time.time()
    if cache.get("ts") and now - cache["ts"] < CACHE_TTL and cache.get("rows"):
        rows = [MatchOdds(**r) for r in cache["rows"]]
        print(f"[tsl] 用快取 {len(rows)} 場（{int((now-cache['ts'])//60)} 分鐘前）")
        return rows

    codes = _sport_codes()
    ok, st = _budget_ok(len(codes))
    if not ok:
        print(f"[tsl] 今日額度已達上限 {DAILY_CAP}，跳過（明日重置）")
        return [MatchOdds(**r) for r in cache.get("rows", [])]

    date = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
    headers = {"X-JBot-Token": TOKEN, "user-agent": "odds-compare/1.0"}
    rows = []
    for i, code in enumerate(codes):
        if i:
            time.sleep(MIN_GAP)     # 遵守 >5 秒間隔
        try:
            r = requests.get(API, headers=headers,
                             params={"sport": code, "date": date, "mode": "close"}, timeout=20)
            data = r.json()
            if isinstance(data, dict) and data.get("status") and "data" not in data:
                print(f"[tsl] {code} API 訊息: {data.get('status')}")
                continue
            rows += _parse(JBOT_TO_SPORT.get(code, "soccer"), data)
            st["count"] += 1
        except Exception as e:
            print(f"[tsl] {code} 抓取失敗: {e}")
    _save(STATE, st)
    _save(CACHE, {"ts": now, "rows": [r.to_dict() for r in rows]})
    print(f"[tsl] 取得 {len(rows)} 場（今日已用 {st['count']}/{DAILY_CAP}）")
    return rows


if __name__ == "__main__":
    for r in fetch()[:8]:
        print(r.to_dict())
