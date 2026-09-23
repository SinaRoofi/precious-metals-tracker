# utils/alerts.py

import json
import logging
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pytz
import jdatetime
from config import (
    DOLLAR_HIGH,
    DOLLAR_LOW,
    SHAMS_HIGH,
    SHAMS_LOW,
    GOLD_HIGH,
    GOLD_LOW,
    SILVER_SHAMS_HIGH,
    SILVER_SHAMS_LOW,
    SILVER_HIGH,
    SILVER_LOW,
    BULLION_ASSET,
    ALERT_THRESHOLD_PERCENT,
    EKHTELAF_THRESHOLD,
    BUBBLE_SHARP_CHANGE_THRESHOLD,
    HARD_SIGNAL_BUBBLE_THRESHOLD,
    FUND_PRICE_ALERTS,
    GIST_ID,
    GIST_TOKEN,
    ALERT_STATUS_FILE,
    SARANE_KHARID_BASELINE_FILE,
    SARANE_FOROSH_BASELINE_FILE,
    ALERT_CHANNEL_HANDLE,
    REQUEST_TIMEOUT,
    TIMEZONE,
    POL_SHARP_CHANGE_THRESHOLD,
    STANDARD_HEADER,
    SARANE_KHARID_MA_DAYS,
    SARANE_KHARID_MA_MIN_DAYS,
    SARANE_KHARID_SPIKE_MULTIPLIER,
    SARANE_FOROSH_SPIKE_MULTIPLIER,
    TRADE_VALUE_ABOVE_AVG_MULTIPLIER,
    TRADE_VALUE_SPIKE_MULTIPLIER,
    TRADE_VALUE_DOD_GROWTH_THRESHOLD,
    TRADE_VALUE_SHARP_CHANGE_RATIO,
    CURRENT_HOLDING,
    SWITCH_FEE_BUY,
    SWITCH_FEE_SELL,
    SWITCH_SAFETY_MARGIN,
    SWITCH_TOP_N,
)
from utils.sheets_storage import read_from_sheets

# حداکثر تعداد ردیف تاریخچه که برای محاسبه‌ی میانگین چند روزه‌ی سرانه خرید می‌خوانیم
# (هم‌رده با TRADE_VALUE_HISTORY_LOOKBACK_ROWS در weekly_report.py)
SARANE_KHARID_HISTORY_LOOKBACK_ROWS = 3000

# ✅ کش محلی برای جلوگیری از reset در صورت خطای Gist (fallback، مثل ALERT_STATUS_CACHE)
SARANE_KHARID_BASELINE_CACHE = None

logger = logging.getLogger(__name__)

COMMODITY_LABEL = {"gold": "طلا", "silver": "نقره"}

THRESHOLDS = {
    "gold": {"ounce_high": GOLD_HIGH, "ounce_low": GOLD_LOW,
             "shams_high": SHAMS_HIGH, "shams_low": SHAMS_LOW},
    "silver": {"ounce_high": SILVER_HIGH, "ounce_low": SILVER_LOW,
               "shams_high": SILVER_SHAMS_HIGH, "shams_low": SILVER_SHAMS_LOW},
}

# ✅ کش محلی برای جلوگیری از reset در صورت خطای Gist
ALERT_STATUS_CACHE = None


# ════════════════════════════════════════════════════════════════
# تابع کمکی برای تبدیل به تاریخ شمسی
# ════════════════════════════════════════════════════════════════


def get_jalali_timestamp(dt):
    """تبدیل datetime به تاریخ و ساعت شمسی"""
    j = jdatetime.datetime.fromgregorian(datetime=dt)
    return j.strftime("%Y/%m/%d - %H:%M")


def _default_alert_status():
    status = {"dollar": "normal"}
    for c in ("gold", "silver"):
        status[f"{c}_shams"] = "normal"
        status[f"{c}_ounce"] = "normal"
        status[f"{c}_bubble"] = "normal"
        status[f"{c}_pol_hagigi"] = "normal"
        status[f"{c}_hard_signal"] = "normal"
        status[f"{c}_sarane_kharid_spike"] = "normal"
        status[f"{c}_sarane_forosh_spike"] = "normal"
        status[f"{c}_trade_value_level"] = "normal"
        status[f"{c}_trade_value_dod_growth"] = "normal"
        status[f"{c}_switch_signal"] = "normal"
    for symbol in FUND_PRICE_ALERTS:
        status[f"fund_{symbol}"] = "normal"
    return status


# ════════════════════════════════════════════════════════════════
# مدیریت Gist
# ════════════════════════════════════════════════════════════════


def get_alert_status():
    """دریافت وضعیت هشدارها از Gist با fallback به کش محلی"""
    global ALERT_STATUS_CACHE

    try:
        if not GIST_ID or not GIST_TOKEN:
            logger.warning("GIST_ID یا GIST_TOKEN تنظیم نشده است")
            return ALERT_STATUS_CACHE or _default_alert_status()

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}"}
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if r.status_code == 200 and ALERT_STATUS_FILE in r.json()["files"]:
            status = json.loads(r.json()["files"][ALERT_STATUS_FILE]["content"])

            for key in _default_alert_status():
                status.setdefault(key, "normal")

            ALERT_STATUS_CACHE = status
            return status

    except Exception as e:
        logger.error(f"خطا در خواندن alert_status: {e}")
        if ALERT_STATUS_CACHE:
            logger.info("استفاده از کش محلی")
            return ALERT_STATUS_CACHE

    default = _default_alert_status()
    ALERT_STATUS_CACHE = default
    return default


def save_alert_status(status):
    """ذخیره وضعیت هشدارها در Gist"""
    global ALERT_STATUS_CACHE

    try:
        if not GIST_ID or not GIST_TOKEN:
            return

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}"}

        response = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    ALERT_STATUS_FILE: {
                        "content": json.dumps(status, ensure_ascii=False)
                    }
                }
            },
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            ALERT_STATUS_CACHE = status

    except Exception as e:
        logger.error(f"خطا در ذخیره alert_status: {e}")


# ════════════════════════════════════════════════════════════════
# وضعیت قبلی از شیت (per-commodity — تب Gold یا Silver)
# ════════════════════════════════════════════════════════════════


def get_previous_state_from_sheet(commodity):
    """دریافت وضعیت قبلی یک کالا با بررسی فاصله زمانی، از تب مربوطه در شیت"""
    empty = {
        "dollar_price": None,
        "shams_price": None,
        "global_price": None,
        "ekhtelaf_sarane": None,
        "bubble_weighted": None,
        "pol_hagigi": None,
        "same_day": None,
        "trade_value": None,
    }

    try:
        rows = read_from_sheets(commodity, limit=3)

        if len(rows) < 2:
            logger.warning(f"[{commodity}] داده کافی برای مقایسه نیست")
            return empty

        prev_row = rows[-2]
        last_row = rows[-1]

        same_day = None
        try:
            prev_time = datetime.strptime(prev_row[0][:19], "%Y-%m-%d %H:%M:%S")
            last_time = datetime.strptime(last_row[0][:19], "%Y-%m-%d %H:%M:%S")
            time_diff = (last_time - prev_time).total_seconds() / 60
            same_day = prev_time.date() == last_time.date()

            if time_diff > 10:
                logger.warning(
                    f"⚠️ [{commodity}] فاصله زمانی غیرعادی: {time_diff:.1f} دقیقه (انتظار: ~5 دقیقه)"
                )
            else:
                logger.debug(f"✓ [{commodity}] فاصله زمانی: {time_diff:.1f} دقیقه")

        except Exception as e:
            logger.warning(f"[{commodity}] نمی‌تونم فاصله زمانی رو بررسی کنم: {e}")

        return {
            "dollar_price": (
                float(prev_row[2]) if len(prev_row) > 2 and prev_row[2] else None
            ),
            "shams_price": (
                float(prev_row[3]) if len(prev_row) > 3 and prev_row[3] else None
            ),
            "global_price": (
                float(prev_row[1]) if len(prev_row) > 1 and prev_row[1] else None
            ),
            "ekhtelaf_sarane": (
                float(prev_row[11]) if len(prev_row) > 11 and prev_row[11] else None
            ),
            "bubble_weighted": (
                float(prev_row[8]) if len(prev_row) > 8 and prev_row[8] else None
            ),
            "pol_hagigi": (
                float(prev_row[12]) if len(prev_row) > 12 and prev_row[12] else None
            ),
            "same_day": same_day,
            "trade_value": (
                float(prev_row[14]) if len(prev_row) > 14 and prev_row[14] else None
            ),
        }

    except Exception as e:
        logger.error(f"[{commodity}] خطا در خواندن وضعیت قبلی: {e}")
        return empty


# ════════════════════════════════════════════════════════════════
# میانگین چند روزه‌ی سرانه خرید بازار (برای هشدار جهش)
# ════════════════════════════════════════════════════════════════


def _default_sarane_kharid_baseline_store():
    return {c: {"date": None, "baseline": None} for c in ("gold", "silver")}


def get_sarane_kharid_baseline_store():
    """
    دریافت مقدار ذخیره‌شده‌ی baseline از Gist (فایل جدا، مستقل از alert_status.json)
    با fallback به کش محلی — دقیقاً هم‌الگوی get_alert_status().
    """
    global SARANE_KHARID_BASELINE_CACHE

    try:
        if not GIST_ID or not GIST_TOKEN:
            logger.warning("GIST_ID یا GIST_TOKEN تنظیم نشده است")
            return SARANE_KHARID_BASELINE_CACHE or _default_sarane_kharid_baseline_store()

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}"}
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if r.status_code == 200 and SARANE_KHARID_BASELINE_FILE in r.json()["files"]:
            store = json.loads(r.json()["files"][SARANE_KHARID_BASELINE_FILE]["content"])
            for key in _default_sarane_kharid_baseline_store():
                store.setdefault(key, {"date": None, "baseline": None})
            SARANE_KHARID_BASELINE_CACHE = store
            return store

    except Exception as e:
        logger.error(f"خطا در خواندن sarane_kharid_baseline: {e}")
        if SARANE_KHARID_BASELINE_CACHE:
            logger.info("استفاده از کش محلی baseline سرانه خرید")
            return SARANE_KHARID_BASELINE_CACHE

    default = _default_sarane_kharid_baseline_store()
    SARANE_KHARID_BASELINE_CACHE = default
    return default


def save_sarane_kharid_baseline_store(store):
    """ذخیره‌ی store در Gist — فایل جدا از alert_status.json (این فایل هم از قبل باید در همون Gist ساخته شده باشد)."""
    global SARANE_KHARID_BASELINE_CACHE

    try:
        if not GIST_ID or not GIST_TOKEN:
            return

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}"}

        response = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    SARANE_KHARID_BASELINE_FILE: {
                        "content": json.dumps(store, ensure_ascii=False)
                    }
                }
            },
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            SARANE_KHARID_BASELINE_CACHE = store

    except Exception as e:
        logger.error(f"خطا در ذخیره sarane_kharid_baseline: {e}")


def get_sarane_kharid_baseline(commodity):
    """
    میانگین سرانه خرید وزنی بازار روی آخرین SARANE_KHARID_MA_DAYS روز کاری «بسته»
    (یعنی به‌جز امروز) را برمی‌گرداند.

    محاسبه‌ی سنگین (خواندن کل تاریخچه‌ی Sheets) فقط یک‌بار در روز انجام می‌شود:
    نتیجه با تاریخ امروز در Gist ذخیره می‌شود، و تا وقتی تاریخ ذخیره‌شده هنوز
    امروز است، هر بار (حتی هر ۱ دقیقه) فقط همون مقدار ذخیره‌شده از Gist خونده
    می‌شود — نه کل شیت. اولین ران هر روز (که تاریخ ذخیره‌شده قدیمی/خالی است)
    محاسبه را دوباره انجام می‌دهد و baseline جدید را برای بقیه‌ی همون روز ذخیره
    می‌کند.

    Returns:
        float | None — None یعنی هنوز baseline معتبری محاسبه نشده
        (کمتر از SARANE_KHARID_MA_MIN_DAYS روز تاریخچه‌ی بسته موجود است).
    """
    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()
    today_str = today.isoformat()

    store = get_sarane_kharid_baseline_store()
    entry = store.get(commodity, {"date": None, "baseline": None})

    if entry.get("date") == today_str and entry.get("baseline") is not None:
        return entry["baseline"]

    baseline = _compute_sarane_kharid_baseline(commodity, today)

    store[commodity] = {"date": today_str, "baseline": baseline}
    save_sarane_kharid_baseline_store(store)

    return baseline


def _compute_sarane_kharid_baseline(commodity, today):
    """محاسبه‌ی واقعی میانگین (بدون کش/Gist) — یک‌بار در روز از get_sarane_kharid_baseline صدا زده می‌شود."""
    try:
        rows = read_from_sheets(commodity, limit=SARANE_KHARID_HISTORY_LOOKBACK_ROWS)
        if not rows:
            return None

        df = pd.DataFrame(rows, columns=STANDARD_HEADER)
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df["sarane_kharid_weighted"] = pd.to_numeric(
            df["sarane_kharid_weighted"], errors="coerce"
        )
        df = df.dropna(subset=["timestamp", "sarane_kharid_weighted"])
        if df.empty:
            return None

        df["date"] = df["timestamp"].dt.date

        # یک ردیف در روز (آخرین snapshot همون روز)، فقط روزهای قبل از امروز
        daily = (
            df[df["date"] < today]
            .sort_values("timestamp")
            .groupby("date", as_index=False)
            .last()
            .sort_values("date")
        )

        if len(daily) < SARANE_KHARID_MA_MIN_DAYS:
            logger.debug(
                f"[{commodity}] تاریخچه‌ی کافی برای میانگین سرانه خرید نیست "
                f"({len(daily)} روز < حداقل {SARANE_KHARID_MA_MIN_DAYS} روز)"
            )
            return None

        window = daily["sarane_kharid_weighted"].tail(SARANE_KHARID_MA_DAYS)
        return float(window.mean())

    except Exception as e:
        logger.error(f"[{commodity}] خطا در محاسبه‌ی میانگین سرانه خرید: {e}")
        return None


# ════════════════════════════════════════════════════════════════
# میانگین چند روزه‌ی سرانه فروش (برای خط «سرانه فروش» در کپشن)
# دقیقاً هم‌الگوی بلوک سرانه خرید بالا — فایل Gist جدا (SARANE_FOROSH_BASELINE_FILE)
# و کش محلی جدا، تا به baseline سرانه خرید (که هشدار جهش ازش استفاده می‌کنه) دست نخوره.
#
# ⚠️ نکته‌ی علامت: main.py مقدار سرانه فروش رو با علامت منفی تو شیت ذخیره می‌کنه
# (`"sarane_forosh_w": -sarane_forosh_w`؛ برای نمودارهای هفتگی/ماهانه که سرانه فروش
# رو زیر خط صفر می‌کشن). این baseline از همون ستون شیت می‌خونه، پس قبل از برگردوندن
# علامتش رو برمی‌گردونیم مثبت — چون مقدار لحظه‌ای سرانه فروش تو کپشن (که مستقیم از
# Fund_df محاسبه می‌شه، نه از شیت) همیشه مثبته و باید با baseline هم‌علامت باشه.
# ════════════════════════════════════════════════════════════════

SARANE_FOROSH_BASELINE_CACHE = None


def _default_sarane_forosh_baseline_store():
    return {c: {"date": None, "baseline": None} for c in ("gold", "silver")}


def get_sarane_forosh_baseline_store():
    """مثل get_sarane_kharid_baseline_store، ولی از فایل Gist جدای SARANE_FOROSH_BASELINE_FILE می‌خونه."""
    global SARANE_FOROSH_BASELINE_CACHE

    try:
        if not GIST_ID or not GIST_TOKEN:
            logger.warning("GIST_ID یا GIST_TOKEN تنظیم نشده است")
            return SARANE_FOROSH_BASELINE_CACHE or _default_sarane_forosh_baseline_store()

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}"}
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if r.status_code == 200 and SARANE_FOROSH_BASELINE_FILE in r.json()["files"]:
            store = json.loads(r.json()["files"][SARANE_FOROSH_BASELINE_FILE]["content"])
            for key in _default_sarane_forosh_baseline_store():
                store.setdefault(key, {"date": None, "baseline": None})
            SARANE_FOROSH_BASELINE_CACHE = store
            return store

    except Exception as e:
        logger.error(f"خطا در خواندن sarane_forosh_baseline: {e}")
        if SARANE_FOROSH_BASELINE_CACHE:
            logger.info("استفاده از کش محلی baseline سرانه فروش")
            return SARANE_FOROSH_BASELINE_CACHE

    default = _default_sarane_forosh_baseline_store()
    SARANE_FOROSH_BASELINE_CACHE = default
    return default


def save_sarane_forosh_baseline_store(store):
    """ذخیره‌ی store در فایل Gist جدای SARANE_FOROSH_BASELINE_FILE — این فایل با اولین
    PATCH موفق خودکار داخل همون Gist ساخته می‌شه (نیازی به ساخت دستی نیست)."""
    global SARANE_FOROSH_BASELINE_CACHE

    try:
        if not GIST_ID or not GIST_TOKEN:
            return

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}"}

        response = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    SARANE_FOROSH_BASELINE_FILE: {
                        "content": json.dumps(store, ensure_ascii=False)
                    }
                }
            },
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            SARANE_FOROSH_BASELINE_CACHE = store

    except Exception as e:
        logger.error(f"خطا در ذخیره sarane_forosh_baseline: {e}")


def get_sarane_forosh_baseline(commodity):
    """
    میانگین وزنی «سرانه فروش» بازار (به‌صورت مثبت) روی آخرین SARANE_KHARID_MA_DAYS روز
    کاری «بسته» را برمی‌گرداند — مثل get_sarane_kharid_baseline، همون کش روزانه‌ی Gist.

    Returns:
        float | None — None یعنی هنوز baseline معتبری محاسبه نشده.
    """
    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()
    today_str = today.isoformat()

    store = get_sarane_forosh_baseline_store()
    entry = store.get(commodity, {"date": None, "baseline": None})

    if entry.get("date") == today_str and entry.get("baseline") is not None:
        return entry["baseline"]

    baseline = _compute_sarane_forosh_baseline(commodity, today)

    store[commodity] = {"date": today_str, "baseline": baseline}
    save_sarane_forosh_baseline_store(store)

    return baseline


def _compute_sarane_forosh_baseline(commodity, today):
    """محاسبه‌ی واقعی میانگین (بدون کش/Gist) — یک‌بار در روز از get_sarane_forosh_baseline صدا زده می‌شود."""
    try:
        rows = read_from_sheets(commodity, limit=SARANE_KHARID_HISTORY_LOOKBACK_ROWS)
        if not rows:
            return None

        df = pd.DataFrame(rows, columns=STANDARD_HEADER)
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df["sarane_forosh_weighted"] = pd.to_numeric(
            df["sarane_forosh_weighted"], errors="coerce"
        )
        df = df.dropna(subset=["timestamp", "sarane_forosh_weighted"])
        if df.empty:
            return None

        df["date"] = df["timestamp"].dt.date

        daily = (
            df[df["date"] < today]
            .sort_values("timestamp")
            .groupby("date", as_index=False)
            .last()
            .sort_values("date")
        )

        if len(daily) < SARANE_KHARID_MA_MIN_DAYS:
            logger.debug(
                f"[{commodity}] تاریخچه‌ی کافی برای میانگین سرانه فروش نیست "
                f"({len(daily)} روز < حداقل {SARANE_KHARID_MA_MIN_DAYS} روز)"
            )
            return None

        window = daily["sarane_forosh_weighted"].tail(SARANE_KHARID_MA_DAYS)
        # تو شیت منفی ذخیره شده (نگاه کن به یادداشت بالای این بلوک) — برمی‌گردونیم مثبت
        return -float(window.mean())

    except Exception as e:
        logger.error(f"[{commodity}] خطا در محاسبه‌ی میانگین سرانه فروش: {e}")
        return None


def check_sarane_kharid_spike_alert(
    bot_token, chat_id, current_sarane_kharid, baseline, status, tz, now, commodity, label
):
    """
    بررسی و ارسال هشدار جهش سرانه خرید بازار: وقتی سرانه خرید فعلی حداقل
    SARANE_KHARID_SPIKE_MULTIPLIER برابر میانگین SARANE_KHARID_MA_DAYS روزه شود.

    state-based (مثل آستانه‌های قیمتی) — فقط موقع ورود به حالت «جهش» پیام
    می‌رود و دوباره وقتی نسبت به زیر آستانه برگشت، وضعیت به normal ریست می‌شود
    (بدون پیام)، تا هر بار که مقدار بالای آستانه می‌ماند اسپم نشود.

    باید baseline > 0 باشد؛ در غیر این صورت نسبت «دو برابر شدن» بی‌معنی است
    (میانگین پایه صفر/منفی) و بررسی رد می‌شود.
    """
    status_key = f"{commodity}_sarane_kharid_spike"

    if baseline is None or baseline <= 0:
        logger.warning(
            f"⚠️ [{commodity}] بررسی جهش سرانه خرید رد شد — baseline نامعتبر است "
            f"(baseline={baseline}). فعلی: {current_sarane_kharid:,.2f}. "
            f"ممکن است تاریخچه‌ی کافی (حداقل {SARANE_KHARID_MA_MIN_DAYS} روز) در Sheets نباشد، "
            f"یا خواندن Gist/Sheets خطا داده باشد — لاگ‌های بالاتر را چک کن."
        )
        return False

    ratio = current_sarane_kharid / baseline
    logger.info(
        f"📊 [{commodity}] سرانه خرید: فعلی {current_sarane_kharid:,.2f} | "
        f"میانگین {SARANE_KHARID_MA_DAYS}روزه {baseline:,.2f} | "
        f"نسبت {ratio:.2f}× (آستانه {SARANE_KHARID_SPIKE_MULTIPLIER:.1f}×)"
    )

    is_spike = current_sarane_kharid >= baseline * SARANE_KHARID_SPIKE_MULTIPLIER

    if is_spike:
        if status[status_key] != "spike":
            send_sarane_kharid_spike_alert(
                bot_token, chat_id, current_sarane_kharid, baseline, tz, now, label
            )
            status[status_key] = "spike"
            logger.info(
                f"🚀 [{commodity}] جهش سرانه خرید: فعلی {current_sarane_kharid:,.2f} "
                f"≥ {SARANE_KHARID_SPIKE_MULTIPLIER:.0f}× میانگین {baseline:,.2f}"
            )
            return True
        return False

    if status[status_key] != "normal":
        status[status_key] = "normal"
        return True

    return False


def send_sarane_kharid_spike_alert(bot_token, chat_id, current_value, baseline, tz, now, label):
    """ارسال هشدار جهش سرانه خرید بازار"""
    ratio = current_value / baseline if baseline else 0

    main_text = f"""
🚀 هشدار جهش سرانه خرید بازار — {label}

📊 سرانه خرید فعلی: {current_value:,.2f}
📉 میانگین {SARANE_KHARID_MA_DAYS} روزه: {baseline:,.2f}
✖️ نسبت: {ratio:,.2f} برابر
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def check_sarane_forosh_spike_alert(
    bot_token, chat_id, current_sarane_forosh, baseline, status, tz, now, commodity, label
):
    """
    عیناً هم‌الگوی check_sarane_kharid_spike_alert بالا، فقط برای سرانه فروش —
    وقتی سرانه فروش فعلی حداقل SARANE_FOROSH_SPIKE_MULTIPLIER برابر میانگین
    SARANE_KHARID_MA_DAYS روزه‌ش بشه، هشدار می‌ره. state-based، فقط موقع ورود
    به حالت «جهش» پیام می‌ره.

    current_sarane_forosh و baseline باید هر دو مثبت باشن (مثل سرانه خرید) —
    baseline از get_sarane_forosh_baseline میاد که خودش قبلاً علامتش رو
    نسبت به ستون شیت (که منفی ذخیره می‌شه) برگردونده مثبت.
    """
    status_key = f"{commodity}_sarane_forosh_spike"

    if baseline is None or baseline <= 0:
        logger.warning(
            f"⚠️ [{commodity}] بررسی جهش سرانه فروش رد شد — baseline نامعتبر است "
            f"(baseline={baseline}). فعلی: {current_sarane_forosh:,.2f}. "
            f"ممکن است تاریخچه‌ی کافی (حداقل {SARANE_KHARID_MA_MIN_DAYS} روز) در Sheets نباشد، "
            f"یا خواندن Gist/Sheets خطا داده باشد — لاگ‌های بالاتر را چک کن."
        )
        return False

    ratio = current_sarane_forosh / baseline
    logger.info(
        f"📊 [{commodity}] سرانه فروش: فعلی {current_sarane_forosh:,.2f} | "
        f"میانگین {SARANE_KHARID_MA_DAYS}روزه {baseline:,.2f} | "
        f"نسبت {ratio:.2f}× (آستانه {SARANE_FOROSH_SPIKE_MULTIPLIER:.1f}×)"
    )

    is_spike = current_sarane_forosh >= baseline * SARANE_FOROSH_SPIKE_MULTIPLIER

    if is_spike:
        if status[status_key] != "spike":
            send_sarane_forosh_spike_alert(
                bot_token, chat_id, current_sarane_forosh, baseline, tz, now, label
            )
            status[status_key] = "spike"
            logger.info(
                f"🚀 [{commodity}] جهش سرانه فروش: فعلی {current_sarane_forosh:,.2f} "
                f"≥ {SARANE_FOROSH_SPIKE_MULTIPLIER:.0f}× میانگین {baseline:,.2f}"
            )
            return True
        return False

    if status[status_key] != "normal":
        status[status_key] = "normal"
        return True

    return False


def send_sarane_forosh_spike_alert(bot_token, chat_id, current_value, baseline, tz, now, label):
    """ارسال هشدار جهش سرانه فروش بازار"""
    ratio = current_value / baseline if baseline else 0

    main_text = f"""
📉 هشدار جهش سرانه فروش بازار — {label}

📊 سرانه فروش فعلی: {current_value:,.2f}
📈 میانگین {SARANE_KHARID_MA_DAYS} روزه: {baseline:,.2f}
✖️ نسبت: {ratio:,.2f} برابر
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


# ════════════════════════════════════════════════════════════════
# 💹 هشدار ارزش معاملات — نسبت به میانگین ماهانه‌ی زنده (avg_monthly_value از Fund_df،
# دقیقاً همون فیلدی که در telegram_sender.py برای کپشن استفاده می‌شه، نه baseline
# جدا محاسبه‌شده) — دو سطح + رشد روز به روز + جهش ناگهانی بین دو خوانش پیاپی
# ════════════════════════════════════════════════════════════════

# سطوح به ترتیب صعودی — ترتیب مهمه چون موقع جهش چندسطحی، پیام‌ها به همین ترتیب
# (اول عبور از میانگین، بعد ۲برابر) ارسال می‌شن تا هیچ سطحی جا نمونه.
_TRADE_VALUE_LEVELS = [
    ("above_avg", TRADE_VALUE_ABOVE_AVG_MULTIPLIER, "send_trade_value_above_avg_alert"),
    ("spike_2x", TRADE_VALUE_SPIKE_MULTIPLIER, "send_trade_value_spike_alert"),
]
_TRADE_VALUE_LEVEL_ORDER = ["normal"] + [lvl[0] for lvl in _TRADE_VALUE_LEVELS]


def check_trade_value_alerts(bot_token, chat_id, current_trade_value, prev_trade_value,
                              monthly_avg, status, tz, now, commodity, label, same_day=None):
    """
    بررسی و ارسال هشدارهای ارزش معاملات نسبت به میانگین ماهانه‌ی زنده:
      ۱) عبور از میانگین ماهانه (سطح «above_avg»)
      ۲) عبور از ۲ برابر میانگین ماهانه (سطح «spike_2x»)
      ۳) جهش ناگهانی بین دو خوانش پیاپی، نسبت به میانگین ماهانه سنجیده می‌شه
         (نه یه عدد ریالی ثابت، چون مقیاس طلا/نقره فرق داره)، فقط در همون روز
         (same_day=True) مقایسه می‌شه — درست مثل تغییر شدید پول حقیقی.

    ۱ و ۲ حالت‌محورن (مثل جهش سرانه): فقط موقع ورود به سطح بالاتر پیام می‌ره،
    برگشت به زیر آستانه بی‌صدا ریست می‌شه. اگه در یک خوانش مستقیم از «normal»
    به «spike_2x» بپره، هر دو پیام («above_avg» و بعدش «spike_2x») به‌ترتیب
    ارسال می‌شن تا سابقه‌ی عبور از میانگین گم نشه.
    """
    status_key = f"{commodity}_trade_value_level"

    if monthly_avg is None or monthly_avg <= 0:
        logger.debug(
            f"[{commodity}] بررسی ارزش معاملات رد شد — avg_monthly_value نامعتبر است "
            f"({monthly_avg}). فعلی: {current_trade_value:,.0f}"
        )
        return False

    ratio = current_trade_value / monthly_avg
    logger.info(
        f"📊 [{commodity}] ارزش معاملات: فعلی {current_trade_value:,.0f} | "
        f"میانگین ماهانه {monthly_avg:,.0f} | نسبت {ratio:.2f}×"
    )

    current_level = "normal"
    for level_name, multiplier, _ in _TRADE_VALUE_LEVELS:
        if ratio >= multiplier:
            current_level = level_name  # صعودیه، پس بالاترین سطح برنده می‌مونه

    prev_level = status.get(status_key, "normal")
    status_changed = False
    sender_by_name = {
        "send_trade_value_above_avg_alert": send_trade_value_above_avg_alert,
        "send_trade_value_spike_alert": send_trade_value_spike_alert,
    }

    prev_idx = _TRADE_VALUE_LEVEL_ORDER.index(prev_level)
    current_idx = _TRADE_VALUE_LEVEL_ORDER.index(current_level)

    if current_idx > prev_idx:
        for level_name, _, sender_name in _TRADE_VALUE_LEVELS:
            level_idx = _TRADE_VALUE_LEVEL_ORDER.index(level_name)
            if prev_idx < level_idx <= current_idx:
                sender_by_name[sender_name](
                    bot_token, chat_id, current_trade_value, monthly_avg, tz, now, label
                )
        status[status_key] = current_level
        status_changed = True
        logger.info(f"📈 [{commodity}] ارزش معاملات وارد سطح «{current_level}» شد ({ratio:.2f}×)")

    elif current_idx < prev_idx:
        status[status_key] = current_level
        status_changed = True

    if prev_trade_value is not None and same_day is True:
        change = current_trade_value - prev_trade_value
        if change >= monthly_avg * TRADE_VALUE_SHARP_CHANGE_RATIO:
            send_trade_value_sharp_change_alert(
                bot_token, chat_id, prev_trade_value, current_trade_value,
                change, monthly_avg, tz, now, label,
            )

    return status_changed


def send_trade_value_above_avg_alert(bot_token, chat_id, current_value, monthly_avg, tz, now, label):
    """ارسال هشدار عبور ارزش معاملات از میانگین ماهانه"""
    ratio = current_value / monthly_avg if monthly_avg else 0

    main_text = f"""
📈 هشدار ارزش معاملات {label}

ارزش معاملات از میانگین ماهانه عبور کرد.
💰 ارزش معاملات فعلی: {current_value:,.1f} میلیارد تومان
📊 میانگین ماهانه: {monthly_avg:,.1f} میلیارد تومان
✖️ نسبت: {ratio:,.2f} برابر
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_trade_value_spike_alert(bot_token, chat_id, current_value, monthly_avg, tz, now, label):
    """ارسال هشدار جهش ارزش معاملات به ۲ برابر میانگین ماهانه"""
    ratio = current_value / monthly_avg if monthly_avg else 0

    main_text = f"""
🚀 هشدار جهش ارزش معاملات {label}

ارزش معاملات به {TRADE_VALUE_SPIKE_MULTIPLIER:.0f} برابر میانگین ماهانه رسید.
💰 ارزش معاملات فعلی: {current_value:,.1f} میلیارد تومان
📊 میانگین ماهانه: {monthly_avg:,.1f} میلیارد تومان
✖️ نسبت: {ratio:,.2f} برابر
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_trade_value_sharp_change_alert(bot_token, chat_id, prev_value, curr_value, change, monthly_avg, tz, now, label):
    """ارسال هشدار جهش ناگهانی ارزش معاملات بین دو خوانش پیاپی"""
    change_ratio = (change / monthly_avg * 100) if monthly_avg else 0

    main_text = f"""
🚨 جهش ناگهانی ارزش معاملات {label} 📈

⏱ افزایش در 1 دقیقه: {change:,.1f} میلیارد تومان ({change_ratio:,.0f}% میانگین ماهانه)
🔴 قبلی: {prev_value:,.1f} میلیارد تومان
🟢 فعلی: {curr_value:,.1f} میلیارد تومان
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


# ════════════════════════════════════════════════════════════════
# 📅 رشد ارزش معاملات نسبت به روز قبل (day-over-day)
# ════════════════════════════════════════════════════════════════


def get_previous_day_trade_value(commodity, today):
    """
    آخرین (پایانی) مقدار trade_value روز کاری قبل رو برمی‌گردونه — بدون کش،
    چون فقط یه خوندن سبک شیته (نه محاسبه‌ی سنگین روی تاریخچه‌ی بلند مثل baseline).

    Returns:
        float | None
    """
    try:
        rows = read_from_sheets(commodity, limit=400)
        if not rows:
            return None

        df = pd.DataFrame(rows, columns=STANDARD_HEADER)
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df["trade_value"] = pd.to_numeric(df["trade_value"], errors="coerce")
        df = df.dropna(subset=["timestamp", "trade_value"])
        df = df[df["timestamp"].dt.date < today]
        if df.empty:
            return None

        yesterday = df["timestamp"].dt.date.max()
        yesterday_rows = df[df["timestamp"].dt.date == yesterday].sort_values("timestamp")
        return float(yesterday_rows["trade_value"].iloc[-1])

    except Exception as e:
        logger.error(f"[{commodity}] خطا در خواندن ارزش معاملات روز قبل: {e}")
        return None


def check_trade_value_dod_growth_alert(bot_token, chat_id, current_trade_value, prev_day_trade_value,
                                        status, tz, now, commodity, label, same_day=None):
    """
    بررسی رشد ارزش معاملات امروز (تا این لحظه) نسبت به کل ارزش معاملات دیروز.
    وقتی رشد از TRADE_VALUE_DOD_GROWTH_THRESHOLD (پیش‌فرض ۵۰٪) بیشتر بشه، یه‌بار
    پیام می‌ره (حالت‌محور، مثل بقیه). چون trade_value هر روز از صفر شروع می‌شه،
    وضعیت با شروع هر روز جدید (same_day=False) خودکار ریست می‌شه تا فردا دوباره
    بشه هشدار داد.
    """
    status_key = f"{commodity}_trade_value_dod_growth"

    if same_day is False:
        status[status_key] = "normal"

    if prev_day_trade_value is None or prev_day_trade_value <= 0:
        logger.debug(f"[{commodity}] بررسی رشد روز به روز رد شد — ارزش معاملات دیروز نامعتبر است")
        return False

    growth = (current_trade_value - prev_day_trade_value) / prev_day_trade_value

    if growth >= TRADE_VALUE_DOD_GROWTH_THRESHOLD:
        if status.get(status_key, "normal") != "triggered":
            send_trade_value_dod_growth_alert(
                bot_token, chat_id, current_trade_value, prev_day_trade_value, growth, tz, now, label
            )
            status[status_key] = "triggered"
            logger.info(f"📅 [{commodity}] رشد روز به روز ارزش معاملات: {growth:+.0%}")
            return True
        return False

    if status.get(status_key, "normal") != "normal":
        status[status_key] = "normal"
        return True

    return False


def send_trade_value_dod_growth_alert(bot_token, chat_id, current_value, prev_day_value, growth, tz, now, label):
    """ارسال هشدار رشد ارزش معاملات نسبت به روز قبل"""
    main_text = f"""
📅 هشدار رشد ارزش معاملات {label}
📈 رشد نسبت به کل دیروز: {growth:+.0%}

💰 امروز (تاکنون): {current_value:,.0f} میلیارد تومان
📊 کل دیروز: {prev_day_value:,.0f} میلیارد تومان
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")



# ════════════════════════════════════════════════════════════════
# ارکستراسیون اصلی — یک‌بار به ازای هر کالا در main.py صدا زده می‌شود
# ════════════════════════════════════════════════════════════════


def check_and_send_alerts(
    commodity,
    bot_token,
    chat_id,
    data,
    dollar_prices,
    global_price,
    yesterday_close,
    global_price_yesterday,
    check_dollar=False,
):
    """
    بررسی و ارسال همه‌ی هشدارهای یک کالا (gold یا silver).

    check_dollar=True فقط باید در یکی از دو فراخوانی (مثلاً طلا) ست بشه،
    چون دلار داده‌ی مشترکه و نباید دوبار در هر ران هشدار بده.
    """
    if commodity not in THRESHOLDS:
        raise ValueError(f"کالای نامعتبر: {commodity}")

    label = COMMODITY_LABEL[commodity]
    bullion_key = BULLION_ASSET[commodity]
    th = THRESHOLDS[commodity]

    prev = get_previous_state_from_sheet(commodity)
    status = get_alert_status()

    current_dollar = dollar_prices.get("last_trade", 0) if dollar_prices else 0
    current_shams = (
        data["dfp"].loc[bullion_key, "close_price"]
        if bullion_key in data["dfp"].index
        else 0
    )
    current_ounce = global_price

    df_funds = data["Fund_df"]
    total_value = df_funds["value"].sum() if not df_funds.empty else 0
    current_ekhtelaf = (
        (df_funds["ekhtelaf_sarane"] * df_funds["value"]).sum() / total_value
        if total_value > 0
        else 0
    )
    current_bubble = (
        (df_funds["nominal_bubble"] * df_funds["value"]).sum() / total_value
        if total_value > 0
        else 0
    )
    current_pol = df_funds["pol_hagigi"].sum() if not df_funds.empty else 0
    current_sarane_kharid = (
        (df_funds["sarane_kharid"] * df_funds["value"]).sum() / total_value
        if total_value > 0
        else 0
    )
    current_sarane_forosh = (
        (df_funds["sarane_forosh"] * df_funds["value"]).sum() / total_value
        if total_value > 0
        else 0
    )

    changed = False
    bubble_status_changed = False
    pol_status_changed = False
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    # نوسان ۵ دقیقه‌ای دلار — فقط یک‌بار در کل ران
    if check_dollar and prev["dollar_price"] and prev["dollar_price"] > 0:
        change = (current_dollar - prev["dollar_price"]) / prev["dollar_price"] * 100
        if abs(change) >= ALERT_THRESHOLD_PERCENT["dollar"]:
            send_price_alert(bot_token, chat_id, "دلار", current_dollar, change, "تومان")

    # نوسان ۵ دقیقه‌ای شمش
    if prev["shams_price"] and prev["shams_price"] > 0:
        change = (current_shams - prev["shams_price"]) / prev["shams_price"] * 100
        if abs(change) >= ALERT_THRESHOLD_PERCENT[commodity]:
            shams_divisor = 10 if commodity == "silver" else 1
            shams_unit = "تومان" if commodity == "silver" else "ریال"
            send_price_alert(
                bot_token, chat_id, f"شمش {label}",
                current_shams / shams_divisor, change, shams_unit,
            )

    # نوسان ۵ دقیقه‌ای انس جهانی
    if prev["global_price"] and prev["global_price"] > 0:
        change = (current_ounce - prev["global_price"]) / prev["global_price"] * 100
        if abs(change) >= ALERT_THRESHOLD_PERCENT[commodity]:
            send_price_alert(
                bot_token, chat_id, f"اونس {label}", current_ounce, change,
                "دلار", is_ounce=True,
            )

    # تغییر شدید اختلاف سرانه
    if prev["ekhtelaf_sarane"] is not None:
        diff = current_ekhtelaf - prev["ekhtelaf_sarane"]
        if abs(diff) >= EKHTELAF_THRESHOLD:
            send_alert_ekhtelaf_fast(
                bot_token, chat_id, prev["ekhtelaf_sarane"], current_ekhtelaf,
                diff, label,
            )

    # هشدارهای حباب و پول حقیقی
    bubble_status_changed = check_bubble_alerts(
        bot_token, chat_id, current_bubble, prev["bubble_weighted"],
        status, tz, now, commodity, label,
    )
    if bubble_status_changed:
        changed = True

    pol_status_changed = check_pol_alerts(
        bot_token, chat_id, current_pol, prev["pol_hagigi"],
        status, tz, now, commodity, label, same_day=prev["same_day"],
    )
    if pol_status_changed:
        changed = True

    # هشدار سخت خرید/فروش
    hard_signal_changed = check_hard_signal_alert(
        bot_token, chat_id, current_bubble, current_pol, current_ekhtelaf,
        status, tz, now, commodity, label,
    )
    if hard_signal_changed:
        changed = True

    # هشدار جهش سرانه خرید بازار نسبت به میانگین چند روزه
    sarane_baseline = get_sarane_kharid_baseline(commodity)
    sarane_spike_changed = check_sarane_kharid_spike_alert(
        bot_token, chat_id, current_sarane_kharid, sarane_baseline,
        status, tz, now, commodity, label,
    )
    if sarane_spike_changed:
        changed = True

    # هشدار جهش سرانه فروش بازار نسبت به میانگین چند روزه
    sarane_forosh_baseline = get_sarane_forosh_baseline(commodity)
    sarane_forosh_spike_changed = check_sarane_forosh_spike_alert(
        bot_token, chat_id, current_sarane_forosh, sarane_forosh_baseline,
        status, tz, now, commodity, label,
    )
    if sarane_forosh_spike_changed:
        changed = True

    # هشدار ارزش معاملات: عبور از میانگین ماهانه، ۲برابر میانگین، جهش ناگهانی
    # میانگین ماهانه از همون فیلد زنده‌ی avg_monthly_value در Fund_df میاد —
    # دقیقاً همونی که telegram_sender.py برای کپشن استفاده می‌کنه، نه baseline جدا.
    total_avg_monthly = df_funds["avg_monthly_value"].sum() if not df_funds.empty else 0
    trade_value_changed = check_trade_value_alerts(
        bot_token, chat_id, total_value, prev["trade_value"], total_avg_monthly,
        status, tz, now, commodity, label, same_day=prev["same_day"],
    )
    if trade_value_changed:
        changed = True

    # هشدار رشد ارزش معاملات امروز نسبت به کل دیروز (>=۵۰٪)
    prev_day_trade_value = get_previous_day_trade_value(commodity, now.date())
    trade_value_dod_changed = check_trade_value_dod_growth_alert(
        bot_token, chat_id, total_value, prev_day_trade_value,
        status, tz, now, commodity, label, same_day=prev["same_day"],
    )
    if trade_value_dod_changed:
        changed = True

    # آستانه‌های قیمتی
    # نکته: SILVER_SHAMS_HIGH/LOW در config به تومان نوشته می‌شن، ولی current_shams
    # خام/ریال از dfp میاد (dfp تبدیل نمی‌شه) — پس فقط برای نقره، مقدار مقایسه رو
    # به تومان تبدیل می‌کنیم. طلا دست‌نخورده (خام/ریال) می‌مونه.
    shams_for_threshold = current_shams / 10 if commodity == "silver" else current_shams

    threshold_checks = [
        (f"شمش {label}", shams_for_threshold, th["shams_high"], th["shams_low"], f"{commodity}_shams"),
        (f"اونس {label}", current_ounce, th["ounce_high"], th["ounce_low"], f"{commodity}_ounce"),
    ]
    if check_dollar:
        threshold_checks.insert(0, ("دلار", current_dollar, DOLLAR_HIGH, DOLLAR_LOW, "dollar"))

    for asset, price, high, low, key in threshold_checks:
        if high is None or low is None:
            logger.debug(f"آستانه‌ی {asset} تنظیم نشده — رد شد")
            continue

        if price > high:
            if status[key] != "above":
                send_alert_threshold(asset, price, high, above=True,
                                      bot_token=bot_token, chat_id=chat_id)
                status[key] = "above"
                changed = True
        elif price < low:
            if status[key] != "below":
                send_alert_threshold(asset, price, low, above=False,
                                      bot_token=bot_token, chat_id=chat_id)
                status[key] = "below"
                changed = True
        else:
            if status[key] != "normal":
                status[key] = "normal"
                changed = True

    # هشدار قیمتی نمادهای صندوق
    fund_changed = check_fund_price_alerts(
        bot_token, chat_id, df_funds, status,
    )
    if fund_changed:
        changed = True

    # فیلتر آربیتراژ داینامیک — سوئیچ بین صندوق‌های هم‌کالا
    switch_changed = check_switch_alert(
        bot_token, chat_id, df_funds, status, tz, now, commodity, label,
    )
    if switch_changed:
        changed = True

    if changed or bubble_status_changed or pol_status_changed or sarane_spike_changed:
        save_alert_status(status)


# ════════════════════════════════════════════════════════════════
# حباب
# ════════════════════════════════════════════════════════════════


def check_bubble_alerts(bot_token, chat_id, current_bubble, prev_bubble,
                         status, tz, now, commodity, label):
    """بررسی و ارسال هشدارهای حباب - کراس صفر + تغییر شدید"""
    status_changed = False
    status_key = f"{commodity}_bubble"

    if current_bubble > 0:
        if status[status_key] != "positive":
            send_bubble_state_alert(bot_token, chat_id, current_bubble, "positive", tz, now, label)
            status[status_key] = "positive"
            status_changed = True
            logger.info(f"🟢 [{commodity}] حباب مثبت شد (کراس صفر): {current_bubble:+.2f}%")

    elif current_bubble < 0:
        if status[status_key] != "negative":
            send_bubble_state_alert(bot_token, chat_id, current_bubble, "negative", tz, now, label)
            status[status_key] = "negative"
            status_changed = True
            logger.info(f"🔴 [{commodity}] حباب منفی شد (کراس صفر): {current_bubble:+.2f}%")

    else:
        if status[status_key] != "normal":
            status[status_key] = "normal"
            status_changed = True
            logger.info(f"⚪ [{commodity}] حباب صفر است: {current_bubble:+.2f}%")

    if prev_bubble is not None:
        bubble_change = current_bubble - prev_bubble
        if abs(bubble_change) >= BUBBLE_SHARP_CHANGE_THRESHOLD:
            send_bubble_sharp_change_alert(
                bot_token, chat_id, prev_bubble, current_bubble, bubble_change, tz, now, label
            )

    return status_changed


def send_bubble_state_alert(bot_token, chat_id, bubble_value, state, tz, now, label):
    """ارسال هشدار کراس صفر حباب"""
    if state == "positive":
        dir_emoji, description = "🟢", "حباب مثبت شد"
    else:
        dir_emoji, description = "🔴", "حباب منفی شد"

    main_text = f"""
🫧 هشدار حباب {label} {dir_emoji}

{description}
💹 حباب فعلی: {bubble_value:+.2f}%
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_bubble_sharp_change_alert(bot_token, chat_id, prev_value, curr_value, change, tz, now, label):
    """ارسال هشدار تغییر شدید حباب"""
    direction = "افزایش" if change > 0 else "کاهش"
    dir_emoji = "📈" if change > 0 else "📉"
    change_text = f"{change:+.2f}%".replace("+-", "−")

    main_text = f"""
🚨 تغییر شدید حباب {label} {dir_emoji}

⏱ {direction} در 1 دقیقه: {change_text}
🔴 قبلی: {prev_value:+.2f}%
🟢 فعلی: {curr_value:+.2f}%
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


# ════════════════════════════════════════════════════════════════
# پول حقیقی
# ════════════════════════════════════════════════════════════════


def check_pol_alerts(bot_token, chat_id, current_pol, prev_pol, status, tz, now, commodity, label, same_day=None):
    """بررسی و ارسال هشدارهای پول حقیقی - کراس صفر + تغییر شدید (1 دقیقه، فقط همون روز)"""
    status_changed = False
    status_key = f"{commodity}_pol_hagigi"

    if current_pol > 0:
        if status[status_key] != "positive":
            send_pol_state_alert(bot_token, chat_id, current_pol, "positive", tz, now, label)
            status[status_key] = "positive"
            status_changed = True
            logger.info(f"🟢 [{commodity}] پول حقیقی مثبت شد: {current_pol:+,.0f} م.ت")

    elif current_pol < 0:
        if status[status_key] != "negative":
            send_pol_state_alert(bot_token, chat_id, current_pol, "negative", tz, now, label)
            status[status_key] = "negative"
            status_changed = True
            logger.info(f"🔴 [{commodity}] پول حقیقی منفی شد: {current_pol:+,.0f} م.ت")

    else:
        if status[status_key] != "normal":
            status[status_key] = "normal"
            status_changed = True
            logger.info(f"⚪ [{commodity}] پول حقیقی صفر است: {current_pol:,.0f} م.ت")

    if prev_pol is not None:
        try:
            if same_day is True:
                pol_change = current_pol - prev_pol
                if abs(pol_change) >= POL_SHARP_CHANGE_THRESHOLD:
                    send_pol_sharp_change_alert(
                        bot_token, chat_id, prev_pol, current_pol, pol_change, tz, now, label
                    )
            elif same_day is False:
                logger.debug(f"[{commodity}] پول حقیقی در روزهای مختلف - هشدار ارسال نمیشه")
        except Exception as e:
            logger.warning(f"[{commodity}] خطا در بررسی تاریخ پول حقیقی: {e}")

    return status_changed


def send_pol_state_alert(bot_token, chat_id, pol_value, state, tz, now, label):
    """ارسال هشدار کراس صفر ورود پول حقیقی"""
    if state == "positive":
        direction, dir_emoji, description = "مثبت", "🟢", "ورود پول حقیقی مثبت شد"
    else:
        direction, dir_emoji, description = "منفی", "🔴", "ورود پول حقیقی منفی شد"

    main_text = f"""
💸 هشدار ورود پول حقیقی {label} {dir_emoji}

{description}
💰 ورود پول حقیقی: {pol_value:+,.0f} میلیارد تومان
📊 وضعیت: {direction}
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_pol_sharp_change_alert(bot_token, chat_id, prev_value, curr_value, change, tz, now, label):
    """ارسال هشدار تغییر شدید ورود پول حقیقی"""
    direction = "ورود" if change > 0 else "خروج"
    dir_emoji = "📈" if change > 0 else "📉"
    change_text = f"{abs(change):,.0f}"

    main_text = f"""
🚨 تغییر شدید ورود پول حقیقی {label} {dir_emoji}

⏱ {direction} در 1 دقیقه: {change_text} میلیارد تومان
🔴 قبلی: {prev_value:+,.0f} م.ت
🟢 فعلی: {curr_value:+,.0f} م.ت
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


# ════════════════════════════════════════════════════════════════
# هشدار سخت خرید / سخت فروش
# ════════════════════════════════════════════════════════════════


def check_hard_signal_alert(bot_token, chat_id, current_bubble, current_pol,
                             current_ekhtelaf, status, tz, now, commodity, label):
    """
    بررسی و ارسال هشدار سخت خرید/فروش.

    سخت خرید: حباب > +HARD_SIGNAL_BUBBLE_THRESHOLD، پول حقیقی و اختلاف سرانه‌ی
    کل هر دو مثبت.
    سخت فروش: حباب < -HARD_SIGNAL_BUBBLE_THRESHOLD، پول حقیقی و اختلاف سرانه
    هر دو منفی.
    state-based (مثل حباب/پول حقیقی) — فقط موقع تغییر وضعیت پیام می‌ره.
    """
    status_changed = False
    status_key = f"{commodity}_hard_signal"

    if (
        current_bubble > HARD_SIGNAL_BUBBLE_THRESHOLD
        and current_pol > 0
        and current_ekhtelaf > 0
    ):
        if status[status_key] != "buy":
            send_hard_signal_alert(bot_token, chat_id, "buy", current_bubble,
                                    current_pol, current_ekhtelaf, tz, now, label)
            status[status_key] = "buy"
            status_changed = True
            logger.info(
                f"🟢 [{commodity}] هشدار سخت خرید: حباب {current_bubble:+.2f}% | "
                f"پول {current_pol:+,.0f} | اختلاف سرانه {current_ekhtelaf:+,.0f}"
            )

    elif (
        current_bubble < -HARD_SIGNAL_BUBBLE_THRESHOLD
        and current_pol < 0
        and current_ekhtelaf < 0
    ):
        if status[status_key] != "sell":
            send_hard_signal_alert(bot_token, chat_id, "sell", current_bubble,
                                    current_pol, current_ekhtelaf, tz, now, label)
            status[status_key] = "sell"
            status_changed = True
            logger.info(
                f"🔴 [{commodity}] هشدار سخت فروش: حباب {current_bubble:+.2f}% | "
                f"پول {current_pol:+,.0f} | اختلاف سرانه {current_ekhtelaf:+,.0f}"
            )

    else:
        if status[status_key] != "normal":
            status[status_key] = "normal"
            status_changed = True

    return status_changed


def send_hard_signal_alert(bot_token, chat_id, signal, bubble, pol, ekhtelaf, tz, now, label):
    """ارسال هشدار سخت خرید/فروش"""
    if signal == "buy":
        title, dir_emoji = "هشدار سخت خرید", "🟢"
    else:
        title, dir_emoji = "هشدار سخت فروش", "🔴"

    main_text = f"""
🚨 {title} — {label} {dir_emoji}

🫧 حباب: {bubble:+.2f}%
💸 ورود پول حقیقی: {pol:+,.0f} میلیارد تومان
📊 اختلاف سرانه: {ekhtelaf:+,.0f}
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


# ════════════════════════════════════════════════════════════════
# هشدار قیمتی نمادهای صندوق (FUND_PRICE_ALERTS در config)
# ════════════════════════════════════════════════════════════════


def check_fund_price_alerts(bot_token, chat_id, df_funds, status):
    """بررسی و ارسال هشدار سقف/کف قیمت برای نمادهای صندوق تنظیم‌شده در config."""
    status_changed = False

    if df_funds is None or df_funds.empty:
        return status_changed

    for symbol, thresholds in FUND_PRICE_ALERTS.items():
        high = thresholds.get("high")
        low = thresholds.get("low")
        key = f"fund_{symbol}"

        if symbol not in df_funds.index:
            logger.debug(f"⚠️ نماد صندوق '{symbol}' در Fund_df پیدا نشد — رد شد")
            continue

        price = df_funds.loc[symbol, "close_price"]

        if high is None or low is None:
            logger.debug(f"آستانه‌ی نماد '{symbol}' تنظیم نشده — رد شد")
            continue

        if price > high:
            if status[key] != "above":
                send_alert_threshold(symbol, price, high, above=True,
                                      bot_token=bot_token, chat_id=chat_id)
                status[key] = "above"
                status_changed = True
        elif price < low:
            if status[key] != "below":
                send_alert_threshold(symbol, price, low, above=False,
                                      bot_token=bot_token, chat_id=chat_id)
                status[key] = "below"
                status_changed = True
        else:
            if status[key] != "normal":
                status[key] = "normal"
                status_changed = True

    return status_changed


# ════════════════════════════════════════════════════════════════
# 🔄 فیلتر آربیتراژ داینامیک (سوئیچ بین صندوق‌های هم‌کالا)
# ════════════════════════════════════════════════════════════════


def check_switch_alert(bot_token, chat_id, df_funds, status, tz, now, commodity, label):
    """
    بررسی سیگنال سوئیچ بین صندوق‌های هم‌کالا (طلا با طلا، نقره با نقره).

    منطق: صندوق فعلی (CURRENT_HOLDING[commodity]) با بهترین صندوق (کمترین
    nominal_bubble) در بین SWITCH_TOP_N صندوق پرحجم‌تر مقایسه می‌شود. اگر
    صندوق فعلی خودش جزو top-N نباشد، جداگانه به مجموعه‌ی مقایسه اضافه می‌شود.

    سوئیچ فقط وقتی سیگنال می‌شود که هر دو شرط برقرار باشد:
      ۱) افزایش طلای واقعی (پس از کارمزد) از SWITCH_SAFETY_MARGIN بیشتر باشد.
      ۲) فیلتر بازگشت-به-میانگین (mean-reversion gate): صندوق فعلی نسبت به
         میانگین حباب ماهانه‌ی خودش گران‌تر باشد، و صندوق مقصد نسبت به
         میانگین حباب ماهانه‌ی خودش ارزان‌تر باشد. این تضمین می‌کند مقایسه
         فقط بین صندوق‌هایی انجام شود که هرکدام نسبت به رفتار عادی خودشان
         «غیرعادی» هستند — نه صرفاً یکی که همیشه ساختاری حباب کمتری دارد.
      اگر avg_monthly_bubble برای هرکدام از دو صندوق موجود نباشد (NaN)،
      به‌صورت محافظه‌کارانه سیگنال صادر نمی‌شود.

    state-based (مثل حباب/پول حقیقی) — فقط موقع تغییر وضعیت (صندوق
    پیشنهادی جدید یا رفع سیگنال) پیام می‌رود.
    """
    status_changed = False
    status_key = f"{commodity}_switch_signal"
    current_symbol = CURRENT_HOLDING.get(commodity)

    if not current_symbol:
        logger.debug(f"[{commodity}] CURRENT_HOLDING تنظیم نشده — چک سوئیچ رد شد")
        return status_changed

    if df_funds is None or df_funds.empty or current_symbol not in df_funds.index:
        logger.debug(f"[{commodity}] صندوق فعلی '{current_symbol}' در Fund_df پیدا نشد")
        return status_changed

    current_bubble = df_funds.loc[current_symbol, "nominal_bubble"]
    current_avg_bubble = df_funds.loc[current_symbol, "avg_monthly_bubble"]
    if pd.isna(current_bubble):
        logger.debug(f"[{commodity}] nominal_bubble برای '{current_symbol}' نامعتبر است")
        return status_changed

    # Fund_df از قبل بر اساس value نزولی مرتب است
    candidates = df_funds.head(SWITCH_TOP_N)
    if current_symbol not in candidates.index:
        candidates = pd.concat([candidates, df_funds.loc[[current_symbol]]])

    candidates = candidates[~candidates.index.duplicated(keep="first")]
    candidates = candidates.dropna(subset=["nominal_bubble"])
    if candidates.empty:
        return status_changed

    best_symbol = candidates["nominal_bubble"].idxmin()
    best_bubble = candidates.loc[best_symbol, "nominal_bubble"]
    best_avg_bubble = candidates.loc[best_symbol, "avg_monthly_bubble"]
    best_price = candidates.loc[best_symbol, "close_price"]

    # نسبت طلای واقعی بعد از سوئیچ به قبلش (دقیق، نه تقریب خطی):
    # gold_ratio = (1-fee_sell)(1-fee_buy) × (1+bubble_from/100)/(1+bubble_to/100)
    gold_gain_percent = (
        (1 - SWITCH_FEE_SELL) * (1 - SWITCH_FEE_BUY)
        * (1 + current_bubble / 100) / (1 + best_bubble / 100)
        - 1
    ) * 100

    gain_ok = gold_gain_percent > SWITCH_SAFETY_MARGIN * 100

    # فیلتر بازگشت-به-میانگین — نیازمند داده‌ی معتبر avg_monthly_bubble برای هر دو
    if pd.isna(current_avg_bubble) or pd.isna(best_avg_bubble):
        reversion_ok = False
        logger.debug(
            f"[{commodity}] avg_monthly_bubble ناقص برای '{current_symbol}' یا "
            f"'{best_symbol}' — فیلتر بازگشت‌به‌میانگین رد شد (محافظه‌کارانه)"
        )
    else:
        reversion_ok = (current_bubble > current_avg_bubble) and (best_bubble < best_avg_bubble)

    if best_symbol != current_symbol and gain_ok and reversion_ok:
        if status[status_key] != best_symbol:
            send_switch_alert(
                bot_token, chat_id, current_symbol, current_bubble,
                best_symbol, best_bubble, best_price, gold_gain_percent, tz, now, label,
            )
            status[status_key] = best_symbol
            status_changed = True
            logger.info(
                f"🔄 [{commodity}] سیگنال سوئیچ: {current_symbol} → {best_symbol} "
                f"| افزایش طلای واقعی: {gold_gain_percent:+.2f}% "
                f"| فعلی: حباب {current_bubble:+.2f}% (میانگین {current_avg_bubble:+.2f}%) "
                f"| مقصد: حباب {best_bubble:+.2f}% (میانگین {best_avg_bubble:+.2f}%)"
            )
    else:
        if status[status_key] != "normal":
            status[status_key] = "normal"
            status_changed = True

    return status_changed


def send_switch_alert(bot_token, chat_id, from_symbol, from_bubble,
                       to_symbol, to_bubble, to_price, gold_gain_percent, tz, now, label):
    """ارسال هشدار سیگنال سوئیچ بین دو صندوق هم‌کالا"""
    main_text = f"""
🔄 سیگنال سوئیچ صندوق {label}

📤 از: {from_symbol} (حباب {from_bubble:+.2f}%)
📥 به: {to_symbol} (حباب {to_bubble:+.2f}%)
💰 قیمت ورودی {to_symbol}: {to_price:,.0f} ریال
🪙 افزایش طلای واقعی (پس از کارمزد): {gold_gain_percent:+.2f}%
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


# ════════════════════════════════════════════════════════════════
# پیام‌های هشدار قیمتی عمومی
# ════════════════════════════════════════════════════════════════


def send_price_alert(bot_token, chat_id, asset_name, price, change_5min, unit="تومان", is_ounce=False):
    """ارسال هشدار نوسان قیمتی"""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    change_text = f"{change_5min:+.2f}%".replace("+-", "−")

    price_formatted = f"${price:,.2f}" if is_ounce else f"{int(round(price)):,} {unit}"

    main_text = f"🚨 هشدار نوسان {asset_name}\n\n💰 قیمت: {price_formatted}\n📊 تغییر نسبت به 1 دقیقه پیش: {change_text}"
    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_alert_ekhtelaf_fast(bot_token, chat_id, prev_val, curr_val, diff, label):
    """ارسال هشدار تغییر شدید اختلاف سرانه"""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    direction = "افزایش شدید (مثبت)" if diff > 0 else "کاهش شدید (منفی)"
    dir_emoji = "🟢" if diff > 0 else "🔴"
    diff_text = f"{diff:+.0f}".replace("+-", "−")
    prev_text = f"{prev_val:+,.0f}".replace("+-", "−")

    main_text = (
        f"🚨 هشدار اختلاف سرانه — {label}\n\n{dir_emoji} {direction}\n"
        f"⏱ تغییر: {diff_text} میلیون تومان\n📉 اختلاف سرانه قبلی: {prev_text} میلیون تومان"
    )
    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_alert_threshold(asset, price, threshold, above, bot_token, chat_id):
    """ارسال هشدار عبور از آستانه قیمتی"""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    direction = "بالای" if above else "زیر"
    dir_emoji = "📈" if above else "📉"

    # price_display/threshold_display فقط برای نمایشن — منطق مقایسه با مقدار خام (price/threshold) قبلاً انجام شده
    if asset == "دلار":
        unit, asset_emoji = "تومان", "💵"
        price_display, threshold_display = price, threshold
    elif asset == "شمش طلا":
        unit, asset_emoji = "ریال", "✨"
        price_display, threshold_display = price, threshold
    elif asset == "شمش نقره":
        unit, asset_emoji = "تومان", "⚪"
        price_display, threshold_display = price, threshold
    elif asset == "اونس طلا":
        unit, asset_emoji = "دلار", "🔆"
        price_display, threshold_display = price, threshold
    elif asset == "اونس نقره":
        unit, asset_emoji = "دلار", "🌕"
        price_display, threshold_display = price, threshold
    else:
        # نمادهای صندوق (FUND_PRICE_ALERTS) هم از همین مسیر رد می‌شوند
        unit, asset_emoji = "ریال", "📌"
        price_display, threshold_display = price, threshold

    is_ounce_asset = "اونس" in asset
    price_formatted = f"{price_display:,.2f}" if is_ounce_asset else f"{int(round(price_display)):,}"
    threshold_formatted = f"{threshold_display:,.2f}" if is_ounce_asset else f"{int(round(threshold_display)):,}"

    main_text = f"""
🔔 هشدار قیمتی {dir_emoji} {asset_emoji} {asset}

📈 قیمت به {direction} {threshold_formatted} رسید.
💰 قیمت فعلی: {price_formatted} {unit}
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_alert_message(bot_token, chat_id, caption):
    """ارسال پیام هشدار به تلگرام"""
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": caption, "parse_mode": "HTML"},
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            logger.info("✅ هشدار ارسال شد")
        elif response.status_code == 429:
            retry_after = response.json().get("parameters", {}).get("retry_after", 5)
            logger.warning(f"⚠️ Rate limit hit, waiting {retry_after}s")
            time.sleep(retry_after)
            return send_alert_message(bot_token, chat_id, caption)
        else:
            logger.warning(f"⚠️ ارسال هشدار با خطا: {response.status_code}")

    except Exception as e:
        logger.error(f"❌ خطا در ارسال هشدار: {e}")
