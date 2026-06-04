"""跑所有來源 → 跨來源配對 → 寫 data/latest.json。

每個 provider 獨立 try/except，單一來源失敗不影響其他來源。
記錄每個來源的狀態（場數 / ok / error），供前端顯示。
"""
import os
import re
import json
import time
import traceback
from datetime import datetime, timezone, timedelta

from core.matcher import merge, _priority
from core import translate
from core.normalize import match_key, norm_team, teams_similar
from providers import pinnacle, polymarket, onexbet, tsl, panda, espn_scores, playsport_results, playsport_live


_SCORE_WIN = {"baseball": 4.5, "basketball": 3.5, "soccer": 3.0, "hockey": 3.4}


def _event_started_recently(e, now):
    """賽事開賽時間是否「就是現在這場」（避免系列賽明天場次套到今天的比分）。"""
    st = e.get("start")
    if not st:
        return None
    try:
        dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt


def _apply_scores(events, scores):
    """用 ESPN 即時比分補上比分/局數，並以 ESPN 狀態校正 live（權威）。
    僅套用到開賽時間接近現在的場次（系列賽明天的同隊賽事不受影響）。
    ESPN 未涵蓋的賽事（足球、KBO/NPB 等）維持原本的 live/比分。"""
    now = datetime.now(timezone.utc)
    for e in events:
        s = scores.get((e["sport"], match_key(e["home"], e["away"], e["sport"])))
        if not s:
            continue
        dt = _event_started_recently(e, now)
        win = _SCORE_WIN.get(e["sport"], 4)
        state = s["state"]
        if state == "in":
            # 只認「已開賽且在比賽時長窗內」的那一場
            if dt is None or not (now - timedelta(hours=win) <= dt <= now + timedelta(minutes=40)):
                continue
            e["live"] = True
            hs, as_ = s.get("hs"), s.get("as")
            sc = ""
            if hs is not None and as_ is not None:
                same = teams_similar(norm_team(e["home"]), norm_team(s["home"]))
                sc = f"{hs}:{as_}" if same else f"{as_}:{hs}"
            e["score"] = " · ".join(x for x in [sc, s.get("status", "")] if x)
        elif state == "post":   # 已結束：只校正「過去」的場次，未來同隊賽事不動
            if dt is not None and dt <= now:
                e["live"] = False
                e["score"] = ""


def _clear_future_live(events, margin_min=40):
    """未開賽的場不可能在滾球：清掉「開賽時間明顯在未來(>margin)卻被標 live/有比分」的場。
    根因：1xbet LiveFeed 的滾球場（自帶比分字串）會被 merge 併到同隊『系列賽』的未來排程場，
    把進行中那場的比分洩漏到還沒開打的場。ESPN 比分套用有時間窗保護，但 provider 自帶的比分沒有，
    故在此統一以開賽時間把關（與 _apply_scores 的 now+40min 上界一致）。"""
    now = datetime.now(timezone.utc)
    cleared = 0
    for e in events:
        if not (e.get("live") or e.get("score")):
            continue
        st = e.get("start")
        if not st:
            continue
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if dt > now + timedelta(minutes=margin_min):
            e["live"] = False
            e["score"] = ""
            cleared += 1
    if cleared:
        print(f"[fetch_all] 清掉 {cleared} 場未開賽卻被標 live/比分（同隊系列賽比分洩漏）")


def _apply_playsport_live(events, ps_games):
    """以 playsport 即時比分「優先」覆寫 live 比分（中職/日職/韓職等 ESPN 未涵蓋的聯賽）。
    在 ESPN 之後套用 → playsport 贏。靠 zh 子字串或英文 token 雙向比對（feed 有時 home_zh 未翻成中文）。"""
    def _entoks(s):
        return set(t for t in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split() if len(t) >= 3)

    def _side(ev_zh, ev_en, ps_zh, ps_en):
        ez, ee = (ev_zh or "").strip(), (ev_en or "").strip()
        pz, pe = (ps_zh or "").strip(), (ps_en or "").strip()
        if pz and (pz in ez or (ez and ez in pz) or pz in ee or (ee and ee in pz)):
            return True
        if pe and (_entoks(ee) & _entoks(pe) or _entoks(ez) & _entoks(pe)):
            return True
        return False

    now = datetime.now(timezone.utc)

    def _is_future(e):
        """事件開賽是否明顯在未來(>40min)：系列賽同隊明天的場，不可掛今天的 live 比分。"""
        st = e.get("start")
        if not st:
            return False
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return False
        return dt > now + timedelta(minutes=40)

    cnt = 0
    matched = set()
    for e in events:
        if _is_future(e):   # 未來場不掛 live → 該 live 場留作 unmatched，下面補成獨立事件
            continue
        for gi, g in enumerate(ps_games):
            if g["sport"] != e["sport"]:
                continue
            direct = (_side(e.get("home_zh"), e.get("home"), g["home_zh"], g["home_en"])
                      and _side(e.get("away_zh"), e.get("away"), g["away_zh"], g["away_en"]))
            swap = (_side(e.get("home_zh"), e.get("home"), g["away_zh"], g["away_en"])
                    and _side(e.get("away_zh"), e.get("away"), g["home_zh"], g["home_en"]))
            if not (direct or swap):
                continue
            hs, as_ = g["home_score"], g["away_score"]
            sc = f"{hs}:{as_}" if direct else f"{as_}:{hs}"  # 依 event 主隊方向擺
            e["live"] = True
            e["score"] = " · ".join(x for x in [sc, g["status"]] if x)
            # 打擊方：g["bat"] 相對 playsport 主客；swap 時翻面對齊 event 主客
            gbat = g.get("bat")
            if gbat in ("home", "away"):
                e["bat_side"] = gbat if direct else ("away" if gbat == "home" else "home")
            matched.add(gi)
            cnt += 1
            break
    if cnt:
        print(f"[playsport_live] 覆寫 {cnt} 場滾球比分（playsport 優先）")
    # 對不到任何賠率事件的 playsport live 場（賠率源沒這個對戰）→ 自建獨立 live 事件，
    # 否則該場的滾球比分沒地方掛、前端完全看不到（如洛磯 vs 天使，賠率源無此對戰）。
    added = 0
    for gi, g in enumerate(ps_games):
        if gi in matched:
            continue
        events.append(_standalone_live_event(g))
        added += 1
    if added:
        print(f"[playsport_live] 補獨立 live 場 {added} 場（賠率源未涵蓋此對戰）")


def _standalone_live_event(g):
    """把一場 playsport live 場包成 feed event（賠率源沒有此對戰時用）。主客＝playsport 原方向。"""
    hs, as_ = g["home_score"], g["away_score"]
    return {
        "sport": g["sport"], "league": g.get("league_zh", ""), "league_zh": g.get("league_zh", ""),
        "home": g.get("home_en") or g["home_zh"], "away": g.get("away_en") or g["away_zh"],
        "home_zh": g["home_zh"], "away_zh": g["away_zh"],
        "live": True,
        "score": " · ".join(x for x in [f"{hs}:{as_}", g.get("status", "")] if x),
        "bat_side": g["bat"] if g.get("bat") in ("home", "away") else None,
        "source_count": 0, "sources": {}, "start": None,
        "has_draw": False, "best": {}, "arb": None,
    }


_SCORE_RE = re.compile(r"\s*(\d+)\s*[:：]\s*(\d+)")


def _attach_numeric_scores(items):
    """從每筆事件的 score 字串（慣例「主:客 · 進度」）解析出 home_score/away_score 數值欄。
    讓前端直接讀數值、不必各自用 regex 解析字串方向（過去兩頁各一份解析，且比分對調 bug 難查）。
    score 字串仍保留（供「· 進度」滾球文字與顯示）。無比分數字者兩欄為 None。"""
    for e in items:
        m = _SCORE_RE.match(str(e.get("score") or ""))
        e["home_score"] = int(m.group(1)) if m else None
        e["away_score"] = int(m.group(2)) if m else None


def _apply_deltas(events, prev_events):
    """為每個賽事/來源/腳計算與「上一次更新」相比的漲跌 %（存成 *_chg）。"""
    prev = {}
    for e in prev_events:
        prev[(e["sport"], match_key(e["home"], e["away"], e["sport"]))] = e
    for e in events:
        rec = prev.get((e["sport"], match_key(e["home"], e["away"], e["sport"])))
        swapped = bool(rec) and not teams_similar(norm_team(e["home"]), norm_team(rec["home"]))
        for src, v in e["sources"].items():
            pv = rec["sources"].get(src) if rec else None
            for leg in ("home", "draw", "away"):
                v[leg + "_chg"] = None
                cur = v.get(leg)
                if not (cur and pv):
                    continue
                pleg = {"home": "away", "away": "home"}.get(leg, leg) if swapped else leg
                p = pv.get(pleg)
                if p and p > 0:
                    v[leg + "_chg"] = round((cur - p) / p * 100, 1)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LATEST = os.path.join(DATA_DIR, "latest.json")

# 來源顯示名稱（前端用）
SOURCE_LABELS = {
    "pinnacle": "Pinnacle",
    "polymarket": "Polymarket",
    "1xbet": "1xbet",
    "tsl": "台灣運彩",
    "panda": "熊貓體育",
}
PROVIDERS = [
    ("pinnacle", pinnacle.fetch),
    ("polymarket", polymarket.fetch),
    ("1xbet", onexbet.fetch),
    ("tsl", tsl.fetch),
    ("panda", panda.fetch),
]


def run_once():
    os.makedirs(DATA_DIR, exist_ok=True)
    # 先讀上一份快照，供計算賠率漲跌
    prev_events = []
    try:
        with open(LATEST, encoding="utf-8") as f:
            prev_events = json.load(f).get("events", [])
    except Exception:
        pass
    all_rows = []
    status = {}
    for key, fn in PROVIDERS:
        t0 = time.time()
        try:
            rows = fn() or []
            all_rows.extend(rows)
            status[key] = {
                "label": SOURCE_LABELS[key],
                "ok": True,
                "count": len(rows),
                "note": "" if rows else (
                    "需登入（ENABLE_PANDA）" if key == "panda" else "目前無賽事"),
                "ms": int((time.time() - t0) * 1000),
            }
        except Exception as e:
            status[key] = {
                "label": SOURCE_LABELS[key],
                "ok": False,
                "count": 0,
                "note": f"錯誤: {e}",
                "ms": int((time.time() - t0) * 1000),
            }
            print(f"[fetch_all] {key} 失敗:\n{traceback.format_exc()}")

    # 自動補譯未收錄的隊名/聯盟（寫入持久快取），再配對 → 繁中欄位才完整
    try:
        translate.ensure_translations(
            [n for r in all_rows for n in (r.home, r.away)],
            [r.league for r in all_rows],
        )
    except Exception as e:
        print(f"[fetch_all] 自動翻譯略過: {e}")

    events = merge(all_rows)
    _apply_deltas(events, prev_events)
    # ===== 比分／滾球狀態：只用 playsport（棄用 ESPN / 1xbet / polymarket）=====
    # 多來源互相打架會一直冒 bug（英文局數、polymarket 依時間推斷的假 live、過時鏡像…），故：
    #   先清掉 odds 來源自帶的 live/比分 → 滾球與比分一律由 playsport 決定。
    for e in events:
        e["live"] = False
        e["score"] = ""
    try:
        # playsport 即時比分（唯一 live 來源）：名字雙向比對，時間相同但主客相反也認得
        _apply_playsport_live(events, playsport_live.fetch_live())
    except Exception as e:
        print(f"[playsport_live] 略過: {e}")
    _clear_future_live(events)  # 安全網：未開賽不可能 live
    events.sort(key=lambda e: (_priority(e), not e["live"], -e["source_count"], e["start"] or "9999"))
    # playsport 終場比分（棒球：gamesData/result；其餘運動：即時頁的已結束場補洞）
    # 完整跑抓近 8 天，補歷史終場 → 過去預測（2 天前的場）也能用 playsport 結算
    try:
        results = playsport_results.fetch_results(days=8)
        print(f"[playsport_results] 終場比分 {len(results)} 場（近 8 天）")
    except Exception as e:
        results = []
        print(f"[playsport_results] 略過: {e}")
    try:
        results.extend(playsport_live.fetch_finals())   # NBA/WNBA/足球/冰球終場
    except Exception as e:
        print(f"[playsport_live] 終場補洞略過: {e}")
    # 從 score 字串解析出數值比分欄（events + results），供前端直接讀
    _attach_numeric_scores(events)
    _attach_numeric_scores(results)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": status,
        "event_count": len(events),
        "multi_source_count": sum(1 for e in events if e["source_count"] >= 2),
        "events": events,
        "results": results,
    }
    tmp = LATEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, LATEST)
    print(f"[fetch_all] 完成：{len(events)} 場（{payload['multi_source_count']} 場跨來源），"
          f"已寫入 {LATEST}")
    return payload


def refresh_scores_only():
    """只重抓 playsport 即時比分套到現有快照後重寫；不重打 odds 來源、不重抓終場。
    供雲端 job 內高頻刷新（每 ~30s）：odds/終場交給每 5 分鐘的完整 run_once，
    這裡只更新最常變動的 live 比分＋滾球狀態，降低對 playsport 與各 odds 站的負擔。"""
    try:
        with open(LATEST, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        print(f"[scores_only] 無現有快照可刷新，略過: {e}")
        return None
    events = payload.get("events", [])
    for e in events:           # 先清掉上一份的 live/比分，再由 playsport 重套
        e["live"] = False
        e["score"] = ""
        e.pop("bat_side", None)
    try:
        _apply_playsport_live(events, playsport_live.fetch_live())
    except Exception as e:
        print(f"[playsport_live] 略過: {e}")
    _clear_future_live(events)
    events.sort(key=lambda e: (_priority(e), not e["live"], -e["source_count"], e["start"] or "9999"))
    # 終場也一起刷：剛結束的場 30 秒內就有終場。只抓近 2 天保持輕量，與既有 results 合併
    # （完整跑抓的 8 天歷史不能被高頻洗掉）→ 同 key 用新值、舊歷史保留。
    try:
        fresh = playsport_results.fetch_results(days=2) + playsport_live.fetch_finals()
        rmap = {}
        for r in payload.get("results", []) + fresh:   # fresh 在後 → 覆蓋同 key 舊值
            rmap[(r.get("date"), r.get("league_zh"), r.get("home_zh"), r.get("away_zh"))] = r
        results = list(rmap.values())
    except Exception as e:
        results = payload.get("results", [])
        print(f"[scores_only] 終場略過（沿用上一份）: {e}")
    _attach_numeric_scores(events)
    _attach_numeric_scores(results)
    payload["events"] = events
    payload["results"] = results
    payload["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tmp = LATEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, LATEST)
    live_n = sum(1 for e in events if e.get("live"))
    print(f"[scores_only] 比分刷新完成：{live_n} 場滾球中 / 共 {len(events)} 場")
    return payload


if __name__ == "__main__":
    run_once()
