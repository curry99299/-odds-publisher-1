"""跑所有來源 → 跨來源配對 → 寫 data/latest.json。

每個 provider 獨立 try/except，單一來源失敗不影響其他來源。
記錄每個來源的狀態（場數 / ok / error），供前端顯示。
"""
import os
import json
import time
import traceback
from datetime import datetime, timezone, timedelta

from core.matcher import merge, _priority
from core import translate
from core.normalize import match_key, norm_team, teams_similar
from providers import pinnacle, polymarket, onexbet, tsl, panda, espn_scores


_SCORE_WIN = {"baseball": 4.5, "basketball": 3.5, "soccer": 3.0}


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
        s = scores.get((e["sport"], match_key(e["home"], e["away"])))
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


def _apply_deltas(events, prev_events):
    """為每個賽事/來源/腳計算與「上一次更新」相比的漲跌 %（存成 *_chg）。"""
    prev = {}
    for e in prev_events:
        prev[(e["sport"], match_key(e["home"], e["away"]))] = e
    for e in events:
        rec = prev.get((e["sport"], match_key(e["home"], e["away"])))
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
    # ESPN 即時比分 + 校正 live 狀態，最後依新狀態重新排序
    try:
        _apply_scores(events, espn_scores.fetch_scores())
        events.sort(key=lambda e: (_priority(e), not e["live"],
                                   -e["source_count"], e["start"] or "9999"))
    except Exception as e:
        print(f"[fetch_all] ESPN 比分略過: {e}")
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": status,
        "event_count": len(events),
        "multi_source_count": sum(1 for e in events if e["source_count"] >= 2),
        "events": events,
    }
    tmp = LATEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, LATEST)
    print(f"[fetch_all] 完成：{len(events)} 場（{payload['multi_source_count']} 場跨來源），"
          f"已寫入 {LATEST}")
    return payload


if __name__ == "__main__":
    run_once()
