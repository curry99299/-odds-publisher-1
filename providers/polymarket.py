"""Polymarket — 透過 gamma API 抓運動對戰市場，把隱含機率換算成歐式賠率。

兩種盤型：
  A. 足球：每隊一個「Will X win?」Yes/No 市場 + 一個 Draw 市場（Yes 價=該結果機率）
  B. 籃球/網球/電競：單一市場，outcomes=[隊A,隊B]，outcomePrices 直接是機率
"""
import json
import re
import datetime
import requests
from core.models import MatchOdds, prob_to_decimal, _line_key
from core.normalize import teams_similar, norm_team

# 各運動「比賽進行中」時長（小時），用來由開賽時間推斷滾球
# hockey：3 節×20 分 + 2 次節間休息 + 暖身/停表，正規約 2.5h，留延長賽空間抓 3.4h
_LIVE_WINDOW = {"soccer": 2.8, "basketball": 3.2, "baseball": 3.9, "hockey": 3.4}


def _is_live(sport, start_iso):
    """Polymarket 比賽進行中盤口仍開、賠率即時跳動；由精確開賽時間推斷是否滾球。"""
    if not start_iso:
        return False
    try:
        st = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    win = _LIVE_WINDOW.get(sport, 3.0)
    return st <= now <= st + datetime.timedelta(hours=win)

BASE = "https://gamma-api.polymarket.com"
HEADERS = {"user-agent": "Mozilla/5.0", "accept": "application/json"}

# Polymarket tag → sport 標籤（只保留能與其他來源對齊的運動）
# NHL 場次 tag 形如 ['Sports','NHL','Games','Hockey']，以 Hockey 對到 hockey
TAG_SPORT = {"soccer": "soccer", "basketball": "basketball", "baseball": "baseball", "hockey": "hockey"}


def _jload(v, default):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return default
    return v if v is not None else default


def _clean_prob(prs, idx=0):
    """取 outcomePrices[idx] 機率，已結算(≈0/≈1)的回傳 None。"""
    try:
        p = float(prs[idx])
    except (TypeError, ValueError, IndexError):
        return None
    if p <= 0.001 or p >= 0.999:
        return None
    return prob_to_decimal(p)


def _date_from_slug(slug):
    """slug 結尾常帶比賽日期，如 ...-2026-05-30。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})$", slug or "")
    return f"{m.group(1)}T12:00:00Z" if m else None


def _split_title(title):
    t = re.sub(r"\s*-\s*(More Markets|Exact Score|.*Markets)$", "", title, flags=re.I)
    m = re.split(r"\s+vs\.?\s+", t, maxsplit=1, flags=re.I)
    if len(m) != 2:
        return None, None
    return m[0].strip(), m[1].strip()


def _sport_of(tags):
    labels = [str(x.get("label", "")).lower() for x in tags]
    for lab in labels:
        if lab in TAG_SPORT:
            return TAG_SPORT[lab]
    return None


def _fetch_events():
    # 依 id 去重：order=volume24hr 跨分頁不穩定，同一場可能重複出現在多頁
    events, seen, offset = [], set(), 0
    for _ in range(6):  # 最多抓 600 筆
        url = (f"{BASE}/events?closed=false&limit=100&offset={offset}"
               f"&tag_id=1&related_tags=true&order=volume24hr&ascending=false")
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for e in batch:
            eid = e.get("id") or e.get("slug")
            if eid in seen:
                continue
            seen.add(eid)
            events.append(e)
        offset += 100
    return events


def fetch():
    try:
        events = _fetch_events()
    except Exception as e:
        print(f"[polymarket] 抓取失敗: {e}")
        return []

    rows = []
    for e in events:
        tags = e.get("tags", []) or []
        labels = [str(x.get("label", "")).lower() for x in tags]
        if "games" not in labels:        # 只要單場對戰，跳過冠軍盤等
            continue
        sport = _sport_of(tags)
        if not sport:
            continue
        home, away = _split_title(e.get("title", ""))
        if not home or not away:
            continue

        ml = [m for m in e.get("markets", []) if m.get("sportsMarketType") == "moneyline"]
        if not ml:
            continue

        ho = do = ao = None
        if len(ml) == 1:
            # 盤型 B：單一市場雙結果
            m = ml[0]
            outs = _jload(m.get("outcomes"), [])
            prs = _jload(m.get("outcomePrices"), [])
            if len(outs) == 2 and len(prs) == 2:
                for i, name in enumerate(outs):
                    if teams_similar(norm_team(name), norm_team(home)):
                        ho = _clean_prob(prs, i)
                    elif teams_similar(norm_team(name), norm_team(away)):
                        ao = _clean_prob(prs, i)
        else:
            # 盤型 A：每隊一個 Yes/No 市場
            for m in ml:
                git = (m.get("groupItemTitle") or "")
                prs = _jload(m.get("outcomePrices"), [])
                if not prs:
                    continue
                yes = _clean_prob(prs, 0)  # outcomes[0]=="Yes"
                if git.lower().startswith("draw"):
                    do = yes
                elif teams_similar(norm_team(git), norm_team(home)):
                    ho = yes
                elif teams_similar(norm_team(git), norm_team(away)):
                    ao = yes

        if ho is None and ao is None:
            continue

        # 讓分(spreads) / 大小分(totals)
        sp_dict, to_dict = {}, {}
        for m in e.get("markets", []):
            smt = m.get("sportsMarketType")
            prs = _jload(m.get("outcomePrices"), [])
            if smt not in ("spreads", "totals") or len(prs) < 2:
                continue
            try:
                line = float(m.get("line"))
            except (TypeError, ValueError):
                continue
            yes, no = _clean_prob(prs, 0), _clean_prob(prs, 1)
            if smt == "spreads":
                mt = re.search(r"Spread:\s*(.+?)\s*\(", m.get("question") or "")
                team = mt.group(1) if mt else ""
                if teams_similar(norm_team(team), norm_team(home)):
                    d = sp_dict.setdefault(_line_key(line), {})
                    if yes:
                        d["home"] = yes
                    if no:
                        d["away"] = no
                elif teams_similar(norm_team(team), norm_team(away)):
                    d = sp_dict.setdefault(_line_key(-line), {})  # 主隊讓分線 = 客隊線取負
                    if yes:
                        d["away"] = yes
                    if no:
                        d["home"] = no
            else:  # totals
                d = to_dict.setdefault(_line_key(line), {})
                if yes:
                    d["over"] = yes
                if no:
                    d["under"] = no

        # 優先用精確開賽時間（startTime / gameStartTime），slug 日期僅最後備援
        precise = bool(e.get("startTime") or e.get("gameStartTime"))
        start = (e.get("startTime") or e.get("gameStartTime")
                 or _date_from_slug(e.get("slug", "")) or e.get("startDate"))
        rows.append((precise, MatchOdds(
            source="polymarket",
            sport=sport,
            home=home,
            away=away,
            start=start,
            live=_is_live(sport, start),
            league=next((str(x.get("label")) for x in tags
                         if str(x.get("label", "")).lower() not in
                         ("games", "sports", "soccer", "basketball", "baseball", "hockey")), ""),
            home_odds=ho,
            draw_odds=do,
            away_odds=ao,
            spreads=sp_dict,
            totals=to_dict,
            url=f"https://polymarket.com/event/{e.get('slug', '')}",
        )))

    # 同一場可能有多個 Polymarket 事件（含無精確時間的重複盤）→ 依賽事去重，保留有精確開賽時間者
    from core.normalize import match_key
    best = {}
    for precise, r in rows:
        k = (r.sport, match_key(r.home, r.away, r.sport))
        if k not in best or (precise and not best[k][0]):
            best[k] = (precise, r)
    out = [r for _, r in best.values()]
    print(f"[polymarket] 取得 {len(out)} 場")
    return out


if __name__ == "__main__":
    for r in fetch()[:8]:
        print(r.to_dict())
