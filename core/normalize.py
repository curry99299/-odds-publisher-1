"""隊名/賽事正規化 — 用於跨來源賽事配對。

各家對同一支球隊的寫法不同（Paris Saint-Germain FC / PSG / 巴黎聖日耳曼），
這裡把名稱壓成一個可比對的 key，並提供常見別名表。
"""
import re
import unicodedata

# 常見後綴/雜訊詞，配對時移除
_NOISE = {
    "fc", "cf", "afc", "sc", "ac", "ss", "as", "us", "rc", "cd", "ud", "club",
    "the", "city", "town", "united", "utd", "fk", "if", "bk", "sk",
    "calcio", "futbol", "football", "bc", "basketball", "hk",
}

# 別名 → 標準短名（小寫）。可持續擴充。
ALIASES = {
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    # 注意：「Spurs」在 NBA 指聖安東尼奧馬刺、在足球指熱刺；本系統以 NBA 為準，
    # 熱刺請用完整名 tottenham / tottenham hotspur。
    "spurs": "san antonio spurs",
    "tottenham hotspur": "tottenham",
    "inter milan": "inter",
    "internazionale": "inter",
    "bayern munich": "bayern",
    "bayern munchen": "bayern",
    "atletico madrid": "atletico",
    "atletico de madrid": "atletico",
    "wolverhampton wanderers": "wolverhampton",
    "wolves": "wolverhampton",
    "la lakers": "los angeles lakers",
    "ny knicks": "new york knicks",
    "gs warriors": "golden state warriors",
    # NBA 簡稱（Polymarket 多用隊名簡稱）→ 全名，修正繁中顯示與配對
    "hawks": "atlanta hawks", "celtics": "boston celtics", "nets": "brooklyn nets",
    "hornets": "charlotte hornets", "bulls": "chicago bulls", "cavaliers": "cleveland cavaliers",
    "cavs": "cleveland cavaliers", "mavericks": "dallas mavericks", "mavs": "dallas mavericks",
    "nuggets": "denver nuggets", "pistons": "detroit pistons", "warriors": "golden state warriors",
    "rockets": "houston rockets", "pacers": "indiana pacers", "clippers": "los angeles clippers",
    "lakers": "los angeles lakers", "grizzlies": "memphis grizzlies", "heat": "miami heat",
    "bucks": "milwaukee bucks", "timberwolves": "minnesota timberwolves",
    "pelicans": "new orleans pelicans", "knicks": "new york knicks", "thunder": "oklahoma city thunder",
    "magic": "orlando magic", "76ers": "philadelphia 76ers", "sixers": "philadelphia 76ers",
    "suns": "phoenix suns", "trail blazers": "portland trail blazers", "blazers": "portland trail blazers",
    "kings": "sacramento kings", "raptors": "toronto raptors", "jazz": "utah jazz",
    "wizards": "washington wizards",
    # NHL 簡稱（Polymarket 多用隊名簡稱）→ ESPN 全名，讓賠率(Polymarket)與比分(ESPN)配對。
    # 註：「kings」與 NBA 沙加緬度國王同名，已改由 ALIASES_BY_SPORT['hockey'] 處理（洛杉磯國王），ESPN 比分可正常掛。
    "ducks": "anaheim ducks", "bruins": "boston bruins", "sabres": "buffalo sabres",
    "flames": "calgary flames", "hurricanes": "carolina hurricanes", "blackhawks": "chicago blackhawks",
    "avalanche": "colorado avalanche", "blue jackets": "columbus blue jackets", "stars": "dallas stars",
    "red wings": "detroit red wings", "oilers": "edmonton oilers", "panthers": "florida panthers",
    "wild": "minnesota wild", "canadiens": "montreal canadiens", "predators": "nashville predators",
    "devils": "new jersey devils", "islanders": "new york islanders", "rangers": "new york rangers",
    "senators": "ottawa senators", "flyers": "philadelphia flyers", "penguins": "pittsburgh penguins",
    "sharks": "san jose sharks", "kraken": "seattle kraken", "blues": "st louis blues",
    "lightning": "tampa bay lightning", "maple leafs": "toronto maple leafs", "mammoth": "utah mammoth",
    "canucks": "vancouver canucks", "golden knights": "vegas golden knights",
    "capitals": "washington capitals", "jets": "winnipeg jets",
    # 中文別名（台灣運彩/熊貓體育常見）
    "巴黎聖日耳曼": "paris saint germain",
    "兵工廠": "arsenal",
    "阿森納": "arsenal",
    "曼城": "manchester city",
    "曼聯": "manchester united",
    "拜仁": "bayern",
    "皇馬": "real madrid",
    "皇家馬德里": "real madrid",
    "巴薩": "barcelona",
    "巴塞隆納": "barcelona",
    "湖人": "los angeles lakers",
    "塞爾提克": "boston celtics",
    "勇士": "golden state warriors",
}

# 運動別專屬別名：同一個短名在不同運動指不同隊（如「kings」NBA=沙加緬度國王、NHL=洛杉磯國王）。
# 當 norm_team/match_key 帶 sport 時，這裡優先於全域 ALIASES；不帶 sport 則完全照舊（零影響其他運動）。
ALIASES_BY_SPORT = {
    "hockey": {
        "kings": "los angeles kings",  # 全域 ALIASES 的 kings 已被 NBA 佔用，冰球用此覆蓋
    },
}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_team(name, sport=None):
    """把任意隊名壓成正規化 token 字串（排序過的核心詞）。
    帶 sport 時，運動別專屬別名（ALIASES_BY_SPORT）優先於全域 ALIASES；不帶 sport 行為照舊。"""
    if not name:
        return ""
    sp_aliases = ALIASES_BY_SPORT.get(sport or "", {})
    s = name.strip().lower()
    # 先查整串別名（運動別專屬優先）
    if s in sp_aliases:
        s = sp_aliases[s]
    elif s in ALIASES:
        s = ALIASES[s]
    s = strip_accents(s)
    # 移除非字母數字（保留中日韓字元）
    s = re.sub(r"[^\w一-鿿]+", " ", s, flags=re.UNICODE)
    tokens = [t for t in s.split() if t and t not in _NOISE]
    # 再對單一 token 查別名（運動別專屬優先）
    joined = " ".join(tokens)
    if joined in sp_aliases:
        joined = sp_aliases[joined]
        tokens = joined.split()
    elif joined in ALIASES:
        joined = ALIASES[joined]
        tokens = joined.split()
    return " ".join(sorted(tokens))


def match_key(home, away, sport=None):
    """產生與主客順序無關的賽事配對 key（帶 sport 時套用運動別專屬別名）。"""
    a, b = norm_team(home, sport), norm_team(away, sport)
    return "|".join(sorted([a, b]))


def teams_similar(a: str, b: str) -> bool:
    """兩個正規化隊名是否足夠相似（容許其中一個是另一個的子集）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return False
    inter = sa & sb
    # 任一方完全被涵蓋，或交集佔多數
    return bool(inter) and (sa <= sb or sb <= sa or len(inter) / min(len(sa), len(sb)) >= 0.5)
