"""
pokemoncenter-online.com 補貨偵測器
- 設定：監控商品、每位通知對象（Recipient_N）的商品清單與監控參數存於 watch_config.json，由 docs/ 的 UI 網頁編輯
- 偵測：requests 平行抓取商品頁，解析 #availability 與「品切れ」標記，每輪約 1 秒
- 通知：依 Recipient_N 分別寄信，補貨立即通知、仍有庫存每隔冷卻時間（預設 1 小時）提醒一次；確認售完後重置，下次補貨再立即通知
- 排隊：偵測到 Queue-it 排隊頁（無法判斷庫存）時另發提醒信，同樣套用冷卻時間
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

STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_products.json"))
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "watch_config.json"))
CONFIG_RELOAD_ROUNDS = int(os.environ.get("CONFIG_RELOAD_ROUNDS", "10"))
TEST_MODE = os.environ.get("TEST_MODE", "").strip() not in ("", "0", "false")
SEND_TEST_EMAIL = os.environ.get("SEND_TEST_EMAIL", "").strip() not in ("", "0", "false")

TW_TZ = timezone(timedelta(hours=8))  # 台灣時區 UTC+8

GMAIL_SENDER = os.environ.get("GMAIL_SENDER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")
# 收件人在 watch_config.json / UI 網頁只用代稱 Recipient_N（頁面公開，不放信箱）。
# Recipient_N 的信箱：Secret RECIPIENT_N（UI 新增收件人時寫入）優先，否則用 GMAIL_RECIPIENTS 第 N 個（舊設定）
GMAIL_RECIPIENTS = [
    addr.strip()
    for addr in os.environ.get("GMAIL_RECIPIENTS", "").split(",")
    if addr.strip()
]

# ── 監控設定（啟動時由 load_config() 從 watch_config.json 載入）──────────
WATCH_PRODUCTS: list[dict] = []                 # 商品庫：{"id": 13 碼 JAN, "label": 中文名稱, ...}
RECIPIENT_PRODUCTS: dict[str, list[str]] = {}   # Recipient_N → 要通知的商品 id

_RECIPIENT_RE = re.compile(r"Recipient_([1-9]\d*)")
_RECIPIENT_SECRET_RE = re.compile(r"RECIPIENT_([1-9]\d*)")

# ── 監控參數（watch_config.json 的 settings，可在 UI 調整）──────────
DEFAULT_SETTINGS = {
    "enabled": True,          # False：run 啟動後直接結束（暫停監控）
    "check_rounds": 50,       # 每次 run 的偵測輪數（環境變數 CHECK_ROUNDS 可覆寫，供手動執行）
    "interval_min": 4,        # 每輪間隔秒數，在 min～max 之間隨機
    "interval_max": 6,
    "cooldown_minutes": 60,   # 仍有庫存／排隊中時多久再提醒一次
    "queue_alert": True,      # 官網啟用 Queue-it 排隊時是否寄提醒信
}
_SETTING_RANGES = {
    "check_rounds": (1, 150),
    "interval_min": (2, 60),
    "interval_max": (2, 60),
    "cooldown_minutes": (5, 1440),
}
# Actions job timeout-minutes 為 15，扣掉環境準備，一次 run 最多跑約 13 分鐘
MAX_RUN_SECONDS = 13 * 60
SETTINGS: dict = dict(DEFAULT_SETTINGS)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# Queue-it 排隊時的狀態 key（存放於各 Recipient 的 notified，與商品 id 共用冷卻機制）
_QUEUE_KEY = "__queue__"

# 同時抓取的商品頁上限，避免商品一多就瞬間打出大量請求
_MAX_WORKERS = 12

# ── 商品頁解析（Salesforce Commerce Cloud 商品頁）──────────
# 有庫存：<input id="availability" value="true">，無「品切れ」標籤，按鈕 class 無 default
# 售完  ：<input id="availability" value="false">，<p class="stock"><span>品切れ</span></p>
_AVAILABILITY_RE = re.compile(r'id="availability"\s+value="(true|false)"')
_STOCK_LABEL_RE = re.compile(r'<p class="stock">\s*<span>([^<]*)</span>')
_TITLE_RE = re.compile(r'<h1 class="lead">([^<]*)')
_PRICE_RE = re.compile(r'<p class="price[^"]*">\s*<span class="txt">([^<]*)')
_LIMIT_RE = re.compile(r'<p class="limit">([^<]*)')
_BUTTON_RE = re.compile(r'<div class="add-to-cart-button[^"]*"><a href="">([^<]*)</a>')
_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]*)"')

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


def _load_recipient_secrets() -> dict[int, str]:
    """
    UI 網頁新增的收件人存成 GitHub Secret RECIPIENT_N（瀏覽器加密後寫入，無法讀回）。
    workflow 逐一以環境變數 RECIPIENT_1～RECIPIENT_10 傳入（沒建立的是空字串）；本機執行同樣設環境變數。
    """
    found = {}
    for key, value in os.environ.items():
        m = _RECIPIENT_SECRET_RE.fullmatch(key)
        if m and value.strip():
            found[int(m.group(1))] = value.strip()
    return found


RECIPIENT_EMAILS = _load_recipient_secrets()


def _alias_num(alias: str) -> int:
    return int(_RECIPIENT_RE.fullmatch(alias).group(1))


def recipient_email(alias: str) -> str | None:
    """Recipient_N → Secret RECIPIENT_N，沒有則用 GMAIL_RECIPIENTS 第 N 個；都沒有回傳 None。"""
    n = _alias_num(alias)
    if n in RECIPIENT_EMAILS:
        return RECIPIENT_EMAILS[n]
    return GMAIL_RECIPIENTS[n - 1] if n <= len(GMAIL_RECIPIENTS) else None


def known_recipient_numbers() -> list[int]:
    """有設定信箱的 N（Secret RECIPIENT_N 或 GMAIL_RECIPIENTS 第 N 個）。"""
    return sorted(set(RECIPIENT_EMAILS) | set(range(1, len(GMAIL_RECIPIENTS) + 1)))


def _email_source(n: int) -> str:
    return f"RECIPIENT_{n}" if n in RECIPIENT_EMAILS else f"GMAIL_RECIPIENTS 第 {n} 個"


# ─────────────────────────────────────────────
# 監控設定（watch_config.json）
# ─────────────────────────────────────────────


def parse_config(data: dict) -> tuple[list, dict]:
    """
    watch_config.json 格式：
      {"products": [{"id", "label", "title_ja", "price", ...}],
       "recipients": {"Recipient_1": [商品id, ...], ...}}
    回傳 (商品庫, {Recipient_N: 商品id清單})，略過重複商品、格式錯誤的代稱與不在商品庫的 id。
    """
    products, seen = [], set()
    for p in data.get("products", []):
        pid = str(p.get("id", "")).strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        label = (p.get("label") or p.get("title_ja") or pid).strip()
        products.append({**p, "id": pid, "label": label})

    routes = {}
    for alias, ids in data.get("recipients", {}).items():
        if not _RECIPIENT_RE.fullmatch(alias):
            log.warning(f"略過格式錯誤的通知對象代稱：{alias}（應為 Recipient_N）")
            continue
        routes[alias] = [pid for pid in dict.fromkeys(str(i) for i in ids) if pid in seen]
    return products, dict(sorted(routes.items(), key=lambda kv: _alias_num(kv[0])))


def parse_settings(data: dict) -> dict:
    """讀取 settings，缺少或超出範圍的值改用預設值／範圍邊界。"""
    raw = data.get("settings") or {}
    settings = dict(DEFAULT_SETTINGS)
    for key in ("enabled", "queue_alert"):
        if isinstance(raw.get(key), bool):
            settings[key] = raw[key]
    for key, (lo, hi) in _SETTING_RANGES.items():
        try:
            settings[key] = min(max(int(raw.get(key, settings[key])), lo), hi)
        except (TypeError, ValueError):
            log.warning(f"settings.{key} 不是數字，使用預設值 {settings[key]}")
    settings["interval_max"] = max(settings["interval_max"], settings["interval_min"])
    return settings


def apply_config(data: dict):
    global WATCH_PRODUCTS, RECIPIENT_PRODUCTS, SETTINGS
    WATCH_PRODUCTS, RECIPIENT_PRODUCTS = parse_config(data)
    SETTINGS = parse_settings(data)


def load_config():
    apply_config(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))


def _cooldown_text() -> str:
    minutes = SETTINGS["cooldown_minutes"]
    return f"{minutes // 60} 小時" if minutes % 60 == 0 else f"{minutes} 分鐘"


def _config_summary() -> str:
    parts = [f"{alias} {len(ids)} 件" for alias, ids in RECIPIENT_PRODUCTS.items()]
    s = SETTINGS
    return (
        f"商品庫 {len(WATCH_PRODUCTS)} 件｜" + ("、".join(parts) or "尚無通知對象")
        + f"｜{'監控中' if s['enabled'] else '已暫停'}・每次 {s['check_rounds']} 輪"
        + f"・間隔 {s['interval_min']}~{s['interval_max']} 秒・提醒冷卻 {_cooldown_text()}"
        + f"・排隊提醒{'開' if s['queue_alert'] else '關'}"
    )


def run_rounds() -> int:
    """本次 run 的輪數：環境變數 CHECK_ROUNDS（手動執行時填）優先，否則用 UI 設定；超過 job 時限會自動減少。"""
    env = os.environ.get("CHECK_ROUNDS", "").strip()
    rounds = int(env) if env else SETTINGS["check_rounds"]
    limit = max(1, MAX_RUN_SECONDS // (SETTINGS["interval_max"] + 2))  # 每輪抓取約 1 秒，多留 1 秒餘裕
    if rounds > limit:
        log.warning(f"{rounds} 輪 × 最長間隔 {SETTINGS['interval_max']} 秒會超過 job 時限，改為 {limit} 輪")
        rounds = limit
    return rounds


def reload_remote_config():
    """
    Actions 中每隔 CONFIG_RELOAD_ROUNDS 輪從 GitHub 讀取最新 watch_config.json，
    UI 網頁修改後約 1 分鐘內生效，不用等下一次 run。讀取失敗時沿用目前設定。
    """
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        return
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/contents/{CONFIG_FILE.name}",
            params={"ref": os.environ.get("GITHUB_REF_NAME", "main")},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning(f"重新讀取監控設定失敗，沿用目前設定：{e}")
        return

    before = (WATCH_PRODUCTS, RECIPIENT_PRODUCTS, SETTINGS)
    apply_config(data)
    if (WATCH_PRODUCTS, RECIPIENT_PRODUCTS, SETTINGS) != before:
        log.info(f"監控設定已更新：{_config_summary()}")
        warn_missing_recipients()


def active_routes() -> dict[str, list[str]]:
    """有對應信箱的通知對象 → 商品 id。"""
    return {alias: ids for alias, ids in RECIPIENT_PRODUCTS.items() if recipient_email(alias)}


def warn_missing_recipients():
    for alias, ids in RECIPIENT_PRODUCTS.items():
        n = _alias_num(alias)
        if ids and not recipient_email(alias):
            log.warning(
                f"{alias} 找不到信箱（沒有 Secret RECIPIENT_{n}，GMAIL_RECIPIENTS 也只有 {len(GMAIL_RECIPIENTS)} 個），"
                f"它的 {len(ids)} 件商品不會通知；請在 UI 網頁設定 {alias} 的信箱"
            )


# ─────────────────────────────────────────────
# 狀態管理（1 小時冷卻）
# ─────────────────────────────────────────────


def load_state() -> dict:
    """
    回傳 notified：{Recipient_N: {商品id 或 __queue__: ISO時間戳}}  ← 1 小時通知冷卻
    確認售完時會移除該商品，下次補貨即視為新補貨、立即通知。
    """
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        # 舊版格式 {商品id: 時間戳}（全體廣播）無法對應到個別對象，直接捨棄
        return {k: v for k, v in data.get("notified", {}).items() if isinstance(v, dict)}
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
        "image": "",
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
        "image": _first(_IMAGE_RE, page_html),
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


def fetch_all(products: list) -> list:
    """平行抓取商品頁（每個商品獨立 Session）。"""
    with ThreadPoolExecutor(max_workers=min(len(products), _MAX_WORKERS)) as executor:
        results = list(executor.map(fetch_product, products))

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


def notify_restock(alias: str, products: list, reminder_ids: set) -> bool:
    """
    補貨通知：寄給 alias 對應的信箱，只列出該對象負責的商品。
    reminder_ids：已通知過、冷卻到期仍有庫存的商品（標示為每小時提醒）。
    """
    count = len(products)
    test_tag = "【測試】" if TEST_MODE else ""
    headline = products[0]["label"] if count == 1 else f"{products[0]['label']} 等 {count} 件"
    kind = "仍有庫存" if all(p["id"] in reminder_ids for p in products) else "補貨了！"
    subject = f"{test_tag}【寶可夢中心 {kind}】{headline}"

    lines = [
        f"Pokémon Center Online 偵測到 {count} 件監控商品有庫存",
        f"通知對象：{alias}",
        f"偵測時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
    ]
    if TEST_MODE:
        lines.append("⚠ 本封為測試信件，請勿當作正式補貨通知")
    lines += ["", "=" * 50]

    for i, p in enumerate(products, 1):
        lines.append(f"\n【商品 {i}】{p['label']}")
        lines.append(f"通知類型：{f'⏰ 仍有庫存（每 {_cooldown_text()}提醒）' if p['id'] in reminder_ids else '🆕 剛補貨'}")
        lines.append(f"商品名：{p['title']}")
        lines.append(f"價格：{p['price']}（税込）")
        if p["limit"]:
            lines.append(f"限購：{p['limit']}")
        if p["button"]:
            lines.append(f"頁面按鈕：{p['button']}")
        lines.append(f"商品連結：{p['url']}")
        lines.append("-" * 40)

    return _send_email([recipient_email(alias)], subject, "\n".join(lines))


def notify_queue(alias: str, products: list) -> bool:
    """Queue-it 排隊提醒：排隊期間無法判斷庫存，通常代表大量補貨或新品發售。"""
    test_tag = "【測試】" if TEST_MODE else ""
    subject = f"{test_tag}【寶可夢中心 排隊中】官網啟用排隊，可能正在補貨"
    lines = [
        "Pokémon Center Online 目前啟用 Queue-it 排隊，偵測器無法判斷庫存。",
        "這通常代表大量補貨或新品發售，建議手動進入排隊確認：",
        f"通知對象：{alias}",
        f"偵測時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
        "",
    ]
    for p in products:
        lines.append(f"▸ {p['label']}：{p['url']}")
    return _send_email([recipient_email(alias)], subject, "\n".join(lines))


def send_test_email() -> bool:
    """測試信：每個有設定的信箱各寄一封，列出它對應的 Recipient_N 與商品（不抓取商品頁）。"""
    labels = {p["id"]: p["label"] for p in WATCH_PRODUCTS}
    all_ok = True
    for n in known_recipient_numbers():
        alias = f"Recipient_{n}"
        email = recipient_email(alias)
        ids = RECIPIENT_PRODUCTS.get(alias, [])
        lines = [
            "這是 Pokémon Center 補貨偵測器的測試信，收到代表 Gmail 寄信設定正常。",
            f"你的通知代稱：{alias}（信箱來源：{_email_source(n)}）",
            f"寄送時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}（台灣時間）",
            "",
            f"會通知你的商品（{len(ids)} 件）：" if ids else "目前沒有分配給你的商品，補貨時不會收到通知。",
        ]
        for pid in ids:
            lines.append(f"▸ {labels[pid]}：{_product_url(pid)}")
        subject = f"【寶可夢中心 測試信】{alias} 通知設定正常"
        all_ok = _send_email([email], subject, "\n".join(lines)) and all_ok
    warn_missing_recipients()
    return all_ok


# ─────────────────────────────────────────────
# 核心偵測邏輯
# ─────────────────────────────────────────────


def check_once() -> bool:
    """回傳 False 代表本輪所有商品都無法判斷（連線失敗或被攔截）。"""
    routes = active_routes()
    watched_ids = {pid for ids in routes.values() for pid in ids}
    products = [p for p in WATCH_PRODUCTS if p["id"] in watched_ids]
    if not products:
        log.info("沒有分配給通知對象的商品，本輪略過")
        return True

    results = fetch_all(products)
    by_id = {r["id"]: r for r in results}

    now = datetime.now(TW_TZ)
    cutoff = now - timedelta(minutes=SETTINGS["cooldown_minutes"])
    # 只保留目前設定中的對象與商品；從某人清單移除再加回 → 視為新分配，有庫存會立即通知
    notified = {
        alias: {k: ts for k, ts in rec.items() if k == _QUEUE_KEY or k in routes[alias]}
        for alias, rec in load_state().items()
        if alias in routes
    }

    # 確認售完 → 清除通知紀錄（下次補貨視為新補貨、立即通知）
    # 無法判斷 / 排隊中 → 保留紀錄，避免誤判造成重複通知
    for r in results:
        if r["status"] == "sold_out" and any(r["id"] in rec for rec in notified.values()):
            log.info(f"{r['label']} 已售完，重置通知紀錄")
            for rec in notified.values():
                rec.pop(r["id"], None)

    for alias, ids in routes.items():
        rec = notified.setdefault(alias, {})
        mine = [by_id[pid] for pid in ids]

        in_stock = [r for r in mine if r["status"] == "in_stock"]
        to_notify = [
            r for r in in_stock
            if r["id"] not in rec or _parse_ts(rec[r["id"]]) < cutoff
        ]
        if to_notify:
            reminder_ids = {r["id"] for r in to_notify if r["id"] in rec}
            log.info(
                f"{alias}：發送補貨通知 {len(to_notify)} 件"
                f"（有庫存 {len(in_stock)} 件，跳過 {len(in_stock) - len(to_notify)} 件冷卻中）"
            )
            if notify_restock(alias, to_notify, reminder_ids):
                for r in to_notify:
                    rec[r["id"]] = now.isoformat()
        elif in_stock:
            log.info(f"{alias}：有庫存的 {len(in_stock)} 件商品均在 {_cooldown_text()}冷卻期內")

        queued = [r for r in mine if r["status"] == "queue"]
        if not queued:
            rec.pop(_QUEUE_KEY, None)
        elif SETTINGS["queue_alert"] and (_QUEUE_KEY not in rec or _parse_ts(rec[_QUEUE_KEY]) < cutoff):
            if notify_queue(alias, queued):
                rec[_QUEUE_KEY] = now.isoformat()

    save_state({alias: rec for alias, rec in notified.items() if rec})
    return any(r["status"] != "unknown" for r in results)


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────


def main():
    missing = [k for k in ("GMAIL_SENDER", "GMAIL_PASSWORD") if not os.environ.get(k, "").strip()]
    if missing:
        raise SystemExit(f"缺少環境變數：{', '.join(missing)}")
    if not known_recipient_numbers():
        raise SystemExit("沒有任何收件人：請在 UI 網頁新增收件人，或設定 GMAIL_RECIPIENTS")
    load_config()
    log.info("收件人信箱來源：" + "、".join(f"Recipient_{n}＝{_email_source(n)}" for n in known_recipient_numbers()))

    if SEND_TEST_EMAIL:
        log.info("寄送測試信（不執行偵測）")
        raise SystemExit(0 if send_test_email() else 1)

    log.info(f"Pokémon Center 補貨偵測器 | {_config_summary()}")
    if not SETTINGS["enabled"]:
        log.info("監控已在 UI 暫停，本次不執行")
        return
    warn_missing_recipients()
    rounds = run_rounds()
    consecutive_failures = 0
    for round_num in range(1, rounds + 1):
        try:
            if rounds > 1:
                log.info(f"── 第 {round_num}/{rounds} 輪 ──")
            if round_num > 1 and (round_num - 1) % CONFIG_RELOAD_ROUNDS == 0:
                reload_remote_config()
                if not SETTINGS["enabled"]:
                    log.info("監控已在 UI 暫停，提早結束本次 run")
                    break
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
            log.exception(f"第 {round_num}/{rounds} 輪發生非預期錯誤：{error}")
        if round_num < rounds:
            wait = random.uniform(SETTINGS["interval_min"], SETTINGS["interval_max"])
            log.info(f"等待 {wait:.1f} 秒後進行下一輪...")
            time.sleep(wait)
    log.info("所有輪次完成")


if __name__ == "__main__":
    main()
