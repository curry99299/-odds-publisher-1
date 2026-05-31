"""隊名/聯盟繁體中文翻譯。

以 norm_team() 正規化後的 key 查表，查不到則回原文（多為較冷門的低級別聯賽）。
涵蓋 MLB 30 隊、NBA 30 隊、主要國家隊、五大聯賽與南美大球會、常見聯盟名稱。
其餘隊名/聯盟由 translate.py 自動翻譯後存入 data/team_translations.json（持久快取），
zh_team/zh_league 會合併靜態表與此快取，查不到才回原文。
"""
import os
import re
import json
from .normalize import norm_team

# 英文標準名 → 繁中
_TEAMS_EN = {
    # ===== MLB =====
    "Arizona Diamondbacks": "亞利桑那響尾蛇", "Atlanta Braves": "亞特蘭大勇士",
    "Baltimore Orioles": "巴爾的摩金鶯", "Boston Red Sox": "波士頓紅襪",
    "Chicago Cubs": "芝加哥小熊", "Chicago White Sox": "芝加哥白襪",
    "Cincinnati Reds": "辛辛那提紅人", "Cleveland Guardians": "克里夫蘭守護者",
    "Colorado Rockies": "科羅拉多落磯", "Detroit Tigers": "底特律老虎",
    "Houston Astros": "休士頓太空人", "Kansas City Royals": "堪薩斯市皇家",
    "Los Angeles Angels": "洛杉磯天使", "Los Angeles Dodgers": "洛杉磯道奇",
    "Miami Marlins": "邁阿密馬林魚", "Milwaukee Brewers": "密爾瓦基釀酒人",
    "Minnesota Twins": "明尼蘇達雙城", "New York Mets": "紐約大都會",
    "New York Yankees": "紐約洋基", "Athletics": "運動家",
    "Oakland Athletics": "奧克蘭運動家", "Philadelphia Phillies": "費城費城人",
    "Pittsburgh Pirates": "匹茲堡海盜", "San Diego Padres": "聖地牙哥教士",
    "San Francisco Giants": "舊金山巨人", "Seattle Mariners": "西雅圖水手",
    "St. Louis Cardinals": "聖路易紅雀", "Tampa Bay Rays": "坦帕灣光芒",
    "Texas Rangers": "德州遊騎兵", "Toronto Blue Jays": "多倫多藍鳥",
    "Washington Nationals": "華盛頓國民",
    # ===== NBA =====
    "Atlanta Hawks": "亞特蘭大老鷹", "Boston Celtics": "波士頓塞爾提克",
    "Brooklyn Nets": "布魯克林籃網", "Charlotte Hornets": "夏洛特黃蜂",
    "Chicago Bulls": "芝加哥公牛", "Cleveland Cavaliers": "克里夫蘭騎士",
    "Dallas Mavericks": "達拉斯獨行俠", "Denver Nuggets": "丹佛金塊",
    "Detroit Pistons": "底特律活塞", "Golden State Warriors": "金州勇士",
    "Houston Rockets": "休士頓火箭", "Indiana Pacers": "印第安納溜馬",
    "LA Clippers": "洛杉磯快艇", "Los Angeles Clippers": "洛杉磯快艇",
    "Los Angeles Lakers": "洛杉磯湖人", "Memphis Grizzlies": "曼菲斯灰熊",
    "Miami Heat": "邁阿密熱火", "Milwaukee Bucks": "密爾瓦基公鹿",
    "Minnesota Timberwolves": "明尼蘇達灰狼", "New Orleans Pelicans": "紐奧良鵜鶘",
    "New York Knicks": "紐約尼克", "Oklahoma City Thunder": "奧克拉荷馬雷霆",
    "Orlando Magic": "奧蘭多魔術", "Philadelphia 76ers": "費城76人",
    "Phoenix Suns": "鳳凰城太陽", "Portland Trail Blazers": "波特蘭拓荒者",
    "Sacramento Kings": "沙加緬度國王", "San Antonio Spurs": "聖安東尼奧馬刺",
    "Toronto Raptors": "多倫多暴龍", "Utah Jazz": "猶他爵士",
    "Washington Wizards": "華盛頓巫師",
    # ===== 國家隊 =====
    "Japan": "日本", "South Korea": "南韓", "Korea Republic": "南韓",
    "Mexico": "墨西哥", "Australia": "澳洲", "Saudi Arabia": "沙烏地阿拉伯",
    "Iceland": "冰島", "Serbia": "塞爾維亞", "Cabo Verde": "維德角",
    "Cape Verde": "維德角", "Ecuador": "厄瓜多", "Trinidad and Tobago": "千里達及托巴哥",
    "Brazil": "巴西", "Argentina": "阿根廷", "France": "法國", "Spain": "西班牙",
    "England": "英格蘭", "Germany": "德國", "Portugal": "葡萄牙", "Italy": "義大利",
    "Netherlands": "荷蘭", "Belgium": "比利時", "Croatia": "克羅埃西亞",
    "Uruguay": "烏拉圭", "Colombia": "哥倫比亞", "USA": "美國", "United States": "美國",
    "Canada": "加拿大", "Iran": "伊朗", "Qatar": "卡達", "Morocco": "摩洛哥",
    "Senegal": "塞內加爾", "Nigeria": "奈及利亞", "Egypt": "埃及", "Ghana": "迦納",
    "Switzerland": "瑞士", "Denmark": "丹麥", "Sweden": "瑞典", "Norway": "挪威",
    "Poland": "波蘭", "Austria": "奧地利", "Turkey": "土耳其", "Ukraine": "烏克蘭",
    "Chile": "智利", "Peru": "秘魯", "Paraguay": "巴拉圭", "Bolivia": "玻利維亞",
    "Venezuela": "委內瑞拉", "Costa Rica": "哥斯大黎加", "Honduras": "宏都拉斯",
    # ===== 英超 =====
    "Arsenal": "兵工廠", "Manchester City": "曼城", "Manchester United": "曼聯",
    "Liverpool": "利物浦", "Chelsea": "切爾西", "Tottenham": "托特納姆熱刺",
    "Tottenham Hotspur": "托特納姆熱刺", "Newcastle United": "紐卡索",
    "Newcastle": "紐卡索", "Aston Villa": "阿斯頓維拉", "West Ham": "西漢姆",
    "West Ham United": "西漢姆聯", "Brighton": "布萊頓", "Everton": "埃弗頓",
    "Wolverhampton": "狼隊", "Nottingham Forest": "諾丁漢森林", "Fulham": "富勒姆",
    "Brentford": "布倫特福德", "Crystal Palace": "水晶宮", "Bournemouth": "伯恩茅斯",
    # ===== 西甲 =====
    "Real Madrid": "皇家馬德里", "Barcelona": "巴塞隆納", "Atletico Madrid": "馬德里競技",
    "Atletico": "馬德里競技", "Sevilla": "塞維亞", "Valencia": "瓦倫西亞",
    "Real Sociedad": "皇家社會", "Villarreal": "比利亞雷亞爾", "Real Betis": "皇家貝提斯",
    "Athletic Bilbao": "畢爾包競技", "Girona": "赫羅納",
    # ===== 義甲 =====
    "Inter": "國際米蘭", "Inter Milan": "國際米蘭", "AC Milan": "AC米蘭",
    "Juventus": "尤文圖斯", "Napoli": "拿坡里", "Roma": "羅馬", "AS Roma": "羅馬",
    "Lazio": "拉齊歐", "Atalanta": "亞特蘭大", "Fiorentina": "佛羅倫斯",
    # ===== 德甲 =====
    "Bayern": "拜仁慕尼黑", "Bayern Munich": "拜仁慕尼黑", "Borussia Dortmund": "多特蒙德",
    "RB Leipzig": "萊比錫紅牛", "Bayer Leverkusen": "勒沃庫森", "Leverkusen": "勒沃庫森",
    "Eintracht Frankfurt": "法蘭克福", "VfB Stuttgart": "斯圖加特",
    # ===== 法甲 =====
    "Paris Saint Germain": "巴黎聖日耳曼", "Paris Saint-Germain": "巴黎聖日耳曼",
    "Marseille": "馬賽", "Monaco": "摩納哥", "Lyon": "里昂", "Lille": "里爾",
    # ===== 巴甲 / 南美 =====
    "Flamengo": "法蘭明哥", "Corinthians": "科林蒂安", "Palmeiras": "帕梅拉斯",
    "Santos": "桑托斯", "Gremio": "格雷米奧", "Botafogo": "博塔弗戈", "Bahia": "巴伊亞",
    "Sao Paulo": "聖保羅", "Fluminense": "弗魯米嫩塞", "Vasco da Gama": "瓦斯科達伽馬",
    "Internacional": "國際", "Cruzeiro": "克魯塞羅", "Atletico Mineiro": "米內羅競技",
    "Boca Juniors": "博卡青年", "River Plate": "河床",
}

# 英文標準名 → 繁中（聯盟）
_LEAGUES_EN = {
    "FIFA - World Cup": "世界盃", "Friendlies. National Teams": "國際友誼賽",
    "Friendlies National Teams": "國際友誼賽", "Fifa Friendly": "國際友誼賽",
    "England - Premier League": "英超", "Premier League": "英超",
    "Spain - La Liga": "西甲", "La Liga": "西甲", "La Liga 2": "西乙",
    "Italy - Serie A": "義甲", "Serie A": "義甲", "Brazil Serie A": "巴甲",
    "Germany - Bundesliga": "德甲", "Bundesliga": "德甲",
    "France - Ligue 1": "法甲", "Ligue 1": "法甲",
    "UEFA Champions League": "歐冠", "UCL": "歐冠",
    "UEFA Europa League": "歐霸", "China - Super League": "中超",
    "Chinese Super League": "中超", "Japan - J League": "日職聯",
    "MLB": "美國職棒大聯盟", "Baseball": "棒球", "NBA": "美國職籃",
    "Basketball": "籃球", "Soccer": "足球",
    # 亞洲職棒
    "KBO": "韓國職棒", "KBO League": "韓國職棒", "Korea - KBO": "韓國職棒",
    "South Korea - KBO": "韓國職棒",
    "NPB": "日本職棒", "Japan - NPB": "日本職棒", "Japan - Baseball": "日本職棒",
    "CPBL": "中華職棒", "CPB": "中華職棒", "Taiwan - CPBL": "中華職棒",
    "Chinese Professional Baseball League": "中華職棒",
    # Pinnacle 棒球聯盟全名
    "Korea Professional Baseball": "韓國職棒", "Korea Baseball Organization": "韓國職棒",
    "Nippon Professional Baseball": "日本職棒", "Japan Professional Baseball": "日本職棒",
    "NCAA Baseball": "美國大學棒球", "Mexican League": "墨西哥棒球聯盟",
    "Mexican Baseball League": "墨西哥棒球聯盟",
}


def _build(d):
    out = {}
    for en, zh in d.items():
        out[norm_team(en)] = zh
    return out


_TEAMS = _build(_TEAMS_EN)
_LEAGUES = {}
for _en, _zh in _LEAGUES_EN.items():
    _LEAGUES[_en.lower()] = _zh
    _LEAGUES[norm_team(_en)] = _zh


# 繁中 → 英文標準名（反查，給台灣運彩等中文來源跨來源配對用）
_ZH_TO_EN = {}
for _en, _zh in _TEAMS_EN.items():
    _ZH_TO_EN.setdefault(_zh, _en)


# ===== 持久翻譯快取（由 translate.py 寫入）=====
_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "team_translations.json")
_CACHE = {"teams": {}, "leagues": {}}
try:
    with open(_CACHE_FILE, encoding="utf-8") as _f:
        _loaded = json.load(_f)
        _CACHE["teams"].update(_loaded.get("teams", {}))
        _CACHE["leagues"].update(_loaded.get("leagues", {}))
except Exception:
    pass

_LATIN = re.compile(r"[A-Za-z]")


def _has_latin(s):
    return bool(s and _LATIN.search(s))


def zh_team(name: str) -> str:
    if not name:
        return name
    nk = norm_team(name)
    return _TEAMS.get(nk) or _CACHE["teams"].get(nk) or name


def zh_league(name: str) -> str:
    if not name:
        return name
    return (_LEAGUES.get(name.lower()) or _LEAGUES.get(norm_team(name))
            or _CACHE["leagues"].get(name.strip()) or name)


def needs_team(name: str) -> bool:
    """此隊名是否需要翻譯（含拉丁字母、且靜態表與快取都沒有）。"""
    if not _has_latin(name):
        return False
    nk = norm_team(name)
    return nk not in _TEAMS and nk not in _CACHE["teams"]


def needs_league(name: str) -> bool:
    if not _has_latin(name):
        return False
    return (name.lower() not in _LEAGUES and norm_team(name) not in _LEAGUES
            and name.strip() not in _CACHE["leagues"])


def add_teams(mapping: dict):
    """mapping: 原文隊名 → 繁中；以 norm_team 為 key 存入快取並寫檔。"""
    for orig, zh in mapping.items():
        if orig and zh:
            _CACHE["teams"][norm_team(orig)] = zh
    _flush_cache()


def add_leagues(mapping: dict):
    for orig, zh in mapping.items():
        if orig and zh:
            _CACHE["leagues"][orig.strip()] = zh
    _flush_cache()


def _flush_cache():
    try:
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f, ensure_ascii=False, indent=0)
        os.replace(tmp, _CACHE_FILE)
    except Exception:
        pass


def en_from_zh(name: str) -> str:
    """繁中隊名 → 英文標準名；查不到回原字串。"""
    if not name:
        return name
    return _ZH_TO_EN.get(name.strip(), name)
