"""【給 -odds-publisher-1 這個 GitHub repo 用的自含版本】
把 Playsport 目前盤口發布成 BetLedger 前端可吃的 JSON → 上傳 Supabase Storage：odds/playsport/latest.json

與 sportslottery/publish.py 同功能，但「自含」：只依賴 fetch_playsport.py（requests + lxml，repo 已有），
不 import scheduler.py / db.py，方便直接放進 odds-publisher repo 跑 GitHub Actions。

部署到 -odds-publisher-1：
  1) 把 sportslottery/fetch_playsport.py 複製到 repo 根目錄
  2) 把這支 playsport_publish.py 複製到 repo 根目錄
  3) 新增 .github/workflows/playsport-publish.yml（見對話內 YAML）
  requirements.txt 已含 requests + lxml，免改。

公開讀取網址（BetLedger 已指向）：
  https://<project>.supabase.co/storage/v1/object/public/odds/playsport/latest.json
"""
import os
import json
import datetime
import requests

from fetch_playsport import fetch_many

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://yxpoqdihxnkxcnzebrwv.supabase.co").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET = os.environ.get("SUPABASE_BUCKET", "odds")
OBJECT_PATH = "playsport/latest.json"

# 要抓的聯盟（與 sportslottery/scheduler.py 的 DEFAULT_ALLIANCES 一致）：
# 1=MLB 6=中職 3=NBA 4=足球 7=NHL 9=日職 18=WNBA 94=中籃
DEFAULT_ALLIANCES = [1, 6, 3, 4, 7, 9, 18, 94]

SPORT_BY_ALLIANCE = {
    1: "baseball", 2: "baseball", 6: "baseball", 9: "baseball", 16: "baseball",
    3: "basketball", 18: "basketball", 94: "basketball",
    4: "football", 7: "hockey", 91: "tennis",
}


def build_payload():
    raw = fetch_many(DEFAULT_ALLIANCES)
    games = []
    for g in raw:
        o = g.get("odds", {}) or {}
        ml, handi, total = o.get("normal", {}), o.get("handi", {}), o.get("total", {})
        games.append({
            "id": g.get("id"),
            "league": g.get("league"),
            "sport": SPORT_BY_ALLIANCE.get(g.get("alliance_id"), "other"),
            "date": g.get("date"),
            "time": g.get("time"),
            "home": g.get("home"),
            "away": g.get("away"),
            "ml": {"h": ml.get("h"), "a": ml.get("a")},
            "handicap": {"line": handi.get("line"), "h": handi.get("h"), "a": handi.get("a")},
            "total": {"line": total.get("line"), "o": total.get("o"), "u": total.get("u")},
        })
    return {"updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "count": len(games), "games": games}


def publish():
    payload = build_payload()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    print(f"[playsport-publish] 抓到 {payload['count']} 場")
    if not SERVICE_KEY:
        print("[playsport-publish] 未設 SUPABASE_SERVICE_KEY，跳過上傳")
        return False
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{OBJECT_PATH}"
    headers = {
        "Authorization": f"Bearer {SERVICE_KEY}", "apikey": SERVICE_KEY,
        "Content-Type": "application/json", "x-upsert": "true", "cache-control": "max-age=60",
    }
    r = requests.post(url, headers=headers, data=body, timeout=30)
    if r.status_code in (200, 201):
        print(f"[playsport-publish] 已上傳 {len(body)} bytes → {SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{OBJECT_PATH}")
        return True
    print(f"[playsport-publish] 上傳失敗 http={r.status_code}: {r.text[:300]}")
    return False


if __name__ == "__main__":
    publish()
