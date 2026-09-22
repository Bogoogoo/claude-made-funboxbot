"""
戰鬥陀螺補貨/新品監控腳本（合併版）
同時監控：
1. Funbox 玩具官網（分類頁 + JSON API，能偵測新品與補貨）
   https://shop.funbox.com.tw/categories/XI/KB
2. 誠品線上（兩個策展頁，僅能偵測新品上架，無庫存資訊）
   https://www.eslite.com/exhibitions/CU202608-00061
   https://www.eslite.com/exhibitions/CU202310-00113

功能：
- 偵測到「新商品上架」或「補貨」（僅 Funbox 支援）時，透過 Telegram 通知
- 支援 Telegram 指令：/status（查詢現況）、/check（立即檢查）、/help（說明）
- 任一來源抓取失敗，不影響其他來源的正常運作；程式異常會透過 Telegram 回報（30 分鐘防洗版）
"""

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============================================================
# 設定區
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DATA_FILE = Path(__file__).parent / "data" / "seen_products.json"
BOT_STATE_FILE = Path(__file__).parent / "data" / "bot_state.json"
ERROR_ALERT_COOLDOWN_SECONDS = 30 * 60  # 30 分鐘

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

# ---- Funbox 設定 ----
FUNBOX_SITE_ROOT = "https://shop.funbox.com.tw"
FUNBOX_API_URL = FUNBOX_SITE_ROOT + "/category_products/XI/KB.json"
FUNBOX_CATEGORY_PAGE_URL = FUNBOX_SITE_ROOT + "/categories/XI/KB"
FUNBOX_PAGE_LIMIT = 18

# ---- 誠品設定 ----
ESLITE_SITE_ROOT = "https://www.eslite.com"
ESLITE_TARGET_URLS = [
    ESLITE_SITE_ROOT + "/exhibitions/CU202608-00061",
    ESLITE_SITE_ROOT + "/exhibitions/CU202310-00113",
]
ESLITE_KEYWORD_FILTER = ["戰鬥陀螺", "BEYBLADE", "beyblade"]
ESLITE_PRODUCT_LINK_PATTERN = re.compile(r"^/product/\d+")


# ============================================================
# 共用：資料存取
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


def load_all_seen() -> dict:
    """回傳格式: {"funbox": {...}, "eslite": {...}}"""
    data = load_json_file(DATA_FILE)
    data.setdefault("funbox", {})
    data.setdefault("eslite", {})
    return data


def save_all_seen(data: dict):
    save_json_file(DATA_FILE, data)


def load_bot_state() -> dict:
    return load_json_file(BOT_STATE_FILE)


def save_bot_state(state: dict):
    save_json_file(BOT_STATE_FILE, state)


# ============================================================
# Funbox：抓取與解析
# ============================================================
def fetch_funbox_products() -> dict:
    """
    回傳格式: {商品ID(字串): {"title":..., "url":..., "price":..., "in_stock": bool}}
    """
    all_items = []
    page = 1
    while True:
        resp = requests.get(
            FUNBOX_API_URL,
            params={"limit": FUNBOX_PAGE_LIMIT, "page": page},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        all_items.extend(items)
        page += 1
        if page > 50:
            print("[警告][Funbox] 已翻超過 50 頁，強制停止。")
            break

    products = {}
    for item in all_items:
        product_id = str(item.get("id"))
        title = item.get("title", "（無標題）")
        url_path = item.get("url", "")
        full_url = FUNBOX_SITE_ROOT + url_path if url_path.startswith("/") else url_path
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


# ============================================================
# 誠品：抓取與解析
# ============================================================
def matches_eslite_keyword(name: str) -> bool:
    return any(kw.lower() in name.lower() for kw in ESLITE_KEYWORD_FILTER)


def parse_eslite_page(soup: BeautifulSoup) -> dict:
    products = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not ESLITE_PRODUCT_LINK_PATTERN.match(href):
            continue
        full_url = ESLITE_SITE_ROOT + href

        name = a.get_text(strip=True)
        if not name:
            img = a.find("img")
            if img and img.get("alt"):
                name = img.get("alt").strip()

        if full_url not in products or (name and len(name) > len(products[full_url])):
            if name:
                products[full_url] = name
            elif full_url not in products:
                products[full_url] = full_url
    return products


def fetch_eslite_products() -> dict:
    """
    回傳格式: {商品完整網址: {"title":..., "url":..., "price": None, "in_stock": None}}
    （誠品沒有價格與庫存資訊，price/in_stock 固定為 None，補貨偵測不適用）
    """
    all_products = {}
    success_count = 0

    for url in ESLITE_TARGET_URLS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            print(f"[警告][誠品] 抓取 {url} 失敗，略過這頁: {e}")
            continue

        success_count += 1
        page_products = parse_eslite_page(soup)
        for full_url, name in page_products.items():
            if matches_eslite_keyword(name):
                all_products[full_url] = {
                    "title": name,
                    "url": full_url,
                    "price": None,
                    "in_stock": None,
                }

    if success_count == 0:
        raise RuntimeError(f"誠品的 {len(ESLITE_TARGET_URLS)} 個監控頁面全部抓取失敗")

    return all_products


# ============================================================
# 來源定義：把兩個網站統一成一致的介面，方便主流程共用邏輯
# ============================================================
SOURCES = {
    "funbox": {
        "label": "Funbox",
        "fetch": fetch_funbox_products,
        "supports_restock": True,
    },
    "eslite": {
        "label": "誠品",
        "fetch": fetch_eslite_products,
        "supports_restock": False,
    },
}


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
        print(f"[警告] 取得 Telegram 指令失敗（不影響本次檢查）: {e}")
        return []


def build_status_text(all_current: dict, fetch_errors: dict) -> str:
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    lines = ["📊 <b>戰鬥陀螺監控 - 目前狀態</b>", "", f"查詢時間：{now_str}"]

    for key, source in SOURCES.items():
        lines.append("")
        if key in fetch_errors:
            lines.append(f"❌ {source['label']}：本次抓取失敗（{fetch_errors[key]}）")
            continue

        products = all_current.get(key, {})
        lines.append(f"✅ {source['label']}：{len(products)} 樣商品")
        if source["supports_restock"]:
            in_stock_count = sum(1 for p in products.values() if p.get("in_stock"))
            lines.append(f"　其中有庫存：{in_stock_count}")
            for p in products.values():
                if p.get("in_stock"):
                    lines.append(f"　・{p['title']}（{format_price(p.get('price'))}）")

    return "\n".join(lines)


HELP_TEXT = (
    "🤖 <b>可用指令</b>\n\n"
    "/status - 查詢兩個來源目前的追蹤狀態\n"
    "/check - 立即手動檢查一次，並回報結果\n"
    "/help - 顯示這則說明\n\n"
    "系統平常每分鐘會自動檢查一次，有新商品上架或補貨（僅 Funbox 支援補貨偵測）會主動通知你。"
)


def handle_telegram_commands(bot_state: dict, all_current: dict, fetch_errors: dict):
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

        if not TELEGRAM_CHAT_ID or sender_chat_id != str(TELEGRAM_CHAT_ID):
            print(f"[提示] 忽略來自非授權 chat_id ({sender_chat_id}) 的訊息")
            continue

        command = text.split()[0].lower() if text else ""

        if command == "/status":
            send_telegram_message(build_status_text(all_current, fetch_errors))
        elif command == "/check":
            send_telegram_message("🔍 收到，正在為你檢查最新狀態...")
            send_telegram_message(build_status_text(all_current, fetch_errors))
        elif command in ("/help", "/start"):
            send_telegram_message(HELP_TEXT)
        elif command:
            send_telegram_message(f"沒有這個指令喔：{command}\n\n{HELP_TEXT}")


# ============================================================
# 錯誤回報
# ============================================================
def report_error(bot_state: dict, source_label: str, error: Exception):
    now_ts = time.time()
    cooldown_key = f"last_error_alert_ts_{source_label}"
    last_alert_ts = bot_state.get(cooldown_key, 0)

    error_summary = f"{type(error).__name__}: {error}"
    print(f"[錯誤][{source_label}] 程式執行異常: {error_summary}")
    print(traceback.format_exc())

    if now_ts - last_alert_ts < ERROR_ALERT_COOLDOWN_SECONDS:
        print(f"[提示][{source_label}] 距離上次錯誤通知未滿 30 分鐘，這次不重複發送。")
        return error_summary

    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    message = (
        f"🚨 <b>{source_label} 監控發生異常</b>\n\n"
        f"時間：{now_str}\n"
        f"錯誤內容：{error_summary}\n\n"
        "下一輪（約 1 分鐘後）會自動重試，其他來源不受影響。\n"
        "如果持續發生，可能是網站改版，需要人工檢查。"
    )
    send_telegram_message(message)
    bot_state[cooldown_key] = now_ts
    return error_summary


# ============================================================
# 主流程
# ============================================================
def main():
    bot_state = load_bot_state()
    all_seen = load_all_seen()

    all_current = {}
    fetch_errors = {}

    for key, source in SOURCES.items():
        print(f"開始檢查來源: {source['label']}")
        try:
            all_current[key] = source["fetch"]()
            print(f"[{source['label']}] 抓到 {len(all_current[key])} 樣商品")
        except Exception as e:
            error_summary = report_error(bot_state, source["label"], e)
            fetch_errors[key] = error_summary
            # 這個來源這次失敗，保留上次記錄，不覆蓋、不比對新品
            all_current[key] = all_seen.get(key, {})

    # 處理 Telegram 指令（用這次抓到的最新資料回答）
    handle_telegram_commands(bot_state, all_current, fetch_errors)

    # 逐一來源比對新品／補貨並發送通知
    for key, source in SOURCES.items():
        if key in fetch_errors:
            continue  # 這次失敗的來源，不比對、不更新記錄

        current_products = all_current[key]
        seen_products = all_seen.get(key, {})

        new_ids = []
        restocked_ids = []

        for pid, info in current_products.items():
            if pid not in seen_products:
                new_ids.append(pid)
            elif source["supports_restock"]:
                was_in_stock = seen_products[pid].get("in_stock", False)
                if (not was_in_stock) and info.get("in_stock"):
                    restocked_ids.append(pid)

        for pid in new_ids:
            info = current_products[pid]
            if source["supports_restock"]:
                stock_note = "現貨" if info.get("in_stock") else "目前無庫存/預購"
                message = (
                    f"🆕 <b>[{source['label']}] 發現新商品！</b>\n\n"
                    f"{info['title']}\n"
                    f"{format_price(info.get('price'))}（{stock_note}）\n\n"
                    f"{info['url']}"
                )
            else:
                message = (
                    f"🆕 <b>[{source['label']}] 發現新商品！</b>\n\n"
                    f"{info['title']}\n\n"
                    f"{info['url']}"
                )
            send_telegram_message(message)

        for pid in restocked_ids:
            info = current_products[pid]
            message = (
                f"📦 <b>[{source['label']}] 補貨通知！</b>\n\n"
                f"{info['title']}\n"
                f"{format_price(info.get('price'))}\n\n"
                f"{info['url']}"
            )
            send_telegram_message(message)

        if not new_ids and not restocked_ids:
            print(f"[{source['label']}] 沒有新商品，也沒有補貨。")

        # 這個來源這次成功執行，清掉它的錯誤冷卻紀錄
        cooldown_key = f"last_error_alert_ts_{source['label']}"
        if cooldown_key in bot_state:
            del bot_state[cooldown_key]

        # 更新這個來源的記錄
        all_seen[key] = current_products

    save_all_seen(all_seen)
    save_bot_state(bot_state)


if __name__ == "__main__":
    main()
