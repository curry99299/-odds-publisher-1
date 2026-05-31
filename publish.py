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
import requests
import fetch_all

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://yxpoqdihxnkxcnzebrwv.supabase.co").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET = os.environ.get("SUPABASE_BUCKET", "odds")
OBJECT_PATH = "odds/latest.json"
LATEST = os.path.join(os.path.dirname(__file__), "data", "latest.json")


def publish():
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

    # 1) 重新抓最新賠率（run_once 內會讀上面那份算漲跌）
    fetch_all.run_once()

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
        "cache-control": "max-age=60",
    }
    r = requests.post(url, headers=headers, data=body, timeout=30)
    if r.status_code in (200, 201):
        pub = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{OBJECT_PATH}"
        print(f"[publish] 已上傳（{len(body)} bytes）→ {pub}")
        return True
    print(f"[publish] 上傳失敗 http={r.status_code}: {r.text[:300]}")
    return False


if __name__ == "__main__":
    publish()
