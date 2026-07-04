"""
ماژول مدیریت تعطیلات رسمی ایران

استراتژی: API زنده (holidayapi.ir) اولویت اول -> لیست هاردکد fallback -> fail-safe.
"""

import logging
import datetime as _dt
from functools import lru_cache
import requests
import jdatetime

logger = logging.getLogger(__name__)

API_URL = "https://holidayapi.ir/gregorian"
API_TIMEOUT = 5

MANUAL_EMERGENCY_HOLIDAYS = {
     (1405, 4, 13),(1405, 4, 14),(1405, 4, 15),(1405, 4, 16),

}

# تعطیلات رسمی 1404 (fallback وقتی API در دسترس نیست)
IRANIAN_HOLIDAYS_1404 = {
    (1404, 1, 1), (1404, 1, 2), (1404, 1, 3), (1404, 1, 4),
    (1404, 1, 11), (1404, 1, 12), (1404, 1, 13),
    (1404, 2, 4),
    (1404, 3, 14), (1404, 3, 15), (1404, 3, 16), (1404, 3, 24),
    (1404, 4, 14), (1404, 4, 15),
    (1404, 5, 23), (1404, 5, 31),
    (1404, 6, 2), (1404, 6, 10), (1404, 6, 19),
    (1404, 9, 3),
    (1404, 10, 13), (1404, 10, 27),
    (1404, 11, 15), (1404, 11, 22),
    (1404, 12, 20), (1404, 12, 29),
}

# تعطیلات رسمی 1405 (fallback)
IRANIAN_HOLIDAYS_1405 = {
    (1405, 1, 1), (1405, 1, 2), (1405, 1, 3), (1405, 1, 4),
    (1405, 1, 12), (1405, 1, 13), (1405, 1, 25),
    (1405, 3, 6), (1405, 3, 14), (1405, 3, 15),
    (1405, 4, 3), (1405, 4, 4),
    (1405, 5, 13), (1405, 5, 21), (1405, 5, 22), (1405, 5, 30),
    (1405, 6, 8),
    (1405, 8, 22),
    (1405, 10, 2), (1405, 10, 16),
    (1405, 11, 4), (1405, 11, 22),
    (1405, 12, 9), (1405, 12, 19), (1405, 12, 20), (1405, 12, 29),
}

# ⚠️ هر سال جدید (نزدیک نوروز) باید اینجا اضافه بشه — این فقط fallback است
IRANIAN_HOLIDAYS = {
    1404: IRANIAN_HOLIDAYS_1404,
    1405: IRANIAN_HOLIDAYS_1405,
}

WEEKEND_WEEKDAY = {3, 4}  # weekday(): 0=دوشنبه ... 3=پنج‌شنبه, 4=جمعه


def _check_holiday_api(year, month, day):
    """
    بررسی تعطیلات رسمی از holidayapi.ir.

    Returns:
        bool: True/False اگر پاسخ معتبر گرفتیم
        None: اگر API در دسترس نبود یا پاسخ نامعتبر بود (سیگنال برای fallback)
    """
    try:
        response = requests.get(
            f"{API_URL}/{year}/{month:02d}/{day:02d}", timeout=API_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()
        events = data.get("events", [])

        # جمعه رو خودمون جدا مدیریت می‌کنیم (قبل از این تابع چک شده)
        return any(
            event.get("is_holiday", False) and event.get("description") != "جمعه"
            for event in events
        )

    except requests.RequestException as e:
        logger.warning(f"⚠️ holidayapi.ir در دسترس نیست ({e}) — fallback به لیست هاردکد")
        return None
    except (ValueError, KeyError) as e:
        logger.warning(f"⚠️ پاسخ نامعتبر از holidayapi.ir ({e}) — fallback به لیست هاردکد")
        return None


def _check_hardcoded_fallback(jalali_year, date_tuple):
    """
    Returns:
        bool: True/False اگر سال در لیست هاردکد بود
        None: اگر سال هم در لیست هاردکد نبود (سیگنال برای fail-safe)
    """
    if jalali_year not in IRANIAN_HOLIDAYS:
        return None
    return date_tuple in IRANIAN_HOLIDAYS[jalali_year]


@lru_cache(maxsize=64)
def _is_holiday_cached(year, month, day):
    """منطق اصلی، کش‌شده روی تاریخ (نه datetime کامل) تا فراخوانی تکراری API نداشته باشیم."""
    jalali_date = jdatetime.date.fromgregorian(year=year, month=month, day=day)
    date_tuple = (jalali_date.year, jalali_date.month, jalali_date.day)

    # 0) بالاترین اولویت: تعطیلات اضطراری دستی
    if date_tuple in MANUAL_EMERGENCY_HOLIDAYS:
        logger.info(f"🚨 {date_tuple} در MANUAL_EMERGENCY_HOLIDAYS است — تعطیل اضطراری")
        return True

    weekday = _dt.date(year, month, day).weekday()
    if weekday in WEEKEND_WEEKDAY:
        return True

    # 1) اولویت اول: API زنده
    api_result = _check_holiday_api(year, month, day)
    if api_result is not None:
        return api_result

    # 2) fallback: لیست هاردکد
    fallback_result = _check_hardcoded_fallback(jalali_date.year, date_tuple)
    if fallback_result is not None:
        return fallback_result

    # 3) هیچ منبعی در دسترس نیست -> fail-safe: فرض کن تعطیله (اجرا نکن)
    logger.error(
        f"🚨 نه API نه لیست هاردکد برای سال {jalali_date.year} در دسترس بود — "
        f"fail-safe فعال شد، امروز به‌عنوان تعطیل در نظر گرفته می‌شود"
    )
    return True


def is_iranian_holiday(date_obj):
    """
    بررسی اینکه آیا تاریخ داده شده تعطیل است یا نه.

    ترتیب اولویت: جمعه/پنج‌شنبه (بدون نیاز به API) -> holidayapi.ir -> لیست هاردکد -> fail-safe (True)

    Args:
        date_obj: datetime object

    Returns:
        bool: True اگر تعطیل باشد
    """
    return _is_holiday_cached(date_obj.year, date_obj.month, date_obj.day)


def is_working_day(date_obj):
    """
    بررسی اینکه آیا روز کاری است یا نه

    Args:
        date_obj: datetime object

    Returns:
        bool: True اگر روز کاری باشد
    """
    return not is_iranian_holiday(date_obj)
