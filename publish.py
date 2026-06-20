"""更新賠率並發布到 Supabase Storage（公開檔），供線上 BetLedger 直接讀取（免重新部署）。

流程：fetch_all.run_once() 重新抓最新賠率 → 把 data/latest.json 上傳到
      Supabase Storage 的公開 bucket，覆寫 odds/latest.json。

繁中翻譯：不在雲端用 API 翻（不放金鑰）。譯名來自 repo 內建的
      data/team_translations.json（由 translate_pending.py 批次補齊後 commit/push），
      i18n 於 import 時載入；雲端只讀不寫。

需要環境變數：
  SUPABASE_URL          （預設用 BetLedger 專案）
  SUPABASE_SERVICE_KEY  （Supabase 後台 Settings→API 的 service_role key；務必保密、勿放前端）
  SUPABASE_BUCKET       （預設 "odds"；需先在後台建一個 Public bucket）

用法：
  SUPABASE_SERVICE_KEY=xxxx python3 publish.py          # 跑一次
  迴圈每 10 分鐘：見 publish_loop.sh；雲端自動：見 .github/workflows/odds-publish.yml
"""
import os
import json
import requests
from datetime import datetime, timezone
import fetch_all

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://yxpoqdihxnkxcnzebrwv.supabase.co").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET = os.environ.get("SUPABASE_BUCKET", "odds")
# SCORES_ONLY=1：只重抓 playsport 即時比分套到雲端現有快照（高頻刷新用，不重打 odds 來源）
SCORES_ONLY = os.environ.get("SCORES_ONLY") == "1"
OBJECT_PATH = "odds/latest.json"
PIN_OBJECT = "odds/pinnacle.json"   # Pinnacle 去水推薦（open/locked/events；自帶狀態，bet-tracker 公開讀取）
LATEST = os.path.join(os.path.dirname(__file__), "data", "latest.json")

# 收盤線改寫 Firestore（取代 Supabase）：透過 BetLedger 加密 ingest API，用共用密鑰。
# 結尾斜線必留（網站 trailingSlash）。INGEST_SECRET 設成 GitHub Actions repo secret。
INGEST_URL = os.environ.get("INGEST_URL", "https://of-site-26-156852458247.asia-east1.run.app/api/ingest/")
INGEST_SECRET = os.environ.get("INGEST_SECRET", "")


def capture_closings():
    """開賽前(約 0~13 分內)把 Pinnacle 獨贏賠率記成『收盤線』，upsert 到 Firestore closing_lines。
    每 5 分跑一次→開賽前最後一次快照即為收盤線（同場 upsert 覆寫，最接近開賽者勝出）。"""
    if not INGEST_SECRET:
        return
    try:
        with open(LATEST, encoding="utf-8") as f:
            events = json.load(f).get("events", [])
    except Exception:
        return
    now = datetime.now(timezone.utc)
    rows = []
    for e in events:
        start = e.get("start")
        if not start:
            continue
        try:
            st = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except Exception:
            continue
        mins = (st - now).total_seconds() / 60.0
        if not (-2 <= mins <= 13):          # 只記即將開賽的
            continue
        pin = (e.get("sources") or {}).get("pinnacle")
        if not pin or not (pin.get("home") or pin.get("away")):
            continue
        sport = e.get("sport")
        home = e.get("home_zh") or e.get("home")
        away = e.get("away_zh") or e.get("away")
        match_date = start[:10]
        # doc id ＝ 原 Supabase 複合鍵 (sport,home,away,match_date)，確保同場覆寫（取代 on_conflict）。
        doc_id = "|".join(str(x or "") for x in (sport, home, away, match_date)).replace("/", "_")
        rows.append({
            "id": doc_id,
            "sport": sport,
            "home": home,
            "away": away,
            "home_raw": e.get("home"),
            "away_raw": e.get("away"),
            "league": e.get("league_zh") or e.get("league"),
            "start": start,
            "match_date": match_date,
            "p_home": pin.get("home"),
            "p_draw": pin.get("draw"),
            "p_away": pin.get("away"),
            "captured_at": now.isoformat(),
            "updated_at": now.isoformat(),  # 與 captured_at 同值，留作將來 delete-stale 清理依據
        })
    if not rows:
        return
    try:
        r = requests.post(
            INGEST_URL,
            json={"secret": INGEST_SECRET, "op": "upsert", "table": "closing_lines", "rows": rows},
            timeout=30,
        )
        print(f"[closings] 記錄 {len(rows)} 場收盤線 (Firestore) http={r.status_code}"
              + ("" if r.ok else f" {r.text[:200]}"))
    except Exception as ex:
        print(f"[closings] 失敗: {ex}")


def publish_pinnacle():
    """Pinnacle 去水推薦：讀 latest.json → 取上一份 pinnacle.json 當狀態 → 算 open/locked/結算 → 覆寫上傳。"""
    import pinnacle_picks
    try:
        with open(LATEST, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[pinnacle] 讀 latest.json 失敗: {e}")
        return
    prev = {}
    try:
        pub = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{PIN_OBJECT}"
        r = requests.get(pub + f"?t={os.getpid()}", timeout=20)
        if r.status_code == 200 and r.content:
            prev = r.json()
    except Exception as e:
        print(f"[pinnacle] 下載上一份失敗（首次或無檔，略過）: {e}")
    out = pinnacle_picks.build(data.get("events", []), data.get("results", []), prev)
    body = json.dumps(out, ensure_ascii=False).encode("utf-8")
    if not SERVICE_KEY:
        print(f"[pinnacle] 未設 SERVICE_KEY，僅計算 open {len(out['open'])}/locked {len(out['locked'])}，未上傳")
        return
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{PIN_OBJECT}"
    headers = {"Authorization": f"Bearer {SERVICE_KEY}", "apikey": SERVICE_KEY, "Content-Type": "application/json", "x-upsert": "true", "cache-control": "max-age=60"}
    try:
        r = requests.post(url, headers=headers, data=body, timeout=30)
        print(f"[pinnacle] open {len(out['open'])} / locked {len(out['locked'])} / events {len(out['events'])} → http={r.status_code}"
              + ("" if r.ok else f" {r.text[:200]}"))
    except Exception as ex:
        print(f"[pinnacle] 上傳失敗: {ex}")


def publish(scores_only=SCORES_ONLY):
    # 0) 先把雲端「上一份」下載成本機 latest.json，run_once 才能算出漲跌%
    #    （GitHub Actions 每次都是全新環境，沒有這步就沒有上一份可比）
    try:
        pub = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{OBJECT_PATH}"
        r = requests.get(pub + f"?t={os.getpid()}", timeout=20)
        if r.status_code == 200 and r.content:
            os.makedirs(os.path.dirname(LATEST), exist_ok=True)
            with open(LATEST, "wb") as f:
                f.write(r.content)
            print("[publish] 已下載雲端上一份作為漲跌基準")
    except Exception as e:
        print(f"[publish] 下載上一份失敗（首次或無檔，略過）: {e}")

    # 1) 重新抓資料：高頻分數刷新只重抓 playsport 比分；完整模式才重抓所有 odds 來源＋終場
    if scores_only:
        if fetch_all.refresh_scores_only() is None:
            return False   # 沒有雲端上一份可刷新（首跑），交給完整模式
    else:
        fetch_all.run_once()      # run_once 內會讀上面那份算漲跌
        # 1.5) 記錄即將開賽比賽的 Pinnacle 收盤線（供 CLV 分析）
        capture_closings()
        # 1.6) Pinnacle 去水推薦（open/locked/結算）→ odds/pinnacle.json
        publish_pinnacle()

    if not SERVICE_KEY:
        print("[publish] 未設定 SUPABASE_SERVICE_KEY，僅更新本機 latest.json（未上傳雲端）")
        return False

    with open(LATEST, "rb") as f:
        body = f.read()

    # 2) 上傳/覆寫到 Storage（x-upsert 允許覆寫）
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{OBJECT_PATH}"
    headers = {
        "Authorization": f"Bearer {SERVICE_KEY}",
        "apikey": SERVICE_KEY,
        "Content-Type": "application/json",
        "x-upsert": "true",
        "cache-control": "max-age=15",   # 降快取→高頻刷新時前端能更快讀到新比分
    }
    r = requests.post(url, headers=headers, data=body, timeout=30)
    if r.status_code in (200, 201):
        pub = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{OBJECT_PATH}"
        print(f"[publish] 已上傳（{len(body)} bytes）→ {pub}")
        return True
    print(f"[publish] 上傳失敗 http={r.status_code}: {r.text[:300]}")
    return False


def _live_count():
    """讀本機 latest.json 算現在有幾場 live。"""
    try:
        with open(LATEST, encoding="utf-8") as f:
            return sum(1 for e in json.load(f).get("events", []) if e.get("live"))
    except Exception:
        return 0


def _activity_from_cloud():
    """看雲端上一份：是否有 live、是否有場次正在/即將比賽(now−15min ~ now+6h)、距上次更新幾秒。
    供自適應決定要不要跑這一輪。回傳 (had_live, has_active, age_sec)。"""
    from datetime import datetime, timezone
    try:
        pub = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{OBJECT_PATH}"
        d = requests.get(pub + f"?t={os.getpid()}", timeout=20).json()
    except Exception:
        return (False, True, 99999)   # 讀不到 → 當作要跑（保守）
    now = datetime.now(timezone.utc)
    had_live = any(e.get("live") for e in d.get("events", []))
    has_active = False
    for e in d.get("events", []):
        st = e.get("start")
        if not st:
            continue
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        except Exception:
            continue
        mins = (dt - now).total_seconds() / 60.0
        if -360 <= mins <= 15:        # 已開賽 6 小時內 或 15 分內即將開賽 ＝ 該有滾球/即將有
            has_active = True
            break
    try:
        age = (now - datetime.fromisoformat(d.get("updated_at", "").replace("Z", "+00:00"))).total_seconds()
    except Exception:
        age = 99999
    return (had_live, has_active, age)


def adaptive():
    """自適應頻率：有滾球(或即將開賽)→ 完整跑一次再每 30 秒高頻刷分數約 4 分鐘；
    完全閒置 → 每 ~10 分鐘才完整跑一次（cron 每 5 分觸發，閒置時隔輪跳過）。"""
    import time
    had_live, has_active, age = _activity_from_cloud()
    # 閒置降頻：沒有 live、沒有正在/即將比賽，且距上次更新不到 ~9.5 分 → 這輪跳過（達成閒置約 10 分）
    if not had_live and not has_active and age < 9.5 * 60:
        print(f"[adaptive] 閒置降頻：無 live／無即將開賽，距上次更新 {int(age)}s(<570s) → 跳過此輪")
        return
    publish(scores_only=False)                 # 完整跑（賠率＋比分＋終場）並上傳
    live = _live_count()
    print(f"[adaptive] 完整跑完成，live={live}")
    if live <= 0:
        print("[adaptive] 無滾球 → 不進高頻，等下一輪")
        return
    end = time.monotonic() + 240               # 有滾球 → 每 30 秒只刷比分，約 4 分鐘
    while time.monotonic() < end:
        time.sleep(30)
        try:
            publish(scores_only=True)
        except Exception as e:                 # noqa: BLE001
            print(f"[adaptive] 高頻刷新略過: {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "adaptive":
        adaptive()
    else:
        publish()
