# odds-publisher

每 10 分鐘抓取運動賠率（Pinnacle / Polymarket / 1xbet）+ ESPN 即時比分，
發布到 Supabase Storage 公開檔，供線上 BetLedger 的「賠率比較」分頁即時讀取。

## 一次性設定（約 5 分鐘）

### 1. Supabase：建公開 bucket + 取 service key
- Supabase 後台 → **Storage** → New bucket → 名稱 `odds`、勾選 **Public bucket**。
- 後台 → **Settings → API** → 複製 **`service_role`** key（保密！只放 GitHub Secret，勿放前端）。

### 2. 建 GitHub repo 並推送
```bash
cd /Users/zhuangshaopei/Documents/odds-publisher
git init && git add . && git commit -m "odds publisher"
# 用 GitHub 網站建一個「私有」repo（例如 odds-publisher），然後：
git branch -M main
git remote add origin https://github.com/<你的帳號>/odds-publisher.git
git push -u origin main
```

### 3. 設定 GitHub Secrets
repo → **Settings → Secrets and variables → Actions → New repository secret**，加三個：
| Name | Value |
|------|-------|
| `SUPABASE_URL` | `https://yxpoqdihxnkxcnzebrwv.supabase.co` |
| `SUPABASE_SERVICE_KEY` | 步驟 1 的 service_role key |
| `PERPLEXITY_API_KEY` | （選填，翻譯新隊名用；沒有就顯示英文） |

### 4. 啟用 / 測試
- repo → **Actions** 分頁 → 若提示啟用 workflow 就啟用。
- 點 **Publish odds to Supabase → Run workflow** 手動跑一次。
- 成功後檢查公開檔：
  `https://yxpoqdihxnkxcnzebrwv.supabase.co/storage/v1/object/public/odds/latest.json`
- 之後每 10 分鐘自動更新；線上 BetLedger「賠率比較」會自動讀到最新賠率（免重新部署）。

## 本機手動跑（不透過 GitHub）
```bash
SUPABASE_SERVICE_KEY=你的key python3 publish.py     # 跑一次
# 或每 10 分鐘迴圈：見 odds-compare/publish_loop.sh
```

> 注意：GitHub Actions 排程最短約每 5 分鐘、且高峰期可能延遲幾分鐘，屬正常。
