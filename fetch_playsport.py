"""從 playsport 的「預測賽事」頁抓台灣運彩賽前盤口。

來源： GET https://www.playsport.cc/predict/games?allianceid={aid}
回傳的是伺服器渲染的 HTML，每場比賽 = 兩個 <tr gameid>（客隊列 + 主隊列）。

欄位對應（每一列）：
  td-gameinfo  → 運彩編號(h3) + 開賽時間(h4)，只在客隊那一列出現
  td-teaminfo  → 隊名(h3) + 先發投手(p)
  td-bank-bet01 (rel=ap)  → 讓分： strong=讓分線(±1.5)  span=賠率
  td-bank-bet03 (rel=pkp) → 不讓/獨贏(moneyline)： span=賠率（無線）
  td-bank-bet02 (rel=bp)  → 大小(total)： strong=總分線  span=賠率

對應到 sportsbot 的命名： pkp→normal、ap→handi、bp→total。
"""
import re
import datetime
from collections import OrderedDict

import requests
from lxml import html as LH

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
BASE = "https://www.playsport.cc/predict/games?allianceid={aid}"

# allianceid → 聯盟名稱（依 playsport 的 livescore 選單）
ALLIANCES = {
    1: "MLB", 2: "中職", 3: "NBA", 4: "足球",
    6: "中職", 7: "NHL", 9: "日職", 16: "韓職",
    18: "WNBA", 91: "網球", 94: "中籃",
}


def _txt(el):
    return el.text_content().strip() if el is not None else ""


def _odds(td):
    """從 data-wrap 取最後一個 span 文字當賠率，去掉前綴逗號。"""
    spans = td.xpath('.//span[@class="data-wrap"]/span')
    if not spans:
        return None
    v = _txt(spans[-1]).lstrip(", ").strip()
    try:
        return float(v)
    except ValueError:
        return None


def _line(td):
    strong = td.xpath('.//strong[not(contains(@class,"team-side"))]')
    v = _txt(strong[0]) if strong else ""
    try:
        return float(v)
    except ValueError:
        return None


def _cell(tr, cls):
    tds = tr.xpath(f'.//td[contains(@class,"{cls}")]')
    return tds[0] if tds else None


def _gameid_to_date(gid):
    """gameid 形如 2026060311001 → 開頭 8 碼為日期 YYYYMMDD。"""
    m = re.match(r"(\d{4})(\d{2})(\d{2})", gid or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def fetch_alliance(aid, timeout=25):
    """抓單一聯盟的賽前盤，回傳 list[dict]。每個 dict：
        {id, league, date, time, away, home, odds:{normal,handi,total}}
    其中 odds 各鍵為 {a,h}（讓分/獨贏）或 {line,o,u}（大小）。
    """
    url = BASE.format(aid=aid)
    resp = requests.get(url, headers=UA, timeout=timeout)
    resp.raise_for_status()
    doc = LH.fromstring(resp.text)

    games = OrderedDict()
    for tr in doc.xpath("//tr[@gameid]"):
        games.setdefault(tr.get("gameid"), []).append(tr)

    out = []
    for gid, trs in games.items():
        if len(trs) < 2:
            continue
        away_tr, home_tr = trs[0], trs[1]

        info = _cell(away_tr, "td-gameinfo")
        code = _txt(info.xpath(".//h3")[0]) if info is not None and info.xpath(".//h3") else ""
        gtime = _txt(info.xpath(".//h4")[0]) if info is not None and info.xpath(".//h4") else ""

        def team(tr):
            h3 = tr.xpath('.//td[contains(@class,"td-teaminfo")]//h3')
            return _txt(h3[0]) if h3 else ""

        def side_odds(tr):
            ap, pk, bp = _cell(tr, "td-bank-bet01"), _cell(tr, "td-bank-bet03"), _cell(tr, "td-bank-bet02")
            return {
                "handi_line": _line(ap) if ap is not None else None,
                "handi_odds": _odds(ap) if ap is not None else None,
                "ml_odds": _odds(pk) if pk is not None else None,
                "total_line": _line(bp) if bp is not None else None,
                "total_odds": _odds(bp) if bp is not None else None,
            }

        a, h = side_odds(away_tr), side_odds(home_tr)
        out.append({
            "id": code,                 # 運彩賽事編號（對外的 id）
            "gameid": gid,              # playsport 內部 gameid（含日期）
            "league": ALLIANCES.get(aid, str(aid)),
            "alliance_id": aid,
            "date": _gameid_to_date(gid),
            "time": gtime,
            "away": team(away_tr),
            "home": team(home_tr),
            "odds": {
                "normal": {"a": a["ml_odds"], "h": h["ml_odds"]},
                "handi": {"line": a["handi_line"], "a": a["handi_odds"], "h": h["handi_odds"]},
                "total": {"line": a["total_line"], "o": a["total_odds"], "u": h["total_odds"]},
            },
        })
    return out


def fetch_many(alliance_ids):
    """抓多個聯盟，回傳合併後的 list。單一聯盟失敗不影響其他。"""
    result = []
    for aid in alliance_ids:
        try:
            result.extend(fetch_alliance(aid))
        except Exception as e:  # noqa: BLE001 - 單來源容錯
            print(f"[warn] alliance {aid} 抓取失敗: {e}")
    return result


if __name__ == "__main__":
    import json
    import sys
    aids = [int(x) for x in sys.argv[1:]] or [1]
    data = fetch_many(aids)
    print(json.dumps(data, ensure_ascii=False, indent=2))
