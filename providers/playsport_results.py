"""playsport 終場比分（結算用）。

odds-compare 的即時源（pinnacle/1xbet）一旦比賽結束就把該場下架，導致 NPB/KBO/CPBL
打完後拿不到終場比分、無法結算。本 provider 從 playsport「賽事結果」頁補回終場比分：

  GET https://www.playsport.cc/gamesData/result?allianceid={aid}&gametime={YYYYMMDD}

allianceid：1=MLB、2=NPB、6=CPBL、9=KBO（實測確認，注意 9 是韓職不是日職）。
每場兩列：上排=secondteam=客隊、下排=winnerteam=主隊；scores ul 兩個數字依序 [客分, 主分]。

回傳給前端（併進 latest.json 的 results）：每場含 league_zh / home_zh / away_zh /
home / away（英文標準名，供 NPB/KBO/CPBL 用英文 token 比對）/ score("主:客") / live=False。
MLB 隊名短中文與 MS AI 一致，靠中文比對即可，故英文欄沿用中文。
"""
import datetime

import requests
from lxml import html as LH

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
RESULT = "https://www.playsport.cc/gamesData/result?allianceid={aid}&gametime={date}"

# alliance → (sport, 標準 league_zh)
ALLIANCE = {1: ("baseball", "美國職棒大聯盟"), 2: ("baseball", "日本職棒"),
            6: ("baseball", "中華職棒"), 9: ("baseball", "韓國職棒")}

# playsport 短中文隊名 → 英文標準名（按聯盟，因短名跨聯盟同名：樂天=NPB金鷹/KBO Lotte/CPBL桃猿）
PS_EN = {
    2: {  # NPB
        "中日": "Chunichi Dragons", "巨人": "Yomiuri Giants", "廣島": "Hiroshima Carp",
        "樂天": "Rakuten Eagles", "橫濱": "Yokohama BayStars", "歐力士": "Orix Buffaloes",
        "火腿": "Nippon Ham Fighters", "羅德": "Chiba Lotte Marines", "西武": "Seibu Lions",
        "軟銀": "Fukuoka SoftBank Hawks", "阪神": "Hanshin Tigers", "養樂多": "Yakult Swallows",
    },
    9: {  # KBO
        "三星獅": "Samsung Lions", "培證": "Kiwoom Heroes", "巫師": "KT Wiz", "恐龍": "NC Dinos",
        "斗山熊": "Doosan Bears", "樂天": "Lotte Giants", "登陸者": "SSG Landers",
        "華老鷹": "Hanwha Eagles", "起亞虎": "KIA Tigers", "雙子": "LG Twins",
    },
    6: {  # CPBL
        "兄弟": "CTBC Brothers", "台鋼": "TSG Hawks", "味全": "Wei Chuan Dragons",
        "富邦": "Fubon Guardians", "樂天": "Rakuten Monkeys", "統一": "Uni President Lions",
    },
}


def _txt(el):
    return el.text_content().strip() if el is not None else ""


def _parse(aid, text, date_iso):
    sport, league_zh = ALLIANCE[aid]
    en_map = PS_EN.get(aid, {})
    doc = LH.fromstring(text)
    out = []
    for tr in doc.xpath("//tr[@gameid]"):
        info = tr.xpath('.//td[contains(@class,"td-teaminfo")]')
        if not info:
            continue
        info = info[0]
        away_zh = _txt((info.xpath('.//td[contains(@class,"secondteam")]//a') or [None])[0])
        home_zh = _txt((info.xpath('.//td[contains(@class,"winnerteam")]//a') or [None])[0])
        nums = [_txt(li) for li in info.xpath('.//td[contains(@class,"scores")]//li[not(contains(@class,"vsicon"))]')
                if _txt(li).isdigit()]
        if not away_zh or not home_zh or len(nums) < 2:
            continue
        away_score, home_score = int(nums[0]), int(nums[1])   # 依序 [客, 主]
        out.append({
            "sport": sport, "league": league_zh, "league_zh": league_zh,
            "away_zh": away_zh, "home_zh": home_zh,
            "away": en_map.get(away_zh, away_zh), "home": en_map.get(home_zh, home_zh),
            "score": f"{home_score}:{away_score}",   # 主:客（與 enrich 慣例一致）
            "date": date_iso,   # 台灣日期 YYYY-MM-DD（供前端日期比對，避免系列賽前一天結果套到今天）
            "live": False, "final": True,
        })
    return out


def fetch_results():
    """抓今天 + 昨天（台灣日期）的終場比分，回傳合併 list。單一聯盟/日期失敗不影響其他。"""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(tz).date()
    dates = [today.strftime("%Y%m%d"), (today - datetime.timedelta(days=1)).strftime("%Y%m%d")]
    seen, out = set(), []
    for aid in ALLIANCE:
        for date in dates:
            date_iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            try:
                r = requests.get(RESULT.format(aid=aid, date=date), headers=UA, timeout=20)
                r.raise_for_status()
                for g in _parse(aid, r.text, date_iso):
                    key = (g["league_zh"], g["home_zh"], g["away_zh"], g["date"])  # 含日期：系列賽同隊不同天各留一筆
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(g)
            except Exception as e:  # noqa: BLE001 - 單來源容錯
                print(f"[playsport_results] aid={aid} {date} 失敗: {e}")
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_results(), ensure_ascii=False, indent=2))
