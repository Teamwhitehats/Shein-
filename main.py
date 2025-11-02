#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHEIN Stock Broadcaster (Ultra-Direct Mode v2)

Key upgrades:
- We ALWAYS send the Stock Update message when totalResults increases.
- We THEN try serviceability checks in parallel.
- For each deliverable product (deliverable==True), we send product alerts instantly.
- If serviceability 403s, you will still get the Stock Update wave message so you see the spike in real time.
- Stronger serviceability request headers to reduce 403.
- Still no queues, no batching, rotate bot tokens per message.

Still obeys:
- No spam if stock is same.
- Snapshot replace model so we don't repeat old wave.
"""

import json
import time
import threading
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import traceback

# ================== CONFIG ==================
BASE_URL = "https://www.sheinindia.in"

# NOTE: change genderfilter%3AMen -> genderfilter%3AWomen if you want Women's feed.
API_BASE = (
    "https://www.sheinindia.in/api/category/sverse-5939-37961"
    "?fields=SITE"
    "&currentPage={page}"
    "&pageSize={page_size}"
    "&format=json"
    "&query=%3Arelevance%3Agenderfilter%3AMen"
    "&sortBy=relevance"
    "&gridColumns=2"
    "&customerType=Existing"
    "&facets=genderfilter%3AMen"
    "&includeUnratedProducts=false"
    "&segmentIds=15%2C8%2C19"
    "&customertype=Existing"
    "&advfilter=true"
    "&platform=Msite"
    "&showAdsOnNextPage=false"
    "&is_ads_enable_plp=true"
    "&displayRatings=true"
    "&&store=shein"
)

DEFAULT_PAGE_SIZE = 40
PRODUCT_POLL_INTERVAL_SEC = 7
REQUEST_TIMEOUT = 20

MAX_PAGE_FETCH_WORKERS = 12          # concurrent PLP page grabs
SVC_MAX_WORKERS = 20                 # concurrent serviceability checks (your cap)

TELEGRAM_CHANNEL_ID = "-1003288025081"
REAL_PIN = "813210"
MASKED_PIN = "8****0"

BOT_TOKENS = [
 
  "8481339746:AAG7YbAkT6L-7CbJHKQtexbd2Gw4q19dtsg",  
    
  "8261375157:AAEIluYvSlQSWyf-6kcWVGON1Mic-AQ0Cjs", 
 
  "8325522235:AAEaEe2TgQnlypWfPx4CB1SFGh3s62DvBys",  

  "8258046790:AAH89YAGdpHXf4K6gd3Vzvb_m6TFMl7qVL0",  

  "8130043152:AAG78e6CY6GmXPoPVQT1YEyNXIJnF94cANs",  
    
]

TG_RETRY_LIMIT = 3
TG_RETRY_SLEEP_SEC = 0.4

DB_FILE = "db_sheinnn.json"

# ================== GLOBAL STATE ======================
db_lock = threading.Lock()
db_cache = {}
rr_index_lock = threading.Lock()
rr_index = 0
startup_message_sent = False

# ================== LOGGING ===========================
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{ts}] {msg}", flush=True)

def log_exc(prefix="EXCEPTION"):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{ts}] {prefix}:", flush=True)
    traceback.print_exc()
    print("-----", flush=True)

# ================== DB HELPERS ========================
# db.json structure:
# {
#   "meta": { "last_total_results": 0 },
#   "prev_deliverable": ["443327217017", "443320087012", ...]
# }

def db_load():
    if not os.path.exists(DB_FILE):
        return { "meta": { "last_total_results": 0 }, "prev_deliverable": [] }
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log_exc("ERROR loading db.json, resetting")
        return { "meta": { "last_total_results": 0 }, "prev_deliverable": [] }

def db_save(db):
    try:
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False)
        os.replace(tmp, DB_FILE)
    except Exception:
        log_exc("ERROR saving db.json")

def db_get_last_total_results(db):
    return int(db.get("meta", {}).get("last_total_results", 0))

def db_set_last_total_results(db, val):
    db.setdefault("meta", {})["last_total_results"] = int(val)

def db_get_prev_deliverable_set(db):
    return set(db.get("prev_deliverable", []))

def db_replace_prev_deliverable_set(db, codes_set):
    db["prev_deliverable"] = sorted(list(codes_set))

# ================== TELEGRAM DIRECT SEND ==============
def round_robin_token():
    global rr_index
    with rr_index_lock:
        token = BOT_TOKENS[rr_index % len(BOT_TOKENS)]
        rr_index += 1
        return token

def telegram_send_text(text):
    bot_token = round_robin_token()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }

    for attempt in range(TG_RETRY_LIMIT):
        try:
            r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return True
            log(f"[TELEGRAM] sendMessage non-200={r.status_code}, attempt {attempt+1}")
        except Exception:
            log_exc("TELEGRAM sendMessage exception")
        time.sleep(TG_RETRY_SLEEP_SEC)

    log("[TELEGRAM] sendMessage FAILED all retries")
    return False

def telegram_send_photo(photo_url, caption):
    bot_token = round_robin_token()
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }

    for attempt in range(TG_RETRY_LIMIT):
        try:
            r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return True
            log(f"[TELEGRAM] sendPhoto non-200={r.status_code}, attempt {attempt+1}")
        except Exception:
            log_exc("TELEGRAM sendPhoto exception")
        time.sleep(TG_RETRY_SLEEP_SEC)

    log("[TELEGRAM] sendPhoto FAILED all retries")
    return False

# ================== SHEIN FETCH =======================
def parse_json_from_response_text(txt: str):
    txt = txt.strip()
    try:
        return json.loads(txt)
    except Exception:
        pass
    si, ei = txt.find("{"), txt.rfind("}")
    if si == -1 or ei == -1:
        raise Exception("Could not extract JSON from PLP response")
    return json.loads(txt[si:ei+1])

def make_plp_session():
    s = requests.Session()
    s.headers.update({
        "host": "www.sheinindia.in",
        "sec-ch-ua": "\"Google Chrome\";v=\"141\", \"Not?A_Brand\";v=\"8\", \"Chromium\";v=\"141\"",
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": "\"Android\"",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-user": "?1",
        "sec-fetch-dest": "document",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "priority": "u=0, i",
    })
    return s

def plp_fetch_page(sess, page: int, page_size: int):
    url = API_BASE.format(page=page, page_size=page_size)
    r = sess.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code == 403:
        raise Exception("403 on PLP")
    r.raise_for_status()
    data = parse_json_from_response_text(r.text)
    products = data.get("products", [])
    pagination = data.get("pagination", {}) or {}
    return products, pagination

def fetch_all_products(sess):
    products0, pagination0 = plp_fetch_page(sess, 0, DEFAULT_PAGE_SIZE)

    total_results_now = int(pagination0.get("totalResults", len(products0)))
    total_pages_value = int(pagination0.get("totalPages", 0))

    if total_pages_value <= 0:
        last_page_index = max((total_results_now - 1) // DEFAULT_PAGE_SIZE, 0)
    else:
        last_page_index = total_pages_value

    all_products = list(products0)

    remaining_pages = list(range(1, last_page_index + 1))
    if remaining_pages:
        def page_worker(pg):
            try:
                plist, _ = plp_fetch_page(sess, pg, DEFAULT_PAGE_SIZE)
                return plist
            except Exception:
                log_exc(f"PLP fetch failed for page {pg}")
                return []

        with ThreadPoolExecutor(max_workers=MAX_PAGE_FETCH_WORKERS) as ex:
            futs = [ex.submit(page_worker, pg) for pg in remaining_pages]
            for fut in as_completed(futs):
                try:
                    all_products.extend(fut.result())
                except Exception:
                    log_exc("PLP future join failed")

    return all_products, total_results_now

def check_serviceability(sess, product_code: str, pincode: str):
    """
    Try to look like a real XHR to reduce 403.
    Still no cookies, honoring your "no personalization" rule.
    """
    if not product_code:
        return {"deliverable": None, "codEligible": None}

    url = (
        "https://www.sheinindia.in/api/edd/checkDeliveryDetails"
        f"?productCode={product_code}"
        f"&postalCode={pincode}"
        "&quantity=1&IsExchange=false"
    )

    headers = {
        # pretend real mobile browser XHR
        "user-agent": sess.headers.get("user-agent", ""),
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "referer": f"{BASE_URL}/",
        "origin": BASE_URL,
        "x-requested-with": "XMLHttpRequest",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        # no cookies, as per requirement
    }

    try:
        r = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except Exception:
        log_exc("Serviceability request exception")
        return {"deliverable": None, "codEligible": None}

    if r.status_code != 200:
        log(f"[SVC] non-200 {r.status_code} for product_code={product_code}")
        return {"deliverable": None, "codEligible": None}

    try:
        j = r.json()
    except Exception:
        try:
            j = parse_json_from_response_text(r.text)
        except Exception:
            log_exc("Serviceability parse fail")
            return {"deliverable": None, "codEligible": None}

    det = None
    if isinstance(j.get("productDetails"), list) and j["productDetails"]:
        det = j["productDetails"][0]

    deliverable = det.get("servicability") if det else j.get("servicability")
    cod_eligible = det.get("codEligible") if det else j.get("codEligible")

    return {
        "deliverable": deliverable,
        "codEligible": cod_eligible
    }

# ================== MESSAGE BUILDERS ==================
def pick_main_image(product):
    imgs = product.get("images", [])
    for im in imgs:
        if str(im.get("imageType", "")).upper() == "PRIMARY" and im.get("url"):
            return im["url"]
    fallback = product.get("fnlColorVariantData", {})
    if fallback and fallback.get("outfitPictureURL"):
        return fallback["outfitPictureURL"]
    for im in imgs:
        if im.get("url"):
            return im["url"]
    return None

def build_stock_update_caption(total_now: int, prev_total: int):
    delta = total_now - prev_total
    return (
        "📊 <b>Stock Update</b>\n"
        f"Total products now: <b>{total_now}</b> (▲ +{delta})\n"
        "🔥 New drops detected. Checking deliverable items…"
    )

def build_product_alert(product, svc_info):
    name = product.get("name", "N/A")

    price_info = product.get("price", {})
    mrp_val = (
        price_info.get("displayformattedValue")
        or price_info.get("formattedValue")
        or "N/A"
    )

    cat_txt = (
        product.get("brickNameText")
        or product.get("verticalNameText")
        or product.get("segmentNameText")
        or ""
    )

    prod_url_path = product.get("url", "") or ""
    if prod_url_path and not prod_url_path.startswith("http"):
        product_url = BASE_URL.rstrip("/") + prod_url_path
    else:
        product_url = prod_url_path or "N/A"

    if svc_info.get("deliverable") is True:
        deliver_line = "🚚 Deliverable ✅"
    elif svc_info.get("deliverable") is False:
        deliver_line = "🚚 Not Deliverable ❌"
    else:
        deliver_line = "🚚 Deliverable: ? ❓"

    cod_eligible = svc_info.get("codEligible")
    if cod_eligible is True:
        cod_line = "💵 COD: Yes"
    elif cod_eligible is False:
        cod_line = "💵 COD: No"
    else:
        cod_line = "💵 COD: ?"

    parts = [
        "🔥 <b>IN STOCK</b>",
        f"<b>{name}</b>",
        "",
        f"💰 Price: {mrp_val}",
        f"📦 Category: {cat_txt}" if cat_txt else "",
        "",
        deliver_line,
        cod_line,
        "",
        f"📍 Pincode: {MASKED_PIN}",
        "",
        f"🔗 {product_url}",
    ]
    msg = "\n".join([p for p in parts if p])
    if len(msg) > 1000:
        msg = msg[:1000] + "…"
    return msg

# ================== CORE WAVE LOGIC ===================
def handle_wave(sess, products, total_results_now, prev_total_results):
    """
    Stock went UP:
    1. Immediately save new totalResults so next 7s cycle doesn't repeat.
    2. IMMEDIATELY send Stock Update message to Telegram (even before deliverability passes).
    3. Parallel serviceability; for each deliverable product not seen before, send alert right now.
    4. Replace prev_deliverable snapshot in db with this wave's deliverables.
    """

    # 1. Mark new totalResults in db immediately
    with db_lock:
        db_set_last_total_results(db_cache, total_results_now)
        db_save(db_cache)

    log(f"[WAVE] NEW STOCK! prev_total={prev_total_results} now={total_results_now} (delta=+{total_results_now - prev_total_results})")
    log("[WAVE] Updated db.last_total_results now -> prevents duplicate wave next cycle")

    # 2. Send Stock Update message NOW (always)
    wave_header_msg = build_stock_update_caption(total_results_now, prev_total_results)
    sent_header_ok = telegram_send_text(wave_header_msg)
    log(f"[WAVE] Stock Update message sent={sent_header_ok}")

    # Load previous deliverable snapshot
    with db_lock:
        prev_deliverable_set = db_get_prev_deliverable_set(db_cache)

    session_announced = set()        # avoid dup same wave
    current_deliverable_set = set()  # new snapshot we'll persist at end

    def svc_worker(prod):
        code = prod.get("code")
        svc = check_serviceability(sess, code, REAL_PIN)
        return prod, code, svc

    with ThreadPoolExecutor(max_workers=SVC_MAX_WORKERS) as ex:
        futs = [ex.submit(svc_worker, p) for p in products]
        for fut in as_completed(futs):
            try:
                prod, code, svc = fut.result()
            except Exception:
                log_exc("svc_worker crashed in wave")
                continue

            if not code:
                continue

            # serviceability passed?
            if svc.get("deliverable") is True:
                current_deliverable_set.add(code)

                if code not in prev_deliverable_set and code not in session_announced:
                    alert_msg = build_product_alert(prod, svc)
                    img = pick_main_image(prod)

                    if img:
                        ok = telegram_send_photo(img, alert_msg)
                        log(f"[ALERT] photo '{prod.get('name','?')[:60]}' deliverable✅ sent={ok}")
                    else:
                        ok = telegram_send_text(alert_msg)
                        log(f"[ALERT] text '{prod.get('name','?')[:60]}' deliverable✅ sent={ok}")

                    session_announced.add(code)
            else:
                # either 403 or not deliverable
                if svc.get("deliverable") is None:
                    # 403 / unknown
                    log(f"[ALERT_SKIP] {code} deliverable=UNKNOWN (403/blocked). Not sending product card.")
                else:
                    # explicit not deliverable
                    log(f"[ALERT_SKIP] {code} not deliverable to {REAL_PIN}")

    # 4. Persist snapshot of deliverables we saw this wave
    with db_lock:
        db_replace_prev_deliverable_set(db_cache, current_deliverable_set)
        # totalResults already saved above
        db_save(db_cache)
        log(f"[DB] Wave snapshot persisted: deliverable_now={len(current_deliverable_set)}, totalResults={total_results_now}")

def handle_no_wave(sess, products, total_results_now):
    """
    Stock not up.
    If totalResults is exactly same as last_total_results: skip everything (fast path).
    If totalResults changed but didn't go up (dropped / reshuffled), update db snapshot quietly.
    """

    with db_lock:
        prev_total = db_get_last_total_results(db_cache)

    if total_results_now == prev_total:
        log(f"[NO-WAVE] totalResults same ({total_results_now}). Skipping checks/alerts.")
        return

    log(f"[NO-WAVE] Stock changed but not up. prev_total={prev_total}, now={total_results_now}. Refreshing snapshot quietly.")

    current_deliverable_set = set()

    def svc_worker(prod):
        code = prod.get("code")
        svc = check_serviceability(sess, code, REAL_PIN)
        return code, svc

    with ThreadPoolExecutor(max_workers=SVC_MAX_WORKERS) as ex:
        futs = [ex.submit(svc_worker, p) for p in products]
        for fut in as_completed(futs):
            try:
                code, svc = fut.result()
            except Exception:
                log_exc("svc_worker crashed in no-wave")
                continue

            if code and svc.get("deliverable") is True:
                current_deliverable_set.add(code)

    with db_lock:
        db_replace_prev_deliverable_set(db_cache, current_deliverable_set)
        db_set_last_total_results(db_cache, total_results_now)
        db_save(db_cache)
        log(f"[DB] no-wave snapshot saved. deliverable_now={len(current_deliverable_set)}, totalResults_now={total_results_now}")

# ================== MAIN LOOP =========================
def stock_poll_loop():
    global startup_message_sent
    sess = make_plp_session()

    if not startup_message_sent:
        boot_msg = (
            "🟢 <b>SHEIN Stock Watcher Online</b>\n"
            f"Monitoring pincode <b>{MASKED_PIN}</b>.\n"
            "We’ll blast new deliverable drops here in realtime. 🔥"
        )
        ok = telegram_send_text(boot_msg)
        log(f"[BOOT] Startup message sent={ok}")
        startup_message_sent = True

    while True:
        poll_start = time.time()
        try:
            products, total_results_now = fetch_all_products(sess)

            with db_lock:
                prev_total_results = db_get_last_total_results(db_cache)

            log(f"[POLL] totalResults_now={total_results_now}, prev_total_results={prev_total_results}, products_fetched={len(products)}")

            if total_results_now > prev_total_results:
                handle_wave(sess, products, total_results_now, prev_total_results)
            else:
                handle_no_wave(sess, products, total_results_now)

        except Exception:
            log_exc("MAIN POLL LOOP ERROR")

        elapsed = time.time() - poll_start
        log(f"[POLL] cycle_done in {elapsed:.2f}s, sleeping {PRODUCT_POLL_INTERVAL_SEC}s\n")
        time.sleep(PRODUCT_POLL_INTERVAL_SEC)

# ================== MAIN ==============================
def main():
    global db_cache
    if len(BOT_TOKENS) < 1:
        raise SystemExit("Please configure BOT_TOKENS with at least 1 token.")

    db_cache = db_load()
    log(f"[BOOT] db.json loaded: last_total_results={db_get_last_total_results(db_cache)}, prev_deliverable={len(db_get_prev_deliverable_set(db_cache))} items")

    stock_poll_loop()

if __name__ == "__main__":
    main()
