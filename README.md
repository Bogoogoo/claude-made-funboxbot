# 戰鬥陀螺補貨/新品監控機器人

監控 https://shop.funbox.com.tw/categories/XI/KB ，有新商品上架時透過 Telegram 通知你。
部署到 GitHub Actions 後完全不需要開電腦，全自動在雲端執行。

---

## 第一步：資料來源（已確認完成，不用你動手）

原本想解析網頁 HTML，但透過你幫忙用瀏覽器 F12 檢查，我們發現這個網站其實是 **Vue.js 單頁應用程式**，商品資料是網頁載入後才用背景 API 抓取的。我們已經抓到正確的 API 網址：

```
https://shop.funbox.com.tw/category_products/XI/KB.json?limit=18&page=1
```

這比解析 HTML 更穩定、更精確，而且這個 API 回傳的資料裡包含 `inventory_quantity`（庫存數量），所以 `monitor.py` 現在能做到兩種通知：

- 🆕 **新商品上架**：清單裡出現從沒看過的商品 ID
- 📦 **補貨通知**：原本庫存是 0 的商品，庫存變成大於 0

不需要你再去猜 CSS class 或處理 JavaScript 動態載入的問題。

### 本機測試

裝好 Python 後，在專案資料夾執行：

```bash
pip install -r requirements.txt
python monitor.py
```

- 印出「目前抓到 N 樣商品」代表連線成功（N 可能是 0，因為戰鬥陀螺目前確實沒貨）
- 如果出現 HTTP 錯誤或 JSON 解析失敗，代表 API 網址或回傳格式改變了，把錯誤訊息貼給我

---

## 第二步：建立 Telegram Bot

1. Telegram 搜尋 **@BotFather**，傳送 `/newbot`，依指示命名
2. 取得 **Bot Token**（格式類似 `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`）
3. 搜尋 **@userinfobot**，傳送任意訊息，取得你的 **Chat ID**
4. 重要：先在 Telegram 上主動傳一句話給你剛建立的 Bot（跟它對話一次），Bot 才能回傳訊息給你

---

## 第三步：上傳到 GitHub

1. 到 https://github.com 建立一個新的 **Private repository**（例如叫 `beyblade-monitor`）
   - 建議設為 Private，避免你的監控邏輯被公開看到
2. 把這個資料夾內的所有檔案上傳上去（可以用網頁介面拖曳上傳，或用 git 指令）：

```bash
cd beyblade-monitor
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/你的帳號/beyblade-monitor.git
git push -u origin main
```

---

## 第四步：設定 GitHub Secrets（存放 Token，不會外洩）

1. 到你的 repo 頁面 → **Settings** → 左側選單 **Secrets and variables** → **Actions**
2. 點 **New repository secret**，新增兩筆：
   - Name: `TELEGRAM_BOT_TOKEN`　Value: 你的 Bot Token
   - Name: `TELEGRAM_CHAT_ID`　Value: 你的 Chat ID

---

## 第五步：測試執行

1. 到 repo 頁面上方 **Actions** 分頁
2. 左側選 **Beyblade Stock Monitor**
3. 右側點 **Run workflow** → **Run workflow**（手動觸發一次）
4. 等約 30 秒到 1 分鐘，點進去看執行紀錄（log）
   - 若成功，你的 Telegram 應該會收到「發現新商品」訊息（因為是第一次執行，全部商品都會被當成「新」的）
   - 之後就會變成只有真的新上架的商品才會通知

---

## 之後怎麼運作

- GitHub Actions 會依照 `.github/workflows/monitor.yml` 裡設定的排程，**每 10 分鐘自動執行一次**，完全不需要你開電腦
- 每次執行都會把最新的商品清單存回 repo（`data/seen_products.json`），下次執行時才知道「哪些是新的」
- 想調整檢查頻率，修改 `monitor.yml` 裡的 `cron: "*/10 * * * *"`（數字是分鐘間隔，GitHub Actions 最短建議 5 分鐘以上，太頻繁可能被限流）

---

## 常見問題

**Q: 抓不到商品，一直顯示「找不到任何符合 selector 的商品卡片」？**
A: 回到「第一步」重新確認 selector；也可能是網站有做防爬蟲機制（例如需要特定 headers 或會顯示驗證頁面），可以把 log 訊息貼給我，我幫你調整。

**Q: 想同時監控多個分類頁或多個關鍵字商品？**
A: 可以，把 `TARGET_URL` 改成清單，迴圈跑多次即可，需要的話跟我說，我幫你擴充。

**Q: GitHub Actions 免費額度會不會用完？**
A: Private repo 每月有 2000 分鐘免費額度，這個腳本每次執行約 30 秒~1 分鐘，就算每 10 分鐘跑一次，一個月也才用約 150 分鐘，完全夠用。
