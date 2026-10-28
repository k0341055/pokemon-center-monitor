"""
pokemoncenter-online.com 補貨偵測器
- 偵測：requests 平行抓取商品頁，解析 #availability 與「品切れ」標記，每輪約 1 秒
- 通知：Gmail 廣播，補貨立即通知、仍有庫存每 1 小時提醒一次；確認售完後重置，下次補貨再立即通知
- 排隊：偵測到 Queue-it 排隊頁（無法判斷庫存）時另發提醒信，同樣 1 小時冷卻
"""

import html
import json
import logging
import os
import random
import re
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

load_dotenv()

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

BASE_URL = "https://www.pokemoncenter-online.com"

CHECK_ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_products.json"))
NOTIFY_COOLDOWN = timedelta(hours=1)
TEST_MODE = os.environ.get("TEST_MODE", "").strip() not in ("", "0", "false")
SEND_TEST_EMAIL = os.environ.get("SEND_TEST_EMAIL", "").strip() not in ("", "0", "false")

TW_TZ = timezone(timedelta(hours=8))  # 台灣時區 UTC+8

GMAIL_SENDER = os.environ["GMAIL_SENDER"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
GMAIL_RECIPIENTS = [
    addr.strip()
    for addr in os.environ["GMAIL_RECIPIENTS"].split(",")
    if addr.strip()
]

# ── 監控商品清單（id = 商品頁網址中的 13 碼 JAN）──────────
WATCH_PRODUCTS: list[dict] = [
    {"id": "4521329454221", "label": "🎄 聖誕樹皮卡丘娃娃"},
    {"id": "4521329454238", "label": "🍪 偷吃餅乾皮卡丘吊飾"},
    {"id": "4521329454252", "label": "🧸 皮卡丘與多邊獸Ⅱ玩具吊飾"},
    {"id": "4521329454306", "label": "🎁 聖誕準備啾！蓬鬆玩偶 BOX"},
    {"id": "4521329425672", "label": "😴 Pokémon Sleep 晚安咚咚鼠娃娃"},
    {"id": "4521329419589", "label": "🚪 咚咚鼠等身大門擋（2.2kg）"},
    {"id": "4521329434230", "label": "🧲 咚咚鼠・電電蟲磁鐵掛勾"},
    {"id": "4521329436845", "label": "🤍 淺淺丘娃娃（Pokopia 淺色皮卡丘）"},
    {"id": "4521329454535", "label": "🤍 淺淺丘登山扣吊飾"},
    {"id": "4521329453422", "label": "🟣 百變怪變身皮卡丘吊飾"},
]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# Queue-it 排隊時的狀態 key（存放於 notified，與商品 id 共用冷卻機制）
_QUEUE_KEY = "__queue__"

# ── 商品頁解析（Salesforce Commerce Cloud 商品頁）──────────
# 有庫存：<input id="availability" value="true">，無「品切れ」標籤，按鈕 class 無 default
# 售完  ：<input id="availability" value="false">，<p class="stock"><span>品切れ</span></p>
_AVAILABILITY_RE = re.compile(r'id="availability"\s+value="(true|false)"')
_STOCK_LABEL_RE = re.compile(r'<p class="stock">\s*<span>([^<]*)</span>')
_TITLE_RE = re.compile(r'<h1 class="lead">([^<]*)')
_PRICE_RE = re.compile(r'<p class="price[^"]*">\s*<span class="txt">([^<]*)')
_LIMIT_RE = re.compile(r'<p class="limit">([^<]*)')
_BUTTON_RE = re.compile(r'<div class="add-to-cart-button[^"]*"><a href="">([^<]*)</a>')

# 每個商品各自一個 Session（平行抓取時不共用，仍可在輪與輪之間重用 TCP/TLS connection）
_SESSIONS: dict = {}


def _session_for(product_id: str) -> requests.Session:
    sess = _SESSIONS.get(product_id)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,zh-TW;q=0.9,en;q=0.8",
        })
        sess.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0))
        _SESSIONS[product_id] = sess
    return sess


# ─────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[0]}***@{domain}"


def _product_url(product_id: str) -> str:
    return f"{BASE_URL}/{product_id}.html"


def _first(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(text)
    return html.unescape(m.group(1)).strip() if m else ""


# ─────────────────────────────────────────────
# 狀態管理（1 小時冷卻）
# ─────────────────────────────────────────────


def load_state() -> dict:
    """
    回傳 notified：{商品id: ISO時間戳}  ← 1 小時通知冷卻
    確認售完時會移除該商品，下次補貨即視為新補貨、立即通知。
    """
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("notified", {})
    return {}


def save_state(notified: dict):
    STATE_FILE.write_text(
        json.dumps(
            {
                "notified": notified,
                "updated": datetime.now(TW_TZ).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _parse_ts(ts_str: str) -> datetime:
    """解析 ISO 時間字串，若無時區資訊則補上台灣時區。"""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TW_TZ)
    return dt


# ─────────────────────────────────────────────
# 擷取商品頁庫存
# ─────────────────────────────────────────────


def parse_product_page(product: dict, page_html: str, final_url: str) -> dict:
    """
    解析商品頁 HTML，回傳 status：
      "in_stock"  ：availability=true 且無「品切れ」標籤
      "sold_out"  ：availability=false 或顯示「品切れ」
      "queue"     ：被導向 Queue-it 排隊頁，無法判斷庫存
      "unknown"   ：頁面缺少庫存標記（可能被 bot 防護攔截或改版）
    """
    result = {
        "id": product["id"],
        "label": product["label"],
        "url": _product_url(product["id"]),
        "status": "unknown",
        "title": "",
        "price": "",
        "limit": "",
        "button": "",
        "reason": "",
    }

    if "queue-it" in urlparse(final_url).netloc:
        result["status"] = "queue"
        result["reason"] = f"導向排隊頁 {final_url}"
        return result

    availability = _first(_AVAILABILITY_RE, page_html)
    if not availability:
        if "queue-it" in page_html.lower():
            result["status"] = "queue"
            result["reason"] = "頁面為 Queue-it 排隊頁"
        else:
            result["reason"] = f"頁面缺少 availability 標記（len={len(page_html)}）"
        return result

    stock_label = _first(_STOCK_LABEL_RE, page_html)
    price = _first(_PRICE_RE, page_html)
    result.update({
        "title": _first(_TITLE_RE, page_html),
        "price": f"¥{price}" if price else "",
        "limit": _first(_LIMIT_RE, page_html),
        "button": _first(_BUTTON_RE, page_html),
    })

    if availability == "true" and stock_label != "品切れ":
        result["status"] = "in_stock"
    else:
        result["status"] = "sold_out"
        if availability == "true":
            log.warning(f"{product['label']}：availability=true 但顯示「{stock_label}」，視為售完")
    return result


def fetch_product(product: dict) -> dict:
    sess = _session_for(product["id"])
    url = _product_url(product["id"])
    last_reason = ""

    for attempt in range(1, 4):
        try:
            resp = sess.get(url, timeout=(5, 12), allow_redirects=True)

            if resp.status_code in (429, 500, 502, 503, 504):
                last_reason = f"HTTP {resp.status_code}"
                if attempt < 3:
                    retry_after = resp.headers.get("Retry-After")
                    wait = int(retry_after) if (retry_after and retry_after.isdigit()) else attempt + random.uniform(0, 1)
                    log.warning(f"{product['label']}：HTTP {resp.status_code} ({attempt}/3)，{wait:.1f} 秒後重試")
                    time.sleep(min(wait, 10))
                    continue
                break

            if resp.status_code != 200 and "queue-it" not in urlparse(resp.url).netloc:
                last_reason = f"HTTP {resp.status_code}（{resp.url}）"
                break

            return parse_product_page(product, resp.text, resp.url)

        except (requests.ConnectTimeout, requests.ReadTimeout, requests.ConnectionError) as e:
            last_reason = f"{type(e).__name__}"
            if attempt < 3:
                wait = attempt + random.uniform(0, 1)
                log.warning(f"{product['label']}：連線失敗 ({attempt}/3)：{last_reason}，{wait:.1f} 秒後重試")
                time.sleep(wait)

    return {
        "id": product["id"],
        "label": product["label"],
        "url": url,
        "status": "unknown",
        "reason": last_reason,
    }


def fetch_all() -> list:
    """平行抓取所有監控商品頁（每個商品獨立 Session）。"""
    with ThreadPoolExecutor(max_workers=len(WATCH_PRODUCTS)) as executor:
        results = list(executor.map(fetch_product, WATCH_PRODUCTS))

    for r in results:
        if r["status"] == "in_stock":
            log.info(f"✅ 有庫存 → {r['label']} | {r['price']} | {r['limit']} | {r['button']}")
        elif r["status"] == "sold_out":
            log.info(f"品切れ → {r['label']}")
        elif r["status"] == "queue":
            log.warning(f"排隊中 → {r['label']} | {r['reason']}")
        else:
            log.warning(f"無法判斷 → {r['label']} | {r['reason']}")
    return results


# ─────────────────────────────────────────────
# Email 通知
# ─────────────────────────────────────────────


def _send_email(to: list, subject: str, body: str) -> bool:
    """底層 SMTP 發送，to 為收件人清單。"""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_SENDER
        msg["To"] = ", ".join(to)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, to, msg.as_string())
        log.info(f"Email 發送成功 → {[_mask_email(r) for r in to]}")
        return True
    except Exception as e:
        log.error(f"Email 發送失敗：{e}")
        return False


def notify_restock(products: list, reminder_ids: set) -> bool:
    """
    補貨廣播：寄給全體 GMAIL_RECIPIENTS。
    reminder_ids：已通知過、冷卻到期仍有庫存的商品（標示為每小時提醒）。
    """
    count = len(products)
    test_tag = "【測試】" if TEST_MODE else ""
    headline = products[0]["label"] if count == 1 else f"{products[0]['label']} 等 {count} 件"
    kind = "仍有庫存" if all(p["id"] in reminder_ids for p in products) else "補貨了！"
    subject = f"{test_tag}【寶可夢中心 {kind}】{headline}"

    lines = [
        f"Pokémon Center Online 偵測到 {count} 件監控商品有庫存",
        f"偵測時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
    ]
    if TEST_MODE:
        lines.append("⚠ 本封為測試信件，請勿當作正式補貨通知")
    lines += ["", "=" * 50]

    for i, p in enumerate(products, 1):
        lines.append(f"\n【商品 {i}】{p['label']}")
        lines.append(f"通知類型：{'⏰ 仍有庫存（每小時提醒）' if p['id'] in reminder_ids else '🆕 剛補貨'}")
        lines.append(f"商品名：{p['title']}")
        lines.append(f"價格：{p['price']}（税込）")
        if p["limit"]:
            lines.append(f"限購：{p['limit']}")
        if p["button"]:
            lines.append(f"頁面按鈕：{p['button']}")
        lines.append(f"商品連結：{p['url']}")
        lines.append("-" * 40)

    return _send_email(GMAIL_RECIPIENTS, subject, "\n".join(lines))


def notify_queue(products: list) -> bool:
    """Queue-it 排隊提醒：排隊期間無法判斷庫存，通常代表大量補貨或新品發售。"""
    test_tag = "【測試】" if TEST_MODE else ""
    subject = f"{test_tag}【寶可夢中心 排隊中】官網啟用排隊，可能正在補貨"
    lines = [
        "Pokémon Center Online 目前啟用 Queue-it 排隊，偵測器無法判斷庫存。",
        "這通常代表大量補貨或新品發售，建議手動進入排隊確認：",
        f"偵測時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
        "",
    ]
    for p in products:
        lines.append(f"▸ {p['label']}：{p['url']}")
    return _send_email(GMAIL_RECIPIENTS, subject, "\n".join(lines))


def send_test_email() -> bool:
    """測試信：確認 Gmail 帳密與收件人設定正確（不抓取商品頁）。"""
    subject = "【寶可夢中心 測試信】Gmail 通知設定正常"
    lines = [
        "這是 Pokémon Center 補貨偵測器的測試信，收到代表 Gmail 寄信設定正常。",
        f"寄送時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
        "",
        f"監控商品（{len(WATCH_PRODUCTS)} 件）：",
    ]
    for p in WATCH_PRODUCTS:
        lines.append(f"▸ {p['label']}：{_product_url(p['id'])}")
    return _send_email(GMAIL_RECIPIENTS, subject, "\n".join(lines))


# ─────────────────────────────────────────────
# 核心偵測邏輯
# ─────────────────────────────────────────────


def check_once() -> bool:
    """回傳 False 代表本輪所有商品都無法判斷（連線失敗或被攔截）。"""
    results = fetch_all()

    now = datetime.now(TW_TZ)
    cutoff = now - NOTIFY_COOLDOWN
    notified = load_state()

    # 確認售完 → 清除通知紀錄（下次補貨視為新補貨、立即通知）
    # 無法判斷 / 排隊中 → 保留紀錄，避免誤判造成重複通知
    for r in results:
        if r["status"] == "sold_out" and r["id"] in notified:
            log.info(f"{r['label']} 已售完，重置通知紀錄")
            notified.pop(r["id"])

    in_stock = [r for r in results if r["status"] == "in_stock"]
    to_notify = [
        r for r in in_stock
        if r["id"] not in notified or _parse_ts(notified[r["id"]]) < cutoff
    ]
    if to_notify:
        reminder_ids = {r["id"] for r in to_notify if r["id"] in notified}
        log.info(
            f"發送補貨通知：{len(to_notify)} 件"
            f"（有庫存 {len(in_stock)} 件，跳過 {len(in_stock) - len(to_notify)} 件冷卻中）"
        )
        if notify_restock(to_notify, reminder_ids):
            for r in to_notify:
                notified[r["id"]] = now.isoformat()
    elif in_stock:
        log.info(f"有庫存的 {len(in_stock)} 件商品均在 1 小時冷卻期內")

    queued = [r for r in results if r["status"] == "queue"]
    if queued:
        if _QUEUE_KEY not in notified or _parse_ts(notified[_QUEUE_KEY]) < cutoff:
            if notify_queue(queued):
                notified[_QUEUE_KEY] = now.isoformat()
    else:
        notified.pop(_QUEUE_KEY, None)

    save_state(notified)
    return any(r["status"] != "unknown" for r in results)


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────


def main():
    if SEND_TEST_EMAIL:
        log.info("寄送測試信（不執行偵測）")
        raise SystemExit(0 if send_test_email() else 1)

    log.info(f"Pokémon Center 補貨偵測器 | 輪數：{CHECK_ROUNDS} | 監控商品：{len(WATCH_PRODUCTS)} 件")
    consecutive_failures = 0
    for round_num in range(1, CHECK_ROUNDS + 1):
        try:
            if CHECK_ROUNDS > 1:
                log.info(f"── 第 {round_num}/{CHECK_ROUNDS} 輪 ──")
            success = check_once()
            if success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                log.warning(f"連續偵測失敗 {consecutive_failures}/3")
                if consecutive_failures >= 3:
                    log.error(
                        "Pokémon Center 連續 3 輪無法判斷庫存，判定目前 GitHub runner 網路異常或遭攔截，"
                        "提早結束本次 Job，交由下次排程取得新 runner"
                    )
                    break
        except Exception as error:
            log.exception(f"第 {round_num}/{CHECK_ROUNDS} 輪發生非預期錯誤：{error}")
        if round_num < CHECK_ROUNDS:
            wait = random.randint(4, 6)
            log.info(f"等待 {wait} 秒後進行下一輪...")
            time.sleep(wait)
    log.info("所有輪次完成")


if __name__ == "__main__":
    main()
