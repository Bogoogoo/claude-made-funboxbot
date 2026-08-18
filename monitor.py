"""
戰鬥陀螺補貨/新品監控腳本
監控 https://shop.funbox.com.tw/categories/XI/KB
資料來源：該分類頁背後呼叫的 JSON API
    https://shop.funbox.com.tw/category_products/XI/KB.json?limit=18&page=N

功能：
1. 偵測到「新商品上架」或「原本缺貨的商品補貨」時，透過 Telegram 發送通知
2. 支援 Telegram 指令：/status（查詢現況）、/check（立即檢查）、/help（說明）
3. 程式執行異常時會透過 Telegram 回報（有 30 分鐘冷卻機制，避免洗版）
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

import requests

# ============================================================
# 設定區
# ============================================================
SITE_ROOT = "https://shop.funbox.com.tw"
API_URL_TEMPLATE = SITE_ROOT + "/category_products/XI/KB.json"
CATEGORY_PAGE_URL = SITE_ROOT + "/categories/XI/KB"  # 純粹給通知訊息附連結用
PAGE_LIMIT = 18  # 跟網站前端一致的每頁筆數，不用改

# Telegram 設定 -> 從環境變數讀取（GitHub Actions Secrets 會注入）
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 存放「上次看過的商品清單」的檔案
DATA_FILE = Path(__file__).parent / "data" / "seen_products.json"

# 存放「機器人狀態」的檔案（上次處理到哪則 Telegram 訊息、上次錯誤通知時間等）
BOT_STATE_FILE = Path(__file__).parent / "data" / "bot_state.json"

# 同類型錯誤通知的冷卻時間（秒），避免網站長時間故障時被訊息洗版
ERROR_ALERT_COOLDOWN_SECONDS = 30 * 60  # 30 分鐘

# 模擬一般瀏覽器的完整 headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": CATEGORY_PAGE_URL,
}


# ============================================================
# 資料存取（商品清單 + 機器人狀態）
# ============================================================
def load_json_file(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json_file(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_seen_products() -> dict:
    return load_json_file(DATA_FILE)


def save_seen_products(products: dict):
    save_json_file(DATA_FILE, products)


def load_bot_state() -> dict:
    return load_json_file(BOT_STATE_FILE)


def save_bot_state(state: dict):
    save_json_file(BOT_STATE_FILE, state)


# ============================================================
# 商品資料抓取與解析
# ============================================================
def fetch_all_products() -> list:
    """呼叫 JSON API，自動翻頁抓完所有商品，回傳原始商品資料 list"""
    all_items = []
    page = 1

    while True:
        resp = requests.get(
            API_URL_TEMPLATE,
            params={"limit": PAGE_LIMIT, "page": page},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json()

        if not items:
            break

        all_items.extend(items)
        page += 1

        if page > 50:  # 保險：避免無限迴圈
            print("[警告] 已翻超過 50 頁，強制停止，請確認網站是否正常。")
            break

    return all_items


def parse_products(raw_items: list) -> dict:
    """
    回傳格式: {商品ID(字串): {"title":..., "url":..., "price":..., "in_stock": bool}}
    """
    products = {}
    for item in raw_items:
        product_id = str(item.get("id"))
        title = item.get("title", "（無標題）")
        url_path = item.get("url", "")
        full_url = SITE_ROOT + url_path if url_path.startswith("/") else url_path
        price = item.get("price")

        variants = item.get("variants", [])
        in_stock = any((v.get("inventory_quantity") or 0) > 0 for v in variants)

        products[product_id] = {
            "title": title,
            "url": full_url,
            "price": price,
            "in_stock": in_stock,
        }
    return products


def format_price(price):
    if price is None:
        return ""
    try:
        return f"NT$ {price:,.0f}"
    except (ValueError, TypeError):
        return str(price)


# ============================================================
# Telegram 相關
# ============================================================
def send_telegram_message(text: str, chat_id: str = None):
    """發送 Telegram 訊息，預設發給設定好的 TELEGRAM_CHAT_ID"""
    target_chat_id = chat_id or TELEGRAM_CHAT_ID

    if not TELEGRAM_BOT_TOKEN or not target_chat_id:
        print("[錯誤] 尚未設定 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，無法發送通知。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, data=payload, timeout=20)
        if resp.status_code != 200:
            print(f"[錯誤] Telegram 發送失敗: {resp.status_code} {resp.text}")
        else:
            print("[成功] Telegram 訊息已發送")
    except requests.RequestException as e:
        print(f"[錯誤] 發送 Telegram 訊息時連線失敗: {e}")


def get_telegram_updates(offset: int) -> list:
    """
    取得 Telegram 新訊息（指令）。
    offset = 上次處理到的 update_id + 1，Telegram 只會回傳這之後的新訊息。
    """
    if not TELEGRAM_BOT_TOKEN:
        return []

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 0}
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            print(f"[警告] getUpdates 回應異常: {data}")
            return []
        return data.get("result", [])
    except requests.RequestException as e:
        print(f"[警告] 取得 Telegram 指令失敗（不影響本次庫存檢查）: {e}")
        return []


def build_status_text(products: dict) -> str:
    total = len(products)
    in_stock_count = sum(1 for p in products.values() if p["in_stock"])
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    lines = [
        "📊 <b>目前狀態</b>",
        "",
        f"追蹤商品總數：{total}",
        f"目前有庫存：{in_stock_count}",
        f"查詢時間：{now_str}",
    ]

    if in_stock_count > 0:
        lines.append("")
        lines.append("有庫存的商品：")
        for info in products.values():
            if info["in_stock"]:
                lines.append(f"・{info['title']}（{format_price(info['price'])}）")

    return "\n".join(lines)


HELP_TEXT = (
    "🤖 <b>可用指令</b>\n\n"
    "/status - 查詢目前追蹤狀態（商品總數、有庫存數量）\n"
    "/check - 立即手動檢查一次，並回報結果\n"
    "/help - 顯示這則說明\n\n"
    "系統平常每分鐘會自動檢查一次，有新商品上架或補貨會主動通知你，不需要手動下指令。"
)


def handle_telegram_commands(bot_state: dict, current_products: dict):
    """
    處理使用者傳來的 Telegram 指令。
    只回應來自設定好的 TELEGRAM_CHAT_ID 的訊息，避免陌生人濫用你的 Bot。
    """
    last_update_id = bot_state.get("last_update_id", 0)
    updates = get_telegram_updates(offset=last_update_id + 1)

    for update in updates:
        update_id = update.get("update_id", 0)
        bot_state["last_update_id"] = max(bot_state.get("last_update_id", 0), update_id)

        message = update.get("message") or update.get("channel_post")
        if not message:
            continue

        sender_chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        # 安全性：只回應設定好的那個 chat，避免其他人對你的 Bot 亂下指令
        if not TELEGRAM_CHAT_ID or sender_chat_id != str(TELEGRAM_CHAT_ID):
            print(f"[提示] 忽略來自非授權 chat_id ({sender_chat_id}) 的訊息")
            continue

        command = text.split()[0].lower() if text else ""

        if command == "/status":
            send_telegram_message(build_status_text(current_products))
        elif command == "/check":
            send_telegram_message("🔍 收到，正在為你檢查最新狀態...")
            send_telegram_message(build_status_text(current_products))
        elif command in ("/help", "/start"):
            send_telegram_message(HELP_TEXT)
        elif command:
            send_telegram_message(f"沒有這個指令喔：{command}\n\n{HELP_TEXT}")


# ============================================================
# 錯誤回報
# ============================================================
def report_error(bot_state: dict, error: Exception):
    """
    發生例外時，透過 Telegram 通知，並附上 30 分鐘冷卻機制避免洗版。
    """
    now_ts = time.time()
    last_alert_ts = bot_state.get("last_error_alert_ts", 0)

    error_summary = f"{type(error).__name__}: {error}"
    print(f"[錯誤] 程式執行異常: {error_summary}")
    print(traceback.format_exc())

    if now_ts - last_alert_ts < ERROR_ALERT_COOLDOWN_SECONDS:
        print("[提示] 距離上次錯誤通知未滿 30 分鐘，這次不重複發送 Telegram 通知。")
        return

    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    message = (
        "🚨 <b>監控程式發生異常</b>\n\n"
        f"時間：{now_str}\n"
        f"錯誤內容：{error_summary}\n\n"
        "程式這次執行失敗，下一輪（約 1 分鐘後）會自動重試。\n"
        "如果持續發生，可能是網站改版或防爬蟲機制變更，需要人工檢查。"
    )
    send_telegram_message(message)
    bot_state["last_error_alert_ts"] = now_ts


# ============================================================
# 主流程
# ============================================================
def main():
    bot_state = load_bot_state()

    print(f"開始檢查: {CATEGORY_PAGE_URL}")

    try:
        raw_items = fetch_all_products()
        current_products = parse_products(raw_items)
    except Exception as e:
        # 抓取失敗：回報錯誤，仍然嘗試處理使用者指令（用舊資料回答 /status），最後結束並讓這次 workflow 標記失敗
        report_error(bot_state, e)
        old_products = load_seen_products()
        handle_telegram_commands(bot_state, old_products)
        save_bot_state(bot_state)
        sys.exit(1)

    print(f"目前抓到 {len(current_products)} 樣商品")

    # 先處理使用者指令（用最新抓到的資料回答，比較準）
    handle_telegram_commands(bot_state, current_products)

    # 比對新舊清單，判斷新商品 / 補貨
    seen_products = load_seen_products()
    new_ids = []
    restocked_ids = []

    for pid, info in current_products.items():
        if pid not in seen_products:
            new_ids.append(pid)
        else:
            was_in_stock = seen_products[pid].get("in_stock", False)
            if (not was_in_stock) and info["in_stock"]:
                restocked_ids.append(pid)

    if new_ids:
        print(f"發現 {len(new_ids)} 樣新商品！")
        for pid in new_ids:
            info = current_products[pid]
            stock_note = "現貨" if info["in_stock"] else "目前無庫存/預購"
            message = (
                f"🆕 <b>發現新商品！</b>\n\n"
                f"{info['title']}\n"
                f"{format_price(info['price'])}（{stock_note}）\n\n"
                f"{info['url']}"
            )
            send_telegram_message(message)

    if restocked_ids:
        print(f"發現 {len(restocked_ids)} 樣商品補貨！")
        for pid in restocked_ids:
            info = current_products[pid]
            message = (
                f"📦 <b>補貨通知！</b>\n\n"
                f"{info['title']}\n"
                f"{format_price(info['price'])}\n\n"
                f"{info['url']}"
            )
            send_telegram_message(message)

    if not new_ids and not restocked_ids:
        print("沒有新商品，也沒有補貨。")

    # 這次成功執行，清掉錯誤冷卻紀錄，下次真的出錯時會立刻通知（不受舊的冷卻時間影響）
    if "last_error_alert_ts" in bot_state:
        del bot_state["last_error_alert_ts"]

    save_seen_products(current_products)
    save_bot_state(bot_state)


if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()
