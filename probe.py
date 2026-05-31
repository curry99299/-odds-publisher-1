"""一次性診斷：從執行環境(GitHub US)掃描 1xbet 各 sport id，找出 NBA/MLB 的正確 id。
結果上傳到 Supabase Storage 的 odds/probe.json（公開），供分析。
"""
import os
import json
import time
import requests

SUPA = os.environ.get("SUPABASE_URL", "https://yxpoqdihxnkxcnzebrwv.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
# accept header 是關鍵：少了會被回 406 NotAcceptable
H = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "accept": "application/json"}
HOSTS = ["https://1xbet.com", "https://1xbet.ng"]


def get(host, feed, sid):
    try:
        time.sleep(0.25)   # 放慢避免被限流
        r = requests.get(f"{host}/service-api/{feed}/Get1x2_VZip?sports={sid}&count=2&lng=en&mode=4&getEmpty=true",
                         headers=H, timeout=7, allow_redirects=False)
        if r.status_code == 200 and r.content:
            v = r.json().get("Value", [])
            if v:
                return v
    except Exception:
        return None
    return None


def main():
    out = {"host_used": None, "sports": []}
    for host in HOSTS:
        rows = []
        for feed in ("LineFeed", "LiveFeed"):
            for sid in range(1, 121):
                v = get(host, feed, sid)
                if v:
                    e = v[0]
                    rows.append({"feed": feed, "id": sid, "n": len(v),
                                 "league": e.get("LE", ""),
                                 "match": f"{e.get('O1','')} vs {e.get('O2','')}"})
        if rows:
            out["host_used"] = host
            out["sports"] = rows
            break

    body = json.dumps(out, ensure_ascii=False).encode()
    if KEY:
        r = requests.post(f"{SUPA}/storage/v1/object/odds/probe.json",
                          headers={"Authorization": f"Bearer {KEY}", "apikey": KEY,
                                   "Content-Type": "application/json", "x-upsert": "true"},
                          data=body, timeout=30)
        print(f"upload probe.json http={r.status_code} found={len(out['sports'])} host={out['host_used']}")
    else:
        print(body.decode())


if __name__ == "__main__":
    main()
