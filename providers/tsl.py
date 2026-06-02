"""台灣運彩 — 從 playsport「預測賽事」頁自抓賠率（無需密鑰、無每日額度限制）。

來源： GET https://www.playsport.cc/predict/games?allianceid={aid}
（伺服器渲染 HTML，無 geo 鎖。取代舊版透過 sportsbot/JBot 付費 API 的做法，
  舊版見 providers/tsl_sportsbot.py.bak）

每場比賽 = 兩個 <tr gameid>（客隊列、主隊列）。盤口欄位：
  td-bank-bet01 (ap)  → 讓分： strong=讓分線  span=賠率
  td-bank-bet03 (pkp) → 不讓/獨贏(moneyline)： span=賠率
  td-bank-bet02 (bp)  → 大小(total)： strong=總分線  span=賠率（客列=大、主列=小）

回傳統一的 MatchOdds，含 moneyline / spreads / totals，與其他 provider 一致。
隊名由繁中簡稱轉英文標準名（以運動別消歧義）以利跨來源配對。
"""
import os
import re
import time
import json
import datetime

import requests
from lxml import html as LH

from core.models import MatchOdds, _line_key
from core import i18n

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
BASE = "https://www.playsport.cc/predict/games?allianceid={aid}"

# allianceid → (sport, 聯盟顯示名)。預設只抓會與其他來源對得上的聯盟。
ALLIANCE = {
    1:  ("baseball", "MLB"),
    6:  ("baseball", "中華職棒"),
    9:  ("baseball", "日本職棒"),
    16: ("baseball", "韓國職棒"),
    3:  ("basketball", "NBA"),
    18: ("basketball", "WNBA"),
    4:  ("soccer", "足球"),
}
DEFAULT_ALLIANCES = [1, 6, 3, 4, 9, 16]

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CACHE = os.path.join(DATA_DIR, "tsl_cache.json")
CACHE_TTL = int(os.environ.get("TSL_CACHE_TTL", "300"))   # 5 分鐘快取（自抓無額度限制，可較短）
REQ_GAP = float(os.environ.get("TSL_REQ_GAP", "1.2"))     # 對 playsport 客氣


# ---- 繁中簡稱 → 英文標準名（依運動別消歧義） ----

def _build_short_map():
    """從 i18n 原始碼解析各英文隊名的運動別，建「簡稱(後綴)→{sport: en}」反查表。
    i18n.py 以註解分區（# ===== MLB ===== 等），用區段標題判運動別。"""
    src_path = os.path.join(os.path.dirname(__file__), "..", "core", "i18n.py")
    section_sport = {
        "MLB": "baseball", "NBA": "basketball", "WNBA": "basketball",
        "國家隊": "soccer", "英超": "soccer", "西甲": "soccer", "義甲": "soccer",
        "德甲": "soccer", "法甲": "soccer", "巴甲": "soccer", "南美": "soccer",
    }
    try:
        with open(src_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []

    cur = None
    en_sport = {}
    for ln in lines:
        m = re.search(r"#\s*=+\s*([^\s=]+)", ln)
        if m:
            key = m.group(1)
            cur = next((sp for tag, sp in section_sport.items() if tag in key), None)
            continue
        for en, _zh in re.findall(r'"([^"]+)":\s*"([^"]+)"', ln):
            if cur and en not in en_sport:
                en_sport[en] = cur

    teams = getattr(i18n, "_TEAMS_EN", {})
    short = {}   # suffix -> {sport: en}
    exact = {}   # full zh -> en（完全相符優先）
    for en, zh in teams.items():
        if not isinstance(zh, str):
            continue
        exact.setdefault(zh, en)
        sp = en_sport.get(en, "?")
        for n in (2, 3, 4):
            if len(zh) >= n:
                short.setdefault(zh[-n:], {}).setdefault(sp, en)
    return short, exact


_SHORT, _EXACT = _build_short_map()


def _zh_to_en(name, sport):
    """繁中簡稱 → 英文標準名。轉不出來時原樣回傳（仍可顯示，只是難跨源配對）。"""
    if not name:
        return name
    if name in _EXACT:
        return _EXACT[name]
    bucket = _SHORT.get(name)
    if bucket:
        if sport in bucket:
            return bucket[sport]
        if len(bucket) == 1:
            return next(iter(bucket.values()))
    return name


# ---- 解析 ----

def _f(v):
    try:
        return float(str(v).lstrip(", ").strip())
    except (TypeError, ValueError):
        return None


def _odds(td):
    if td is None:
        return None
    spans = td.xpath('.//span[@class="data-wrap"]/span')
    return _f(spans[-1].text_content()) if spans else None


def _line(td):
    if td is None:
        return None
    s = td.xpath('.//strong[not(contains(@class,"team-side"))]')
    return _f(s[0].text_content()) if s else None


def _cell(tr, cls):
    tds = tr.xpath(f'.//td[contains(@class,"{cls}")]')
    return tds[0] if tds else None


def _start_utc(gameid, gtime):
    """gameid 前 8 碼=台灣日期，gtime 形如 'AM 06:40' → UTC ISO。"""
    m = re.match(r"(\d{4})(\d{2})(\d{2})", gameid or "")
    tm = re.search(r"(AM|PM)?\s*(\d{1,2}):(\d{2})", gtime or "")
    if not m or not tm:
        return None
    hh, mm = int(tm.group(2)), int(tm.group(3))
    ap = tm.group(1)
    if ap == "PM" and hh != 12:
        hh += 12
    elif ap == "AM" and hh == 12:
        hh = 0
    try:
        dt = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hh, mm,
                               tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _parse_alliance(aid, text):
    sport, league = ALLIANCE.get(aid, ("baseball", str(aid)))
    doc = LH.fromstring(text)
    games = {}
    for tr in doc.xpath("//tr[@gameid]"):
        games.setdefault(tr.get("gameid"), []).append(tr)

    rows = []
    for gid, trs in games.items():
        if len(trs) < 2:
            continue
        away_tr, home_tr = trs[0], trs[1]

        def team(tr):
            h3 = tr.xpath('.//td[contains(@class,"td-teaminfo")]//h3')
            return h3[0].text_content().strip() if h3 else ""

        away_zh, home_zh = team(away_tr), team(home_tr)
        if not away_zh or not home_zh:
            continue

        # moneyline（不讓 / 獨贏）
        ao = _odds(_cell(away_tr, "td-bank-bet03"))
        ho = _odds(_cell(home_tr, "td-bank-bet03"))
        if ao is None and ho is None:
            continue

        # 讓分（spreads，以主隊讓分線為 key）
        spreads = {}
        home_line = _line(_cell(home_tr, "td-bank-bet01"))
        sp_a = _odds(_cell(away_tr, "td-bank-bet01"))
        sp_h = _odds(_cell(home_tr, "td-bank-bet01"))
        if home_line is not None and (sp_a or sp_h):
            spreads[_line_key(home_line)] = {"home": sp_h, "away": sp_a}

        # 大小（totals）：客列=大(over)、主列=小(under)
        totals = {}
        tline = _line(_cell(away_tr, "td-bank-bet02")) or _line(_cell(home_tr, "td-bank-bet02"))
        ov = _odds(_cell(away_tr, "td-bank-bet02"))
        un = _odds(_cell(home_tr, "td-bank-bet02"))
        if tline is not None and (ov or un):
            totals[_line_key(tline)] = {"over": ov, "under": un}

        info = _cell(away_tr, "td-gameinfo")
        gtime = ""
        if info is not None:
            h4 = info.xpath(".//h4")
            gtime = h4[0].text_content().strip() if h4 else ""

        rows.append(MatchOdds(
            source="tsl", sport=sport,
            home=_zh_to_en(home_zh, sport), away=_zh_to_en(away_zh, sport),
            start=_start_utc(gid, gtime), league=league,
            home_odds=ho, away_odds=ao, draw_odds=None,
            url="https://www.playsport.cc/predict/games",
            spreads=spreads, totals=totals,
        ))
    return rows


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


def _alliances():
    env = os.environ.get("TSL_ALLIANCES")
    if env:
        return [int(x) for x in env.split(",") if x.strip().isdigit()]
    return DEFAULT_ALLIANCES


def fetch():
    cache = _load(CACHE, {})
    now = time.time()
    if cache.get("ts") and now - cache["ts"] < CACHE_TTL and cache.get("rows"):
        rows = [MatchOdds(**r) for r in cache["rows"]]
        print(f"[tsl] 用快取 {len(rows)} 場（{int((now - cache['ts']) // 60)} 分鐘前）")
        return rows

    rows = []
    for i, aid in enumerate(_alliances()):
        if i:
            time.sleep(REQ_GAP)
        try:
            resp = requests.get(BASE.format(aid=aid), headers=UA, timeout=25)
            resp.raise_for_status()
            got = _parse_alliance(aid, resp.text)
            rows += got
            print(f"[tsl] alliance {aid} ({ALLIANCE.get(aid, ('?', '?'))[1]}): {len(got)} 場")
        except Exception as e:  # noqa: BLE001 - 單聯盟失敗不影響其他
            print(f"[tsl] alliance {aid} 抓取失敗: {e}")

    _save(CACHE, {"ts": now, "rows": [r.to_dict() for r in rows]})
    print(f"[tsl] 共取得 {len(rows)} 場（playsport 自抓，無額度限制）")
    return rows


if __name__ == "__main__":
    for r in fetch()[:8]:
        d = r.to_dict()
        print(f"{d['league']:6} {d['away']} @ {d['home']}  ML={d['away_odds']}/{d['home_odds']}  "
              f"讓={d['spreads']} 大小={d['totals']}")
