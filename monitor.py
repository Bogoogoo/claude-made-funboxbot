"""
戰鬥陀螺補貨/新品監控腳本
監控 https://shop.funbox.com.tw/categories/XI/KB
資料來源：該分類頁背後呼叫的 JSON API
    https://shop.funbox.com.tw/category_products/XI/KB.json?limit=18&page=N
偵測到「新商品上架」或「原本缺貨的商品補貨」時，透過 Telegram 發送通知
"""

import json
import os
import sys
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


def fetch_all_products() -> list:
    """
    呼叫 JSON API，自動翻頁抓完所有商品，回傳原始商品資料 list
    """
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

        # 保險：避免網站行為異常造成無限迴圈
        if page > 50:
            print("[警告] 已翻超過 50 頁，強制停止，請確認網站是否正常。")
            break

    return all_items


def parse_products(raw_items: list) -> dict:
    """
    把 API 回傳的原始資料整理成好比對的格式
    回傳格式: {商品ID(字串): {"title":..., "url":..., "price":..., "in_stock": bool}}
    """
    products = {}

    for item in raw_items:
        product_id = str(item.get("id"))
        title = item.get("title", "（無標題）")
        url_path = item.get("url", "")
        full_url = SITE_ROOT + url_path if url_path.startswith("/") else url_path
        price = item.get("price")

        # 只要任一個 variant 的庫存 > 0，就視為「有庫存」
        variants = item.get("variants", [])
        in_stock = any(
            (v.get("inventory_quantity") or 0) > 0 for v in variants
        )

        products[product_id] = {
            "title": title,
            "url": full_url,
            "price": price,
            "in_stock": in_stock,
        }

    return products


def load_seen_products() -> dict:
    """讀取上次記錄的商品清單"""
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen_products(products: dict):
    """把目前商品清單存檔"""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)


def send_telegram_message(text: str):
    """發送 Telegram 通知"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[錯誤] 尚未設定 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，無法發送通知。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, data=payload, timeout=20)
    if resp.status_code != 200:
        print(f"[錯誤] Telegram 發送失敗: {resp.status_code} {resp.text}")
    else:
        print("[成功] Telegram 通知已發送")


def format_price(price):
    if price is None:
        return ""
    try:
        return f"NT$ {price:,.0f}"
    except (ValueError, TypeError):
        return str(price)


def main():
    print(f"開始檢查: {CATEGORY_PAGE_URL}")

    try:
        raw_items = fetch_all_products()
    except requests.RequestException as e:
        print(f"[錯誤] 抓取 API 失敗: {e}")
        sys.exit(1)
    except ValueError as e:
        # JSON 解析失敗，通常代表 API 回傳格式改變或不是 JSON
        print(f"[錯誤] API 回傳內容無法解析成 JSON: {e}")
        sys.exit(1)

    current_products = parse_products(raw_items)
    print(f"目前抓到 {len(current_products)} 樣商品")

    seen_products = load_seen_products()

    new_ids = []       # 全新上架的商品
    restocked_ids = []  # 原本缺貨、現在補貨的商品

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

    # 不論結果如何，都更新記錄成目前的完整清單
    # 注意：就算目前是 0 樣商品，也直接存檔覆蓋——因為現在資料來源是可靠的 API，
    # 不再需要像 HTML 解析那樣擔心「抓到 0 筆」代表 selector 壞掉的問題
    save_seen_products(current_products)


if __name__ == "__main__":
    main()
