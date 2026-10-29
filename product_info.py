"""
商品資訊更新：抓取商品頁，把日文品名、價格、限購、圖片與當下庫存寫回 watch_config.json
- UI 網頁新增商品後會觸發 product_info.yml 執行本程式（瀏覽器受 CORS 限制無法直接抓官網）
- 預設補齊所有缺日文品名的商品；指定商品編號或 --all 時重新抓取
- 只更新設定檔中已存在的商品，不會新增或刪除商品、不會動到通知對象
"""

import argparse
import json
import re
from datetime import datetime

import pokemon_center_monitor as pcm

log = pcm.log

_URL_ID_RE = re.compile(r"/(\d{6,16})\.html")
_BARE_ID_RE = re.compile(r"\d{6,16}")


def extract_ids(text: str) -> list[str]:
    """從商品頁網址或純編號（逗號／空白分隔）取出商品 id。"""
    ids = []
    for token in re.split(r"[\s,，、]+", text.strip()):
        m = _URL_ID_RE.search(token)
        if m:
            ids.append(m.group(1))
        elif _BARE_ID_RE.fullmatch(token):
            ids.append(token)
        elif token:
            log.warning(f"看不懂的商品編號，略過：{token}")
    return list(dict.fromkeys(ids))


def _error_text(result: dict) -> str:
    if result["status"] == "queue":
        return "官網排隊中，稍後再重新抓取"
    if result["reason"].startswith("HTTP 404"):
        return "找不到商品頁（HTTP 404），請確認商品編號"
    return result["reason"] or "無法解析商品頁"


def update_info(config: dict, ids: list[str], refresh_all: bool = False) -> int:
    """更新 config 內商品資訊，回傳成功更新的件數。"""
    products = config.get("products", [])
    by_id = {p["id"]: p for p in products}
    for pid in ids:
        if pid not in by_id:
            log.warning(f"{pid} 不在 watch_config.json，略過（請先在 UI 新增）")

    targets = [
        p for p in products
        if refresh_all or p["id"] in ids or not p.get("title_ja")
    ]
    if not targets:
        log.info("沒有需要更新資訊的商品")
        return 0

    log.info(f"抓取 {len(targets)} 件商品資訊")
    results = pcm.fetch_all([{"id": p["id"], "label": p.get("label") or p["id"]} for p in targets])
    now = datetime.now(pcm.TW_TZ).isoformat(timespec="seconds")
    updated = 0
    for p, r in zip(targets, results):
        p["info_updated"] = now
        if r["status"] in ("in_stock", "sold_out"):
            p.update({
                "title_ja": r["title"],
                "price": r["price"],
                "limit": r["limit"],
                "image": r["image"],
                "stock": r["status"],
            })
            p.pop("info_error", None)
            updated += 1
        else:
            p["info_error"] = _error_text(r)
            log.warning(f"{p['id']}：{p['info_error']}")
    return updated


def main():
    parser = argparse.ArgumentParser(description="抓取商品頁資訊並寫回 watch_config.json")
    parser.add_argument("ids", nargs="*", help="商品編號或商品頁網址（可多個）")
    parser.add_argument("--all", action="store_true", help="重新抓取全部商品")
    args = parser.parse_args()

    config = json.loads(pcm.CONFIG_FILE.read_text(encoding="utf-8"))
    updated = update_info(config, extract_ids(" ".join(args.ids)), args.all)
    pcm.CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info(f"完成，成功更新 {updated} 件")


if __name__ == "__main__":
    main()
