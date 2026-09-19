# utils/salaf.py
"""
عسکه — سلف امامی بانک مرکزی (رهاورد asset 51198).

این ماژول کاملاً مستقله و بعد از سررسید (۱۸ آذر ۱۴۰۵) قابل حذفه؛ ثابت‌ها تو config.py
(بلاک SALAF_*) هستن. بعد از سررسید خودش هم بدون هیچ درخواست شبکه‌ای None برمی‌گردونه.

خروجی برای کپشن: دو خط
    🏦 عسکه: قیمت (درصد تغییر)
    🎯 حداقل بازده: x٪

«حداقل بازده» = تضمین ÷ قیمت × (۱ − کارمزد) − ۱  (عسکه مثل آپشن پوته: کف ۲۵۲ م، و
اگه امامی بالاتر بره، قیمت بازار بیشتر می‌ده).
"""

import logging
import math
from datetime import datetime

import pytz
import requests
from persiantools.jdatetime import JalaliDate

from config import (
    REQUEST_TIMEOUT,
    SALAF_ASSET_URL, SALAF_GUARANTEE_PRICE, SALAF_MATURITY_JALALI, SALAF_FEE,
    SALAF_PRICE_SCALE,
)

logger = logging.getLogger(__name__)

TEHRAN = pytz.timezone("Asia/Tehran")


def days_to_maturity(now=None):
    """روزهای باقی‌مانده تا سررسید (امروز حساب نمی‌شه). منفی/صفر یعنی سررسید گذشته."""
    now = now or datetime.now(TEHRAN)
    maturity = JalaliDate(*SALAF_MATURITY_JALALI).to_gregorian()
    return (maturity - now.date()).days


def floor_return(price):
    """حداقل بازده تا سررسید بعد از کارمزد، به‌صورت کسری (۰.۰۴۸ = ۴.۸٪)."""
    return SALAF_GUARANTEE_PRICE / price * (1 - SALAF_FEE) - 1


def fetch_salaf():
    """
    آخرین معامله‌ی عسکه → {"price": تومان، "change_pct": درصد} یا None.
    هر خطا/داده‌ی نامعتبر None برمی‌گردونه تا کپشن سالم بمونه.
    """
    if days_to_maturity() <= 0:
        return None  # بعد از سررسید: بدون درخواست شبکه

    try:
        resp = requests.get(
            SALAF_ASSET_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        last_trade = (resp.json().get("data") or {}).get("last_trade")
        if not last_trade:
            logger.warning("⚠️ عسکه: last_trade خالیه")
            return None

        price = float(last_trade["real_close_price"]) * SALAF_PRICE_SCALE
        change_pct = float(last_trade["real_close_price_change_percent"]) * 100
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        logger.error(f"❌ خطا در دریافت عسکه: {e}")
        return None

    if not (math.isfinite(price) and price > 0 and math.isfinite(change_pct)):
        logger.warning(f"⚠️ عسکه: مقدار نامعتبر (price={price}, change={change_pct})")
        return None

    logger.info(f"✅ عسکه: {price:,.0f} ({change_pct:+.2f}%)")
    return {"price": price, "change_pct": change_pct}


def format_salaf_lines(salaf, days):
    """دو خط کپشن (با \\n آخر هر خط)، یا None اگه داده/سررسید معتبر نباشه."""
    if not salaf or days <= 0:
        return None
    total = floor_return(salaf["price"])
    return (
        f"🏦 عسکه: {salaf['price']:,.0f} ({salaf['change_pct']:+.1f}%)\n"
        f"🎯 حداقل بازده: {total * 100:+.1f}%\n"
    )


def fetch_salaf_lines():
    """نقطه‌ی ورود main.py: دریافت + فرمت. None یعنی «خط عسکه نیاد»."""
    salaf = fetch_salaf()
    return format_salaf_lines(salaf, days_to_maturity()) if salaf else None
