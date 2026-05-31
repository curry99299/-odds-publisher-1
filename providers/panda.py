"""熊貓體育 (Panda Sports) — 容錯型 provider。

熊貓體育為需登入、且有地區/裝置風控的亞洲體育博彩平台，賠率由登入後的
JS 前端載入，無公開賠率 API。此 provider 嘗試以無頭瀏覽器讀取，失敗則回 []。
受 ENABLE_PANDA 控制（預設關閉，因需有效帳號與可達域名）。

要啟用：設定環境變數
  ENABLE_PANDA=1
  PANDA_URL=<熊貓體育登入後的賽事賠率頁網址>
  PANDA_ACCOUNT / PANDA_PASSWORD
並依實際 DOM/網路結構補上 _parse() 解析。
"""
import os
import json
import re

URL = os.environ.get("PANDA_URL", "")
ACCOUNT = os.environ.get("PANDA_ACCOUNT", "")
PASSWORD = os.environ.get("PANDA_PASSWORD", "")


def _looks_like_odds(obj):
    s = json.dumps(obj)[:5000] if not isinstance(obj, str) else obj[:5000]
    return ("odds" in s.lower() or "price" in s.lower()) and bool(re.search(r"\d+\.\d{2}", s))


def fetch():
    if os.environ.get("ENABLE_PANDA", "0") != "1":
        print("[panda] 已停用（需設 ENABLE_PANDA=1 + 帳號 + PANDA_URL）")
        return []
    if not URL:
        print("[panda] 未設定 PANDA_URL")
        return []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[panda] playwright 不可用: {e}")
        return []

    captured = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--disable-blink-features=AutomationControlled", "--no-sandbox"])
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            page = ctx.new_page()
            page.on("response", lambda r: _maybe(r, captured))
            page.goto(URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(8000)
            browser.close()
    except Exception as e:
        print(f"[panda] 流程失敗: {e}")
        return []

    rows = _parse(captured)
    print(f"[panda] 攔截 {len(captured)} 筆、解析 {len(rows)} 場")
    return rows


def _maybe(resp, captured):
    try:
        if "json" in resp.headers.get("content-type", ""):
            b = resp.json()
            if _looks_like_odds(b):
                captured.append(b)
    except Exception:
        pass


def _parse(captured):
    return []


if __name__ == "__main__":
    print(fetch())
