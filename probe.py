"""一次性診斷：抓一場 1xbet 賽事的完整盤口(E 陣列)，找讓分/大小分的代碼與線值。
結果上傳 Supabase Storage odds/probe.json。
"""
import os
import json
import requests

SUPA = os.environ.get("SUPABASE_URL", "https://yxpoqdihxnkxcnzebrwv.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
H = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "accept": "application/json"}
HOSTS = ["https://1xbet.com", "https://1xbet.ng"]


def main():
    out = {"host": None, "events": []}
    for host in HOSTS:
        try:
            r = requests.get(f"{host}/service-api/LineFeed/Get1x2_VZip?sports=1&count=3&lng=en&mode=4&getEmpty=true",
                             headers=H, timeout=15, allow_redirects=False)
            if r.status_code != 200 or not r.content:
                continue
            v = r.json().get("Value", [])
            if not v:
                continue
            out["host"] = host
            for e in v[:2]:
                out["events"].append({
                    "O1": e.get("O1"), "O2": e.get("O2"), "LE": e.get("LE"),
                    # E 陣列：每個盤口 {T:類型, G:群組, P:線值, C:賠率}
                    "E": [{"T": x.get("T"), "G": x.get("G"), "P": x.get("P"), "C": x.get("C")}
                          for x in (e.get("E") or [])],
                })
            break
        except Exception as ex:
            out["err"] = str(ex)
    body = json.dumps(out, ensure_ascii=False).encode()
    if KEY:
        r = requests.post(f"{SUPA}/storage/v1/object/odds/probe.json",
                          headers={"Authorization": f"Bearer {KEY}", "apikey": KEY,
                                   "Content-Type": "application/json", "x-upsert": "true"},
                          data=body, timeout=30)
        print(f"upload http={r.status_code} host={out['host']} events={len(out['events'])}")
    else:
        print(body.decode()[:2000])


if __name__ == "__main__":
    main()
