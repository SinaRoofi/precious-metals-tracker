"""
ماژول مدیریت تعطیلات رسمی ایران

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
    (1405, 4, 13), (1405, 4, 14), (1405, 4, 15),
}

# تعطیلات رسمی 1405 (fallback وقتی API در دسترس نیست)
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

IRANIAN_HOLIDAYS_1406 = {
     (1406, 1, 1),(1406, 1, 2),(1406, 1, 3),(1406, 1, 4)
}

IRANIAN_HOLIDAYS = {
    1405: IRANIAN_HOLIDAYS_1405,
    1406: IRANIAN_HOLIDAYS_1406,
}

WEEKEND_WEEKDAY = {3, 4}  # 3=پنج‌شنبه, 4=جمعه


def _check_holiday_api(year, month, day):
    """
    بررسی تعطیلات رسمی از holidayapi.ir
    """
    try:
        response = requests.get(
            f"{API_URL}/{year}/{month:02d}/{day:02d}",
            timeout=API_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()
        events = data.get("events", [])

        return any(
            event.get("is_holiday", False) and event.get("description") != "جمعه"
            for event in events
        )

    except requests.RequestException as e:
        logger.warning(f"holidayapi.ir unavailable ({e}) -> fallback")
        return None
    except (ValueError, KeyError) as e:
        logger.warning(f"Invalid response from holidayapi.ir ({e}) -> fallback")
        return None


def _check_hardcoded_fallback(jalali_year, date_tuple):
    """
    بررسی لیست هاردکد.

    Returns:
        True/False: اگه سال واقعاً در IRANIAN_HOLIDAYS ثبت شده باشه
        None: اگه سال اصلاً ثبت نشده (سیگنال برای رفتن به fail-safe)
    """
    if jalali_year not in IRANIAN_HOLIDAYS:
        return None
    return date_tuple in IRANIAN_HOLIDAYS[jalali_year]


@lru_cache(maxsize=64)
def _is_holiday_cached(year, month, day):
    """
    منطق اصلی بررسی تعطیلی (کش‌شده)
    """
    jalali_date = jdatetime.date.fromgregorian(
        year=year, month=month, day=day
    )
    date_tuple = (jalali_date.year, jalali_date.month, jalali_date.day)

    # 0) تعطیلات اضطراری
    if date_tuple in MANUAL_EMERGENCY_HOLIDAYS:
        logger.info(f"Emergency holiday: {date_tuple}")
        return True

    # 1) آخر هفته
    weekday = _dt.date(year, month, day).weekday()
    if weekday in WEEKEND_WEEKDAY:
        return True

    # 2) API
    api_result = _check_holiday_api(year, month, day)
    if api_result is not None:
        return api_result

    # 3) fallback
    fallback_result = _check_hardcoded_fallback(jalali_date.year, date_tuple)
    if fallback_result is not None:
        return fallback_result

    # 4) fail-safe
    logger.error(
        f"No data available for year {jalali_date.year} -> fail-safe holiday=True"
    )
    return True


def is_iranian_holiday(date_obj):
    """
    بررسی تعطیل بودن روز
    """
    return _is_holiday_cached(date_obj.year, date_obj.month, date_obj.day)


def is_working_day(date_obj):
    """
    بررسی روز کاری بودن
    """
    return not is_iranian_holiday(date_obj)
