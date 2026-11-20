# utils/gist_store.py
"""دسترسی یکپارچه به GitHub Gist (GET/PATCH با retry).

قبلاً همین منطق ۷ جا کپی شده بود (۶ جا تو alerts.py بدون retry، یکی تو telegram_sender.py با retry).
حالا همه از این ماژول استفاده می‌کنن:
  - retry روی خطای گذرا (تایم‌اوت/شبکه/۴۲۹/۵xx/۴۰۳)
  - ۴۰۴ و بقیه‌ی ۴xx عمداً retry نمی‌شن
  - هدر Authorization: Bearer (فرمت پیشنهادی GitHub؛ `token` قدیمی‌تره)
"""

import json
import logging
import time

import requests

from config import GIST_ID, GIST_TOKEN, MAX_RETRIES, RETRY_DELAY, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# 403 → معمولاً secondary rate limit گیت‌هاب (نه لزوماً توکن غلط)؛ 429 → rate limit صریح؛ 5xx → مشکل موقت سرور
TRANSIENT_STATUS_CODES = {403, 429, 500, 502, 503, 504}


def is_configured():
    return bool(GIST_ID and GIST_TOKEN)


def _url():
    return f"https://api.github.com/gists/{GIST_ID}"


def _headers():
    return {
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def request_with_retry(method, label, json_body=None, max_retries=MAX_RETRIES, retry_delay=RETRY_DELAY):
    """درخواست GET/PATCH به Gist با retry روی خطای گذرا.

    Returns:
        (response یا None, status) — status یکی از:
        "no_config", "ok", "timeout", "network_error", "http_error:CODE"
    """
    if not is_configured():
        return None, "no_config"

    last_status = "unknown_error"
    for attempt in range(1, max_retries + 1):
        start = time.monotonic()
        try:
            if method == "GET":
                response = requests.get(_url(), headers=_headers(), timeout=REQUEST_TIMEOUT)
            else:
                response = requests.patch(_url(), headers=_headers(), json=json_body, timeout=REQUEST_TIMEOUT)
            elapsed = time.monotonic() - start

            if response.status_code == 200:
                logger.info(f"✅ {label} موفق (تلاش {attempt}/{max_retries}, {elapsed:.1f}s)")
                return response, "ok"

            last_status = f"http_error:{response.status_code}"
            if response.status_code == 404:
                return response, last_status

            if response.status_code in TRANSIENT_STATUS_CODES and attempt < max_retries:
                logger.warning(
                    f"⚠️ {label} کد {response.status_code} برگشت (گذرا، تلاش {attempt}/{max_retries}, "
                    f"{elapsed:.1f}s) — {retry_delay}s صبر و تلاش مجدد..."
                )
                time.sleep(retry_delay)
                continue

            logger.error(f"❌ {label} با کد {response.status_code} برگشت ({elapsed:.1f}s) — بدنه: {response.text[:300]}")
            return response, last_status

        except requests.exceptions.Timeout as e:
            last_status = "timeout"
            logger.warning(f"⏱️ {label} TIMEOUT (تلاش {attempt}/{max_retries}, {time.monotonic() - start:.1f}s): {e}")
        except requests.exceptions.RequestException as e:
            last_status = "network_error"
            logger.warning(
                f"🌐 {label} خطای شبکه (تلاش {attempt}/{max_retries}, {time.monotonic() - start:.1f}s): "
                f"{type(e).__name__}: {e}"
            )

        if attempt < max_retries:
            time.sleep(retry_delay)

    logger.error(f"❌ {label} بعد از {max_retries} تلاش ناموفق ماند (status={last_status})")
    return None, last_status


def read_files(label):
    """همه‌ی فایل‌های Gist رو می‌خونه. Returns: (files dict یا None, status)"""
    response, status = request_with_retry("GET", label)
    if status != "ok":
        return None, status
    try:
        return response.json()["files"], "ok"
    except (KeyError, ValueError, TypeError) as e:
        logger.error(f"❌ {label}: کد 200 بود ولی ساختار پاسخ نامعتبره: {type(e).__name__}: {e}")
        return None, "parse_error"


def read_json(filename, label):
    """یک فایل JSON رو از Gist می‌خونه.

    Returns: (data, state)
        state = "ok"        → data = محتوای پارس‌شده
        state = "missing"   → Gist در دسترسه ولی این فایل هنوز ساخته نشده (اولین استفاده؛ data=None)
        state = دیگه        → خواندن/پارس ناموفق (no_config / timeout / network_error / http_error:CODE / parse_error)
    """
    files, status = read_files(label)
    if status != "ok":
        return None, status
    if filename not in files:
        return None, "missing"
    try:
        return json.loads(files[filename]["content"]), "ok"
    except (KeyError, ValueError, TypeError) as e:
        logger.error(f"❌ {label}: پارس محتوای {filename} شکست خورد: {type(e).__name__}: {e}")
        return None, "parse_error"


def write_json(filename, data, label):
    """یک فایل JSON رو تو Gist می‌نویسه (PATCH با retry). Returns: True اگه موفق."""
    body = {"files": {filename: {"content": json.dumps(data, ensure_ascii=False)}}}
    _, status = request_with_retry("PATCH", label, json_body=body)
    return status == "ok"
