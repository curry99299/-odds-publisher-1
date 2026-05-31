"""一次性診斷：用 onexbet 相同的可行方式（accept header + host fallback），
精準測試少數候選 sport id（放慢避免限流），找出籃球/棒球的 id。
結果上傳 Supabase Storage odds/probe.json。
"""
import os
import json
import time
import requests

SUPA = os.environ.get("SUPABASE_URL", "https://yxpoqdihxnkxcnzebrwv.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
H = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "accept": "application/json"}
HOSTS = ["https://1xbet.com", "https://1xbet.ng"]
# 1xbet 常見候選 id（含足球1做對照、板球66確認）
CAND = [1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 17, 29, 40, 66, 85, 107, 128]


def get_feed(sid, feed):
    for host in HOSTS:
        try:
            time.sleep(0.8)   # 放慢，避免限流
            r = requests.get(f"{host}/service-api/{feed}/Get1x2_VZip?sports={sid}&count=2&lng=en&mode=4&getEmpty=true",
                             headers=H, timeout=12, allow_redirects=False)
            if r.status_code == 200 and r.content:
                v = r.json().get("Value", [])
                if v:
                    return v, host
        except Exception:
            pass
    return None, None


def main():
    rows = []
    for feed in ("LineFeed", "LiveFeed"):
        for sid in CAND:
            v, host = get_feed(sid, feed)
            if v:
                e = v[0]
                rows.append({"feed": feed, "id": sid, "host": host, "n": len(v),
                             "league": e.get("LE", ""),
                             "match": f"{e.get('O1','')} vs {e.get('O2','')}"})
    out = {"sports": rows}
    body = json.dumps(out, ensure_ascii=False).encode()
    if KEY:
        r = requests.post(f"{SUPA}/storage/v1/object/odds/probe.json",
                          headers={"Authorization": f"Bearer {KEY}", "apikey": KEY,
                                   "Content-Type": "application/json", "x-upsert": "true"},
                          data=body, timeout=30)
        print(f"upload http={r.status_code} found={len(rows)}")
    else:
        print(body.decode())


if __name__ == "__main__":
    main()
