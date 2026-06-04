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
import datetime
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

# alliance → 聯盟中文（供前端 leagueKey 比對；終場補洞用非棒球那組）
_LEAGUE_ZH_ALL = {1: "MLB", 2: "日本職棒", 6: "中華職棒", 9: "韓國職棒",
                  3: "NBA", 7: "WNBA", 4: "足球", 91: "NHL"}
_LEAGUE_ZH = {3: "NBA", 7: "WNBA", 4: "足球", 91: "NHL"}


def _today_tpe():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")


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


def _first(doc, gid, sufs):
    """依序試多個欄位後綴，回第一個非空值（各聯賽 id 命名不一：棒球 _as_b、籃球 _asr…）。"""
    for s in sufs:
        v = _txt(doc, gid + s)
        if v:
            return v
    return ""


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


# 各聯賽欄位命名不一，逐一 fallback：
#   隊名：_aname/_hname（棒球）、_aname_big/_hname_big、_atn/_htn（籃球主打場）
#   比分：_as_b/_hs_b（棒球總分）、_asr/_hsr、_asr_big/_hsr_big（籃球總分）
#   狀態：_inning（棒球）、_inning_big（籃球「第N節」），都沒有就用每局得分推（中職）
_AWAY_NAME = ["_aname", "_aname_big", "_atn"]
_HOME_NAME = ["_hname", "_hname_big", "_htn"]
_AWAY_SCORE = ["_as_b", "_asr", "_asr_big"]
_HOME_SCORE = ["_hs_b", "_hsr", "_hsr_big"]
_STATUS = ["_inning", "_inning_big"]


def _parse(aid, sport, text, want_done=False):
    """want_done=False 回傳進行中的場；True 回傳已結束的場（終場比分，供非棒球終場補洞）。"""
    doc = LH.fromstring(text)
    en_map = PS_EN.get(aid, {})
    out = []
    seen = set()
    # 用 regex 收集所有 gid（數字 id）：_aname/_aname_big 都含「_aname」、籃球另有 _atn
    gids = set(re.findall(r"(\d+)_aname", text)) | set(re.findall(r"(\d+)_atn", text))
    for gid in gids:
        if gid in seen:
            continue
        seen.add(gid)
        a_zh, h_zh = _first(doc, gid, _AWAY_NAME), _first(doc, gid, _HOME_NAME)
        as_, hs = _first(doc, gid, _AWAY_SCORE), _first(doc, gid, _HOME_SCORE)
        if not a_zh or not h_zh or not hs.isdigit() or not as_.isdigit():
            continue
        # 狀態：優先 _inning/_inning_big；沒有就用每局得分推（中職）。終場/未開賽 → 不算 live
        status = _first(doc, gid, _STATUS)
        addinfo = _txt(doc, gid + "_addinfo")
        is_done = bool(_DONE.search(status) or _DONE.search(addinfo))
        if want_done:
            # 只要「已結束且有比分」的場 → 當終場回傳（home:away 字串，供前端對齊／結算）
            if not is_done:
                continue
            out.append({
                "sport": sport, "alliance": aid,
                "league_zh": _LEAGUE_ZH.get(aid, ""), "league": _LEAGUE_ZH.get(aid, ""),
                "home_zh": h_zh, "away_zh": a_zh,
                "home": en_map.get(h_zh, ""), "away": en_map.get(a_zh, ""),
                "score": f"{hs}:{as_}",   # home:away
                "final": True, "live": False, "date": _today_tpe(),
            })
            continue
        if is_done:
            continue
        if not status:
            status = _inning_from_box(doc, gid)
        if not status:  # 無局數/節數可推 = 多半未開賽
            continue
        trm = _txt(doc, gid + "_trm_big")  # 籃球剩餘時間「06:49」→ 併進狀態「第3節 06:49」
        if trm and "節" in status:
            status = f"{status} {trm}"
        # 棒球攻守：用局數上下半判定（棒球鐵則：局上＝客隊打、局下＝主隊打）。
        # 注意 _showTeam 不可靠（實測 8 局上卻回傳主隊），故不採用。
        bat = ""
        if sport == "baseball":
            if "局上" in status:
                bat = "away"
            elif "局下" in status:
                bat = "home"
        out.append({
            "sport": sport, "alliance": aid, "league_zh": _LEAGUE_ZH_ALL.get(aid, ""),
            "home_zh": h_zh, "away_zh": a_zh,
            "home_en": en_map.get(h_zh, ""), "away_en": en_map.get(a_zh, ""),
            "home_score": int(hs), "away_score": int(as_),
            "status": status, "bat": bat,   # 'home'|'away'|''＝目前打擊方（棒球）
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


def fetch_finals():
    """即時比分頁裡『已結束』的非棒球場（NBA/WNBA/足球/冰球）終場比分。
    playsport_results 只收棒球終場，籃球等一打完 feed 就沒比分→前端卡在滾球中，故在此補洞。"""
    games = []
    for aid, sport in ALLIANCES.items():
        if sport == "baseball":   # 棒球終場走 playsport_results（有日期、含昨天、較完整）
            continue
        try:
            games += _parse(aid, sport, _get(LIVE.format(aid=aid)), want_done=True)
        except Exception as e:  # noqa: BLE001
            print(f"[playsport_live] finals aid={aid} 失敗: {e}")
    print(f"[playsport_live] 終場(非棒球) {len(games)} 場")
    return games


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_live(), ensure_ascii=False, indent=2))
