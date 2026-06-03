"""playsport 即時比分（/livescore/{allianceid}）。

中職/日職/韓職等 ESPN 未涵蓋的聯賽，靠這裡補滾球比分＋中文局數/節數（如「4局上」「第3節」）。
用於 fetch_all 以「playsport 優先」覆寫 live 比分。

頁面結構（各 alliance 通用，以 gameid 為前綴的 id）：
  {gid}_aname / {gid}_hname  → 客/主隊名（短中文）
  {gid}_as_b  / {gid}_hs_b   → 客/主分
  {gid}_inning               → 中文進度（局上/局下/第N節/上下半…）

中職（aid=6）等沒有 {gid}_inning 元素，改以每局得分欄 {gid}_as1.._hs1.. 推算局數，
狀態文字看 {gid}_addinfo（「比賽結束」=終場）。故以 {gid}_aname 為錨點掃所有場。
"""
import re

import requests
from lxml import html as LH

from providers.playsport_results import PS_EN  # 短中文→英文（NPB/KBO/CPBL），供英文 token 比對

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
LIVE = "https://www.playsport.cc/livescore/{aid}"

# alliance → 我們的 sport（playsport livescore 支援的主要聯賽）
ALLIANCES = {
    1: "baseball", 2: "baseball", 6: "baseball", 9: "baseball",   # MLB / 日職 / 中職 / 韓職
    3: "basketball", 7: "basketball",                              # NBA / WNBA
    4: "soccer",                                                   # 足球
    91: "hockey",                                                  # NHL
}

# 已結束/未開賽的進度字樣（不是進行中，不採用）
_DONE = re.compile(r"結束|完場|完賽|未開賽|取消|延賽|保留|PPD", re.I)


def _get(url, tries=2):
    """抓頁面；playsport 從海外 IP 偶爾慢/丟包，故重試＋較長 timeout，降低漏抓一輪的機率。"""
    last = None
    for _ in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=25)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def _txt(doc, eid):
    v = doc.xpath(f'string(//*[@id="{eid}"])')
    return (v or "").strip()


def _inning_from_box(doc, gid):
    """棒球無 _inning 元素時（中職），用每局得分欄推算當前局數＋上下半。
    取最後一個有得分（任一隊）的局；該局主隊欄已填→局下（主隊進攻中），否則局上。"""
    last = 0
    home_filled = False
    for i in range(1, 16):
        a = _txt(doc, f"{gid}_as{i}")
        h = _txt(doc, f"{gid}_hs{i}")
        if a.isdigit() or h.isdigit():
            last = i
            home_filled = h.isdigit()
    if not last:
        return ""
    return f"{last}局{'下' if home_filled else '上'}"


def _parse(aid, sport, text):
    doc = LH.fromstring(text)
    en_map = PS_EN.get(aid, {})
    out = []
    seen = set()
    # 以 _aname 為錨點掃所有場（_inning 不是每個聯賽都有；中職就沒有）
    for el in doc.xpath('//*[contains(@id,"_aname")]'):
        gid = el.get("id").replace("_aname", "")
        if gid in seen:
            continue
        seen.add(gid)
        a_zh, h_zh = _txt(doc, gid + "_aname"), _txt(doc, gid + "_hname")
        as_, hs = _txt(doc, gid + "_as_b"), _txt(doc, gid + "_hs_b")
        if not a_zh or not h_zh or not hs.isdigit() or not as_.isdigit():
            continue
        # 狀態：優先 _inning 元素；沒有就用每局得分推（中職）。終場/未開賽 → 不算 live
        status = _txt(doc, gid + "_inning")
        addinfo = _txt(doc, gid + "_addinfo")
        if _DONE.search(status) or _DONE.search(addinfo):
            continue
        if not status:
            status = _inning_from_box(doc, gid)
        if not status:  # 無局數可推 = 多半未開賽
            continue
        out.append({
            "sport": sport, "alliance": aid,
            "home_zh": h_zh, "away_zh": a_zh,
            "home_en": en_map.get(h_zh, ""), "away_en": en_map.get(a_zh, ""),
            "home_score": int(hs), "away_score": int(as_),
            "status": status,
        })
    return out


def fetch_live():
    """回傳所有進行中的場：含 home/away zh+en、比分、中文局數/節數。"""
    games = []
    for aid, sport in ALLIANCES.items():
        try:
            games += _parse(aid, sport, _get(LIVE.format(aid=aid)))
        except Exception as e:  # noqa: BLE001 - 單一聯賽失敗不影響其他
            print(f"[playsport_live] aid={aid} 失敗: {e}")
    print(f"[playsport_live] 即時比分 {len(games)} 場")
    return games


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_live(), ensure_ascii=False, indent=2))
