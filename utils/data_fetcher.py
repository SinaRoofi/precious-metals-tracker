# utils/data_fetcher.py
"""دریافت داده‌های بازار: قیمت جهانی طلا/نقره و دلار مبنا (رهاورد)،
دلار بازار آزاد (تلگرام)، دارایی‌های داخلی و صندوق‌ها (رهاورد/تریدرآرنا)."""

import re
import time
import logging
import pytz
import requests
from telethon import TelegramClient
from bs4 import BeautifulSoup

from config import (
    TELEGRAM_CHANNELS, API_URLS, HTTP_HEADERS,
    MAX_RETRIES, RETRY_DELAY, REQUEST_TIMEOUT,
)

logger = logging.getLogger(__name__)

DOLLAR_CHANNEL = TELEGRAM_CHANNELS['dollar']


# ==============================================================================
# دلار بازار آزاد (تلگرام) — بدون تغییر منطقی نسبت به نسخه‌ی قبلی، فقط منبع مشترک
# ==============================================================================

def extract_prices_new(text):
    """استخراج قیمت‌های دلار (معامله/خرید/فروش) از متن پیام."""
    prices = {"معامله": None, "خرید": None, "فروش": None}

    معامله_pattern = r"(\d{1,3})[,،\u200c\u200b\s]*(\d{3})\s*مـعامله\s*شد"
    خرید_pattern = r"(\d{1,3})[,،\u200c\u200b\s]*(\d{3})\s*خــرید"
    فروش_pattern = r"(\d{1,3})[,،\u200c\u200b\s]*(\d{3})\s*فروش"

    معامله_match = re.search(معامله_pattern, text)
    if معامله_match:
        prices["معامله"] = int(معامله_match.group(1) + معامله_match.group(2))

    خرید_match = re.search(خرید_pattern, text)
    if خرید_match:
        prices["خرید"] = int(خرید_match.group(1) + خرید_match.group(2))

    فروش_match = re.search(فروش_pattern, text)
    if فروش_match:
        prices["فروش"] = int(فروش_match.group(1) + فروش_match.group(2))

    return prices


async def fetch_dollar_prices(client: TelegramClient):
    """دریافت قیمت‌های دلار بازار آزاد از کانال تلگرام (مشترک برای طلا و نقره)."""
    try:
        tehran_tz = pytz.timezone("Asia/Tehran")
        messages = await client.get_messages(DOLLAR_CHANNEL, limit=50)

        final_prices = {
            "last_trade": None, "bid": None, "ask": None,
            "last_trade_time": None, "bid_time": None, "ask_time": None,
        }

        for message in messages:
            if message.text and "دلار فردایی" in message.text:
                prices = extract_prices_new(message.text)
                msg_time_tehran = message.date.astimezone(tehran_tz)

                if prices["معامله"] and not final_prices["last_trade"]:
                    final_prices["last_trade"] = prices["معامله"]
                    final_prices["last_trade_time"] = msg_time_tehran

                if prices["خرید"] and not final_prices["bid"]:
                    final_prices["bid"] = prices["خرید"]
                    final_prices["bid_time"] = msg_time_tehran

                if prices["فروش"] and not final_prices["ask"]:
                    final_prices["ask"] = prices["فروش"]
                    final_prices["ask_time"] = msg_time_tehran

                if all([final_prices["last_trade"], final_prices["bid"], final_prices["ask"]]):
                    break

        if final_prices["last_trade"]:
            logger.info(
                f"✅ قیمت‌های دلار: معامله={final_prices['last_trade']:,}, "
                f"خرید={final_prices['bid']:,}, فروش={final_prices['ask']:,}"
            )
        else:
            logger.warning("❌ قیمت معامله دلار پیدا نشد")

        if any([final_prices["last_trade"], final_prices["bid"], final_prices["ask"]]):
            return final_prices
        return None

    except Exception as e:
        logger.error(f"خطا در دریافت قیمت دلار: {e}")
        return None


# ==============================================================================
# قیمت جهانی طلا/نقره + دلار مبنا (رهاورد light-charts)
# ==============================================================================

def fetch_light_chart(commodity: str, max_retries: int = MAX_RETRIES,
                       retry_delay: int = RETRY_DELAY):
    """
    دریافت قیمت جهانی انس (طلا یا نقره) از رهاورد.

    ساختار پاسخ رهاورد: data["data"][0] = انس کالا، data["data"][3] = دلار مبنا.
    هشدار: دلار این endpoint نیمایی/مبناست، نه دلار بازار آزاد — در این پروژه
    مصرف نمی‌شود (دلار بازار آزاد از fetch_dollar_prices/تلگرام می‌آید).
    فقط global_price این تابع استفاده می‌شود.

    Returns:
        dict {'price': float, 'change_percent': float} یا None در صورت شکست
    """
    if commodity not in API_URLS:
        raise ValueError(f"کالای نامعتبر: {commodity}")

    url = API_URLS[commodity]['light_charts']

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"📡 [{commodity}] تلاش {attempt}/{max_retries} - درخواست light-charts...")
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()["data"]

            global_price = data[0]["close_price"]
            global_change_percent = round(data[0]["close_price_change_percent"] * 100, 2)

            logger.info(f"✅ [{commodity}] قیمت جهانی: {global_price}")
            return {"price": global_price, "change_percent": global_change_percent}

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ [{commodity}] تلاش {attempt}: خطای درخواست light-charts - {e}")
        except (KeyError, IndexError, ValueError) as e:
            logger.error(f"❌ [{commodity}] تلاش {attempt}: پاسخ light-charts نامعتبر - {e}")

        if attempt < max_retries:
            logger.info(f"⏳ [{commodity}] صبر {retry_delay} ثانیه قبل از تلاش مجدد...")
            time.sleep(retry_delay)
        else:
            logger.error(f"❌ [{commodity}] همه‌ی تلاش‌های light-charts ناموفق بود")
            return None

    return None


# ==============================================================================
# دارایی‌های داخلی (رهاورد intrinsic-values) و صندوق‌ها (تریدرآرنا)
# ==============================================================================

def fetch_market_data(commodity: str, max_retries: int = MAX_RETRIES,
                       retry_delay: int = RETRY_DELAY):
    """
    دریافت داده‌ی دارایی‌های داخلی (رهاورد) و صندوق‌ها (تریدرآرنا) برای یک کالا.

    Returns:
        dict {'intrinsic_data': ..., 'funds_data': ...} یا None در صورت شکست کامل
    """
    if commodity not in API_URLS:
        raise ValueError(f"کالای نامعتبر: {commodity}")

    session = requests.Session()
    session.headers.update(HTTP_HEADERS)

    url_intrinsic = API_URLS[commodity]['intrinsic']
    url_funds = API_URLS[commodity]['funds']

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"📡 [{commodity}] تلاش {attempt}/{max_retries} - درخواست intrinsic-values...")
            resp1 = session.get(url_intrinsic, timeout=REQUEST_TIMEOUT)
            resp1.raise_for_status()
            intrinsic_data = resp1.json()
            logger.info(f"✅ [{commodity}] intrinsic-values دریافت شد")

            time.sleep(2)

            logger.info(f"📡 [{commodity}] تلاش {attempt}/{max_retries} - درخواست funds...")
            resp2 = session.get(url_funds, timeout=REQUEST_TIMEOUT)
            resp2.raise_for_status()
            funds_data = resp2.json()
            logger.info(f"✅ [{commodity}] funds دریافت شد")

            return {"intrinsic_data": intrinsic_data, "funds_data": funds_data}

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ [{commodity}] تلاش {attempt}: خطای درخواست - {e}")
        except ValueError as e:
            logger.error(f"❌ [{commodity}] تلاش {attempt}: پاسخ JSON نامعتبر - {e}")

        if attempt < max_retries:
            logger.info(f"⏳ [{commodity}] صبر {retry_delay} ثانیه قبل از تلاش مجدد...")
            time.sleep(retry_delay)
        else:
            logger.error(f"❌ [{commodity}] همه‌ی تلاش‌ها ناموفق بود")
            return None

    return None


# ==============================================================================
# درهم امارات — مستقل از طلا/نقره، بدون تغییر
# ==============================================================================

def fetch_dirham_price():
    """دریافت قیمت فروش درهم امارات از alanchand.com"""
    try:
        def persian_to_english_number(s):
            persian_numbers = "۰۱۲۳۴۵۶۷۸۹"
            english_numbers = "0123456789"
            for p, e in zip(persian_numbers, english_numbers):
                s = s.replace(p, e)
            return s

        url = "https://alanchand.com/currencies-price"
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table")

        price_sale_dirham = None
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if cols and cols[0].text.strip() == "درهم":
                price_sale_dirham = cols[2].text.strip()
                break

        if price_sale_dirham:
            price_sale_dirham = persian_to_english_number(price_sale_dirham).replace(",", "")
            price_sale_dirham_int = int(price_sale_dirham)
            logger.info(f"✅ قیمت درهم: {price_sale_dirham_int:,} تومان")
            return price_sale_dirham_int

        logger.warning("⚠️ قیمت فروش درهم پیدا نشد")
        return None

    except Exception as e:
        logger.error(f"❌ خطا در دریافت قیمت درهم: {e}")
        return None
