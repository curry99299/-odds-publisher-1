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


def sports_list(host, feed):
    """抓運動總清單（一個請求回所有 id+名稱+場數），不需逐一掃描。"""
    for params in (
        f"lng=en&country=1&partner=1&virtualSports=false&groupChamps=true&getEmpty=true",
        f"lng=en&virtualSports=false&groupChamps=true",
    ):
        try:
            r = requests.get(f"{host}/service-api/{feed}/GetSportsShortZip?{params}",
                             headers=H, timeout=15, allow_redirects=False)
            if r.status_code == 200 and r.content:
                v = r.json().get("Value", [])
                if v:
                    return v
        except Exception:
            pass
    return []


def main():
    out = {"host_used": None, "sports": []}
    for host in HOSTS:
        rows = []
        for feed in ("LineFeed", "LiveFeed"):
            for s in sports_list(host, feed):
                rows.append({"feed": feed, "id": s.get("I"), "name": s.get("N", s.get("L", "")),
                             "champs": s.get("CI"), "games": s.get("GC")})
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
