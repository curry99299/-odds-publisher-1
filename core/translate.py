"""自動翻譯隊名/聯盟為繁體中文（用 Perplexity API），結果寫入 i18n 持久快取。

只翻譯「靜態表與快取都沒有、且含拉丁字母」的新名稱；翻過就快取，不重複翻。
無 PERPLEXITY_API_KEY 或翻譯失敗時，靜默跳過（保留原文）。
"""
import os
import re
import json
import requests
from . import i18n

API = "https://api.perplexity.ai/chat/completions"
MODEL = os.environ.get("TRANSLATE_MODEL", "sonar")
BATCH = 50
MAX_PER_RUN = int(os.environ.get("TRANSLATE_MAX_PER_RUN", "600"))  # 每次更新最多翻幾個新名稱


def _key():
    k = os.environ.get("PERPLEXITY_API_KEY", "")
    if k:
        return k
    # 後備：從專案根目錄 .env 讀
    try:
        env = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
        with open(env, encoding="utf-8") as f:
            for line in f:
                if line.startswith("PERPLEXITY_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _call(names, kind):
    """names: list[str] → {原文: 繁中}。"""
    key = _key()
    if not key:
        return {}
    sys_prompt = (
        "你是運動賽事名稱翻譯器。把使用者給的 JSON 陣列中每個"
        + ("隊伍名" if kind == "team" else "聯盟/賽事名")
        + "翻成台灣慣用繁體中文。只輸出一個 JSON 物件，key 為原文、value 為繁中譯名，"
        "不要加任何說明、註解或引用標記。譯名沿用台灣運彩/體育媒體慣用譯法。"
    )
    try:
        r = requests.post(API, timeout=40,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"model": MODEL, "temperature": 0,
                                "messages": [{"role": "system", "content": sys_prompt},
                                             {"role": "user", "content": json.dumps(names, ensure_ascii=False)}]})
        content = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)   # 去除可能的 code fence/前後文字
        return json.loads(m.group(0)) if m else {}
    except Exception as e:
        print(f"[translate] {kind} 批次失敗: {e}")
        return {}


def _run(names, kind, adder):
    uniq, seen = [], set()
    for n in names:
        k = (n or "").strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    uniq = uniq[:MAX_PER_RUN]
    if not uniq:
        return 0
    total = {}
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i:i + BATCH]
        mapping = _call(chunk, kind)
        # 只保留確實翻成中文的（避免回原文）
        for orig, zh in mapping.items():
            if zh and re.search(r"[一-鿿]", str(zh)):
                total[orig] = str(zh)
    if total:
        adder(total)
    return len(total)


def ensure_translations(team_names, league_names):
    teams = [n for n in set(team_names) if i18n.needs_team(n)]
    leagues = [n for n in set(league_names) if i18n.needs_league(n)]
    nt = _run(teams, "team", i18n.add_teams) if teams else 0
    nl = _run(leagues, "league", i18n.add_leagues) if leagues else 0
    if nt or nl:
        print(f"[translate] 新增繁中：隊名 {nt}、聯盟 {nl}")
    return nt + nl
