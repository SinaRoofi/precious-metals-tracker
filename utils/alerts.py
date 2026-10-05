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
    TRADE_VALUE_AVG_CROSS_BAND,
    DAY_MOVE_ALERT_PERCENT,
    DAY_MOVE_REPEAT_EVERY_STEP,
    DAY_MOVE_HYSTERESIS_PERCENT,
    DEFAULT_DOLLAR_PRICE,
    CURRENT_HOLDING,
    SWITCH_FEE_BUY,
    SWITCH_FEE_SELL,
    SWITCH_MIN_REAL_GAIN,
    SWITCH_DEST_BELOW_AVG_MARGIN,
    SWITCH_HYSTERESIS,
    SWITCH_REALERT_MIN_IMPROVEMENT,
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


def _pct_change(current, reference, skip_zero=False):
    """درصد تغییر current نسبت به reference؛ None اگه هرکدوم نامعتبر باشه.
    skip_zero=True: تغییر دقیقاً صفر هم None می‌شه (برای وقتی که مرجع جایگزین
    قیمت فعلی شده و تغییر صفر ساختگیه)."""
    try:
        if current is None or reference is None or pd.isna(current) or pd.isna(reference):
            return None
        if reference <= 0:
            return None
        pct = (current - reference) / reference * 100
        if skip_zero and abs(pct) < 1e-12:
            return None
        return float(pct)
    except (TypeError, ValueError):
        return None


def _weighted_avg_bubble(df_funds):
    """میانگین وزنی (به ارزش معاملات) avg_monthly_bubble؛ ردیف‌های ناقص کنار گذاشته
    می‌شن (هم از صورت، هم از وزن). None اگه چیزی برای محاسبه نباشه."""
    try:
        if df_funds is None or df_funds.empty or "avg_monthly_bubble" not in df_funds.columns:
            return None
        avg = pd.to_numeric(df_funds["avg_monthly_bubble"], errors="coerce")
        val = pd.to_numeric(df_funds["value"], errors="coerce")
        mask = avg.notna() & val.notna()
        weight = val[mask].sum()
        if weight <= 0:
            return None
        return float((avg[mask] * val[mask]).sum() / weight)
    except Exception:
        return None


def _avg_bubble_line(avg_bubble):
    """خط «میانگین ماهانه حباب» برای ته پیام‌ها؛ رشته‌ی خالی اگه مقدار نامعتبر باشه."""
    if avg_bubble is None or pd.isna(avg_bubble):
        return ""
    return f"\n📊 میانگین ماهانه حباب: {avg_bubble:+.2f}%"


def _fund_bubble_lines(row):
    """خطوط حباب و میانگین ماهانه‌ی حباب یک صندوق (برای پیام‌های مربوط به صندوق)."""
    try:
        bubble = pd.to_numeric(row.get("nominal_bubble"), errors="coerce")
        avg = pd.to_numeric(row.get("avg_monthly_bubble"), errors="coerce")
    except Exception:
        return ""
    if pd.isna(bubble):
        return ""
    if pd.isna(avg):
        return f"🫧 حباب: {bubble:+.2f}%"
    return f"🫧 حباب: {bubble:+.2f}% (میانگین ماهانه {avg:+.2f}%)"


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
        status[f"{c}_trade_value_avg_cross"] = "normal"
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


def _format_avg_lines(weekly_avg, monthly_avg, decimals=1):
    """
    خطوط میانگین هفتگی (۵ روزه) و ماهانه (۲۰ روزه) ارزش معاملات برای پیام‌های هشدار.
    مقدار نامعتبر (None / NaN / ≤۰) بی‌صدا حذف می‌شه تا پیام خراب نشه.
    """
    lines = []
    if weekly_avg is not None and pd.notna(weekly_avg) and weekly_avg > 0:
        lines.append(f"📆 میانگین هفتگی: {weekly_avg:,.{decimals}f} میلیارد تومان")
    if monthly_avg is not None and pd.notna(monthly_avg) and monthly_avg > 0:
        lines.append(f"📊 میانگین ماهانه: {monthly_avg:,.{decimals}f} میلیارد تومان")
    return "\n".join(lines)


def check_trade_value_alerts(bot_token, chat_id, current_trade_value, prev_trade_value,
                              monthly_avg, status, tz, now, commodity, label, same_day=None,
                              weekly_avg=None):
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
                    bot_token, chat_id, current_trade_value, monthly_avg, tz, now, label,
                    weekly_avg=weekly_avg,
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
                change, monthly_avg, tz, now, label, weekly_avg=weekly_avg,
            )

    return status_changed


def send_trade_value_above_avg_alert(bot_token, chat_id, current_value, monthly_avg, tz, now, label, weekly_avg=None):
    """ارسال هشدار عبور ارزش معاملات از میانگین ماهانه"""
    ratio = current_value / monthly_avg if monthly_avg else 0

    main_text = f"""
📈 هشدار ارزش معاملات {label}

ارزش معاملات از میانگین ماهانه عبور کرد.
💰 ارزش معاملات فعلی: {current_value:,.1f} میلیارد تومان
{_format_avg_lines(weekly_avg, monthly_avg)}
✖️ نسبت: {ratio:,.2f} برابر
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_trade_value_spike_alert(bot_token, chat_id, current_value, monthly_avg, tz, now, label, weekly_avg=None):
    """ارسال هشدار جهش ارزش معاملات به ۲ برابر میانگین ماهانه"""
    ratio = current_value / monthly_avg if monthly_avg else 0

    main_text = f"""
🚀 هشدار جهش ارزش معاملات {label}

ارزش معاملات به {TRADE_VALUE_SPIKE_MULTIPLIER:.0f} برابر میانگین ماهانه رسید.
💰 ارزش معاملات فعلی: {current_value:,.1f} میلیارد تومان
{_format_avg_lines(weekly_avg, monthly_avg)}
✖️ نسبت: {ratio:,.2f} برابر
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_trade_value_sharp_change_alert(bot_token, chat_id, prev_value, curr_value, change, monthly_avg, tz, now, label, weekly_avg=None):
    """ارسال هشدار جهش ناگهانی ارزش معاملات بین دو خوانش پیاپی"""
    change_ratio = (change / monthly_avg * 100) if monthly_avg else 0

    main_text = f"""
🚨 جهش ناگهانی ارزش معاملات {label} 📈

⏱ افزایش در 1 دقیقه: {change:,.1f} میلیارد تومان ({change_ratio:,.0f}% میانگین ماهانه)
🔴 قبلی: {prev_value:,.1f} میلیارد تومان
🟢 فعلی: {curr_value:,.1f} میلیارد تومان
{_format_avg_lines(weekly_avg, monthly_avg)}
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
                                        status, tz, now, commodity, label, same_day=None,
                                        weekly_avg=None, monthly_avg=None):
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
                bot_token, chat_id, current_trade_value, prev_day_trade_value, growth, tz, now, label,
                weekly_avg=weekly_avg, monthly_avg=monthly_avg,
            )
            status[status_key] = "triggered"
            logger.info(f"📅 [{commodity}] رشد روز به روز ارزش معاملات: {growth:+.0%}")
            return True
        return False

    if status.get(status_key, "normal") != "normal":
        status[status_key] = "normal"
        return True

    return False


def send_trade_value_dod_growth_alert(bot_token, chat_id, current_value, prev_day_value, growth, tz, now, label,
                                       weekly_avg=None, monthly_avg=None):
    """ارسال هشدار رشد ارزش معاملات نسبت به روز قبل"""
    main_text = f"""
📅 هشدار رشد ارزش معاملات {label}
📈 رشد نسبت به کل دیروز: {growth:+.0%}

💰 امروز (تاکنون): {current_value:,.0f} میلیارد تومان
📊 کل دیروز: {prev_day_value:,.0f} میلیارد تومان
{_format_avg_lines(weekly_avg, monthly_avg, decimals=0)}
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")



# ════════════════════════════════════════════════════════════════
# 📊 شکست میانگین ماهانه‌ی ارزش معاملات توسط میانگین هفتگی (۵ روزه)
# ════════════════════════════════════════════════════════════════


def check_trade_value_avg_cross_alert(bot_token, chat_id, weekly_avg, monthly_avg, current_value,
                                       status, tz, now, commodity, label):
    """
    وقتی میانگین ۵ روزه‌ی ارزش معاملات از میانگین ماهانه (۲۰ روزه) به بالا یا پایین
    عبور کنه، یه‌بار پیام می‌ره (حالت‌محور: "above" / "below").

    ضد پیام کاذب:
      - اولین مشاهده (state=normal) فقط ثبت می‌شه و پیام نمی‌ره، چون عبوری دیده نشده.
      - فاصله‌ی ضد نوسان TRADE_VALUE_AVG_CROSS_BAND: بین (1-band) و (1+band) وضعیت قبلی
        حفظ می‌شه تا چرخیدن دور برابری، پیام تکراری نسازه.
    """
    status_key = f"{commodity}_trade_value_avg_cross"

    def _valid(v):
        return v is not None and pd.notna(v) and v > 0

    if not (_valid(weekly_avg) and _valid(monthly_avg)):
        logger.debug(
            f"[{commodity}] بررسی شکست میانگین ارزش معاملات رد شد — "
            f"هفتگی={weekly_avg} ماهانه={monthly_avg}"
        )
        return False

    ratio = weekly_avg / monthly_avg
    if ratio >= 1 + TRADE_VALUE_AVG_CROSS_BAND:
        side = "above"
    elif ratio <= 1 - TRADE_VALUE_AVG_CROSS_BAND:
        side = "below"
    else:
        return False  # داخل فاصله‌ی ضد نوسان؛ وضعیت قبلی حفظ می‌شه

    prev = status.get(status_key, "normal")
    if side == prev:
        return False

    status[status_key] = side
    if prev == "normal":
        logger.info(f"📌 [{commodity}] وضعیت اولیه‌ی میانگین ۵ روزه نسبت به ماهانه: {side} ({ratio:.2f}×)")
        return True

    send_trade_value_avg_cross_alert(
        bot_token, chat_id, side, weekly_avg, monthly_avg, current_value, tz, now, label,
    )
    logger.info(f"📊 [{commodity}] میانگین ۵ روزه ارزش معاملات {side} میانگین ماهانه شد ({ratio:.2f}×)")
    return True


def send_trade_value_avg_cross_alert(bot_token, chat_id, side, weekly_avg, monthly_avg,
                                      current_value, tz, now, label):
    """ارسال هشدار شکست میانگین ماهانه توسط میانگین ۵ روزه‌ی ارزش معاملات"""
    ratio = weekly_avg / monthly_avg if monthly_avg else 0
    if side == "above":
        title = f"📈 میانگین ارزش معاملات {label} — شکست رو به بالا 🟢"
        desc = "میانگین ۵ روزه از میانگین ماهانه عبور کرد (رو به بالا)."
    else:
        title = f"📉 میانگین ارزش معاملات {label} — شکست رو به پایین 🔴"
        desc = "میانگین ۵ روزه به زیر میانگین ماهانه رفت (رو به پایین)."

    lines = [
        title, "", desc,
        f"📆 میانگین ۵ روزه: {weekly_avg:,.1f} میلیارد تومان",
        f"📊 میانگین ماهانه: {monthly_avg:,.1f} میلیارد تومان",
        f"✖️ نسبت ۵ روزه به ماهانه: {ratio:,.2f} برابر",
    ]
    if current_value is not None and pd.notna(current_value) and current_value > 0:
        lines.append(f"💰 ارزش معاملات امروز (تاکنون): {current_value:,.1f} میلیارد تومان")

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, "\n".join(lines) + f"\n{footer}")

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
    current_avg_bubble = _weighted_avg_bubble(df_funds)
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
            send_price_alert(
                bot_token, chat_id, "دلار", current_dollar, change, "تومان",
                change_vs_yesterday=_pct_change(current_dollar, yesterday_close, skip_zero=True),
            )

    # نوسان ۵ دقیقه‌ای شمش
    if prev["shams_price"] and prev["shams_price"] > 0:
        change = (current_shams - prev["shams_price"]) / prev["shams_price"] * 100
        if abs(change) >= ALERT_THRESHOLD_PERCENT[commodity]:
            shams_divisor = 10 if commodity == "silver" else 1
            shams_unit = "تومان" if commodity == "silver" else "ریال"
            shams_vs_yesterday = None
            if bullion_key in data["dfp"].index:
                v = data["dfp"].loc[bullion_key, "close_price_change_percent"]
                shams_vs_yesterday = float(v) if pd.notna(v) else None
            send_price_alert(
                bot_token, chat_id, f"شمش {label}",
                current_shams / shams_divisor, change, shams_unit,
                change_vs_yesterday=shams_vs_yesterday,
            )

    # نوسان ۵ دقیقه‌ای انس جهانی
    if prev["global_price"] and prev["global_price"] > 0:
        change = (current_ounce - prev["global_price"]) / prev["global_price"] * 100
        if abs(change) >= ALERT_THRESHOLD_PERCENT[commodity]:
            send_price_alert(
                bot_token, chat_id, f"اونس {label}", current_ounce, change,
                "دلار", is_ounce=True,
                change_vs_yesterday=_pct_change(current_ounce, global_price_yesterday),
            )

    # 🌅 حرکت نسبت به قیمت اول روز: دلار، شمش، و صندوق خودت (CURRENT_HOLDING)
    day_assets = []
    if (check_dollar and current_dollar and pd.notna(current_dollar)
            and current_dollar != DEFAULT_DOLLAR_PRICE):  # قیمت پیش‌فرضِ fallback ساختگیه
        day_assets.append({
            "key": "dollar", "name": "دلار", "price": current_dollar,
            "unit": "تومان", "percent_key": "dollar",
        })
    if current_shams and pd.notna(current_shams):
        shams_div = 10 if commodity == "silver" else 1
        day_assets.append({
            "key": f"{commodity}_shams", "name": f"شمش {label}",
            "price": current_shams / shams_div,
            "unit": "تومان" if commodity == "silver" else "ریال",
            "percent_key": commodity,
        })
    holding = CURRENT_HOLDING.get(commodity)
    if holding and not df_funds.empty and holding in df_funds.index:
        holding_row = df_funds.loc[holding]
        day_assets.append({
            "key": f"{commodity}_fund_{holding}", "name": f"صندوق {holding}",
            "price": holding_row["close_price"], "unit": "ریال",
            "percent_key": commodity, "extra": _fund_bubble_lines(holding_row),
        })
    if check_day_move_alerts(bot_token, chat_id, day_assets, status, now):
        changed = True

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
        status, tz, now, commodity, label, avg_bubble=current_avg_bubble,
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
        status, tz, now, commodity, label, avg_bubble=current_avg_bubble,
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
    # میانگین هفتگی (۵ روزه) هم از همون Fund_df زنده؛ فقط برای نمایش تو پیام‌هاست،
    # در منطق آستانه‌ها نقشی نداره
    total_avg_weekly = (
        df_funds["avg_weekly_value"].sum()
        if not df_funds.empty and "avg_weekly_value" in df_funds.columns else None
    )
    trade_value_changed = check_trade_value_alerts(
        bot_token, chat_id, total_value, prev["trade_value"], total_avg_monthly,
        status, tz, now, commodity, label, same_day=prev["same_day"],
        weekly_avg=total_avg_weekly,
    )
    if trade_value_changed:
        changed = True

    # شکست میانگین ماهانه توسط میانگین ۵ روزه‌ی ارزش معاملات (به بالا/پایین)
    trade_cross_changed = check_trade_value_avg_cross_alert(
        bot_token, chat_id, total_avg_weekly, total_avg_monthly, total_value,
        status, tz, now, commodity, label,
    )
    if trade_cross_changed:
        changed = True

    # هشدار رشد ارزش معاملات امروز نسبت به کل دیروز (>=۵۰٪)
    prev_day_trade_value = get_previous_day_trade_value(commodity, now.date())
    trade_value_dod_changed = check_trade_value_dod_growth_alert(
        bot_token, chat_id, total_value, prev_day_trade_value,
        status, tz, now, commodity, label, same_day=prev["same_day"],
        weekly_avg=total_avg_weekly, monthly_avg=total_avg_monthly,
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
                         status, tz, now, commodity, label, avg_bubble=None):
    """بررسی و ارسال هشدارهای حباب - کراس صفر + تغییر شدید"""
    status_changed = False
    status_key = f"{commodity}_bubble"

    if current_bubble > 0:
        if status[status_key] != "positive":
            send_bubble_state_alert(bot_token, chat_id, current_bubble, "positive", tz, now, label,
                                    avg_bubble=avg_bubble)
            status[status_key] = "positive"
            status_changed = True
            logger.info(f"🟢 [{commodity}] حباب مثبت شد (کراس صفر): {current_bubble:+.2f}%")

    elif current_bubble < 0:
        if status[status_key] != "negative":
            send_bubble_state_alert(bot_token, chat_id, current_bubble, "negative", tz, now, label,
                                    avg_bubble=avg_bubble)
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
                bot_token, chat_id, prev_bubble, current_bubble, bubble_change, tz, now, label,
                avg_bubble=avg_bubble,
            )

    return status_changed


def send_bubble_state_alert(bot_token, chat_id, bubble_value, state, tz, now, label, avg_bubble=None):
    """ارسال هشدار کراس صفر حباب"""
    if state == "positive":
        dir_emoji, description = "🟢", "حباب مثبت شد"
    else:
        dir_emoji, description = "🔴", "حباب منفی شد"

    main_text = f"""
🫧 هشدار حباب {label} {dir_emoji}

{description}
💹 حباب فعلی: {bubble_value:+.2f}%{_avg_bubble_line(avg_bubble)}
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_bubble_sharp_change_alert(bot_token, chat_id, prev_value, curr_value, change, tz, now, label, avg_bubble=None):
    """ارسال هشدار تغییر شدید حباب"""
    direction = "افزایش" if change > 0 else "کاهش"
    dir_emoji = "📈" if change > 0 else "📉"
    change_text = f"{change:+.2f}%".replace("+-", "−")

    main_text = f"""
🚨 تغییر شدید حباب {label} {dir_emoji}

⏱ {direction} در 1 دقیقه: {change_text}
🔴 قبلی: {prev_value:+.2f}%
🟢 فعلی: {curr_value:+.2f}%{_avg_bubble_line(avg_bubble)}
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
                             current_ekhtelaf, status, tz, now, commodity, label, avg_bubble=None):
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
                                    current_pol, current_ekhtelaf, tz, now, label,
                                    avg_bubble=avg_bubble)
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
                                    current_pol, current_ekhtelaf, tz, now, label,
                                    avg_bubble=avg_bubble)
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


def send_hard_signal_alert(bot_token, chat_id, signal, bubble, pol, ekhtelaf, tz, now, label, avg_bubble=None):
    """ارسال هشدار سخت خرید/فروش"""
    if signal == "buy":
        title, dir_emoji = "هشدار سخت خرید", "🟢"
    else:
        title, dir_emoji = "هشدار سخت فروش", "🔴"

    main_text = f"""
🚨 {title} — {label} {dir_emoji}

🫧 حباب: {bubble:+.2f}%{_avg_bubble_line(avg_bubble)}
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
                                      bot_token=bot_token, chat_id=chat_id,
                                      extra_lines=_fund_bubble_lines(df_funds.loc[symbol]))
                status[key] = "above"
                status_changed = True
        elif price < low:
            if status[key] != "below":
                send_alert_threshold(symbol, price, low, above=False,
                                      bot_token=bot_token, chat_id=chat_id,
                                      extra_lines=_fund_bubble_lines(df_funds.loc[symbol]))
                status[key] = "below"
                status_changed = True
        else:
            if status[key] != "normal":
                status[key] = "normal"
                status_changed = True

    return status_changed


# ════════════════════════════════════════════════════════════════
# 🌅 حرکت نسبت به قیمت اول روز (دلار، شمش، صندوق خودت)
# ════════════════════════════════════════════════════════════════


def check_day_move_alerts(bot_token, chat_id, assets, status, now):
    """
    برای هر دارایی، قیمت فعلی رو با «قیمت اول روز» مقایسه می‌کنه و با هر پله‌ی
    DAY_MOVE_ALERT_PERCENT (بالا یا پایین) پیام می‌ده.

    assets: لیست دیکشنری با کلیدهای key, name, price, unit, percent_key و اختیاری extra.
    قیمت اول روز = اولین قیمت معتبری که ربات اون روز می‌بینه؛ تو status["day_open"]
    (همون Gist وضعیت هشدارها) ذخیره می‌شه و با عوض‌شدن روز خودکار پاک می‌شه.

    منطق پله (level = int(تغییر٪ / پله)، یعنی بریده به سمت صفر):
      - پیام: وقتی از level=0 خارج بشه، جهتش عوض بشه، یا (اگه DAY_MOVE_REPEAT_EVERY_STEP)
        پله‌ی دورتری از اولین‌بار برسه.
      - ریست بی‌صدا: فقط وقتی حرکت به‌اندازه‌ی DAY_MOVE_HYSTERESIS_PERCENT پایین‌تر از
        پله‌ی اعلام‌شده برگرده (ضد اسپم دور مرز).
    خروجی: True اگه status تغییر کرده (باید ذخیره بشه).
    """
    changed = False
    store = status.get("day_open")
    if not isinstance(store, dict):
        store = {}
        status["day_open"] = store

    today = now.strftime("%Y-%m-%d")
    for k in [k for k, v in store.items() if not isinstance(v, dict) or v.get("date") != today]:
        del store[k]  # قیمت‌های اول روزِ روزهای قبل
        changed = True

    for asset in assets:
        try:
            price = asset["price"]
            if price is None or pd.isna(price) or price <= 0:
                continue
            step = DAY_MOVE_ALERT_PERCENT.get(asset["percent_key"])
            if not step:
                continue

            rec = store.get(asset["key"])
            if rec is None:
                store[asset["key"]] = {"date": today, "open": float(price), "level": 0}
                changed = True
                logger.info(f"🌅 قیمت اول روز {asset['name']} ثبت شد: {price:,.0f}")
                continue

            open_price = rec.get("open")
            if not open_price or open_price <= 0:
                continue

            change = (price - open_price) / open_price * 100
            level_now = int(change / step)
            last = int(rec.get("level", 0))
            new_last = last
            send = False

            if level_now != 0 and (
                last == 0
                or level_now * last < 0
                or (DAY_MOVE_REPEAT_EVERY_STEP and abs(level_now) > abs(last))
            ):
                send, new_last = True, level_now
            elif (
                last != 0
                and level_now * last >= 0
                and abs(level_now) < abs(last)
                and abs(change) < abs(last) * step - DAY_MOVE_HYSTERESIS_PERCENT
            ):
                new_last = level_now  # برگشت قابل‌توجه؛ ریست بی‌صدا

            if send:
                send_day_move_alert(
                    bot_token, chat_id, asset["name"], price, open_price, change,
                    asset["unit"], now, extra_lines=asset.get("extra"),
                )
                logger.info(f"🌅 {asset['name']}: {change:+.2f}% نسبت به قیمت اول روز")

            if new_last != last:
                rec["level"] = new_last
                changed = True
        except Exception as e:
            logger.warning(f"⚠️ خطا در هشدار حرکت روزانه‌ی {asset.get('name')}: {e}")

    return changed


def send_day_move_alert(bot_token, chat_id, name, price, open_price, change, unit, now,
                         extra_lines=None):
    """ارسال هشدار حرکت نسبت به قیمت اول روز"""
    up = change > 0
    arrow, dot = ("📈", "🟢") if up else ("📉", "🔴")
    diff = price - open_price

    lines = [
        f"{arrow} هشدار حرکت {name} نسبت به قیمت اول روز {dot}",
        "",
        f"💰 قیمت فعلی: {int(round(price)):,} {unit}",
        f"🌅 قیمت اول روز: {int(round(open_price)):,} {unit}",
        f"📊 تغییر: {change:+.2f}% ({diff:+,.0f} {unit})",
    ]
    if extra_lines:
        lines.append(extra_lines)

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, "\n".join(lines) + f"\n{footer}")

# ════════════════════════════════════════════════════════════════
# 🔄 فیلتر آربیتراژ داینامیک (سوئیچ بین صندوق‌های هم‌کالا)
# ════════════════════════════════════════════════════════════════


def _switch_gain_percent(from_bubble, to_bubble):
    """
    افزایش طلای واقعی (٪) پس از کارمزد رفت‌وبرگشت (دقیق، نه تقریب خطی):
    gold_ratio = (1-fee_sell)(1-fee_buy) × (1+bubble_from/100)/(1+bubble_to/100)
    """
    ratio = (
        (1 - SWITCH_FEE_SELL) * (1 - SWITCH_FEE_BUY)
        * (1 + from_bubble / 100) / (1 + to_bubble / 100)
    )
    return (ratio - 1) * 100


def _switch_conditions_met(cur_bubble, cur_avg, to_bubble, to_avg, slack=0.0):
    """
    سه شرط هم‌زمان برای سیگنال سوئیچ:
      ۱) سود واقعی پس از کارمزد > SWITCH_MIN_REAL_GAIN
      ۲) حباب صندوق فعلی بالای میانگین ماهانه‌ی خودش
      ۳) حباب صندوق مقصد حداقل SWITCH_DEST_BELOW_AVG_MARGIN زیر میانگین ماهانه‌ی خودش
    slack=0 برای ورود؛ slack=SWITCH_HYSTERESIS برای نگه‌داشتن سیگنال فعال (هر سه آستانه
    به‌اندازه‌ی slack شل می‌شن). اگه میانگین هرکدام NaN باشه محافظه‌کارانه False.
    """
    if pd.isna(cur_avg) or pd.isna(to_avg):
        return False
    gain = _switch_gain_percent(cur_bubble, to_bubble)
    return (
        gain > SWITCH_MIN_REAL_GAIN * 100 - slack
        and cur_bubble > cur_avg - slack
        and to_bubble < to_avg - (SWITCH_DEST_BELOW_AVG_MARGIN - slack)
    )


def check_switch_alert(bot_token, chat_id, df_funds, status, tz, now, commodity, label):
    """
    بررسی سیگنال سوئیچ بین صندوق‌های هم‌کالا (طلا با طلا، نقره با نقره).

    صندوق فعلی (CURRENT_HOLDING[commodity]) با هر یک از SWITCH_TOP_N صندوق پرحجم‌تر
    (به‌جز خودش) مقایسه می‌شود. مقصدی «واجد شرایط» است که هر سه شرط
    _switch_conditions_met را داشته باشد. بین واجدین شرایط، کم‌حباب‌ترین (= بیشترین
    سود واقعی) انتخاب می‌شود — نه صرفاً کم‌حباب‌ترین کل صندوق‌ها که ممکن است ساختاری
    همیشه ارزان باشد و فیلتر میانگین را رد کند و جلوی مقصد درست را بگیرد.

    ضد اسپم (state-based + هیسترزیس):
      - پیام فقط موقع تغییر وضعیت می‌رود (سیگنال جدید یا رفع سیگنال).
      - سیگنال فعال تا وقتی شروط شل‌شده‌ی خروج (slack=SWITCH_HYSTERESIS) برقرارند پاک
        نمی‌شود، تا نوسان دور مرز ۱٪ پیام تکراری نسازد.
      - اگه سیگنال فعاله و مقصد دیگری بهتر شد، فقط وقتی پیام جدید می‌رود که سودش
        حداقل SWITCH_REALERT_MIN_IMPROVEMENT از مقصد فعال بیشتر باشد.
    """
    status_key = f"{commodity}_switch_signal"
    current_symbol = CURRENT_HOLDING.get(commodity)

    if not current_symbol:
        logger.debug(f"[{commodity}] CURRENT_HOLDING تنظیم نشده — چک سوئیچ رد شد")
        return False

    if df_funds is None or df_funds.empty or current_symbol not in df_funds.index:
        logger.debug(f"[{commodity}] صندوق فعلی '{current_symbol}' در Fund_df پیدا نشد")
        return False

    current_bubble = df_funds.loc[current_symbol, "nominal_bubble"]
    current_avg_bubble = df_funds.loc[current_symbol, "avg_monthly_bubble"]
    if pd.isna(current_bubble):
        logger.debug(f"[{commodity}] nominal_bubble برای '{current_symbol}' نامعتبر است")
        return False

    # Fund_df از قبل بر اساس value نزولی مرتب است
    top = df_funds.head(SWITCH_TOP_N)
    top = top[~top.index.duplicated(keep="first")]
    dests = top.drop(index=current_symbol, errors="ignore").dropna(subset=["nominal_bubble"])

    qualified = [
        sym for sym, row in dests.iterrows()
        if _switch_conditions_met(
            current_bubble, current_avg_bubble,
            row["nominal_bubble"], row["avg_monthly_bubble"],
        )
    ]
    best_symbol = dests.loc[qualified, "nominal_bubble"].idxmin() if qualified else None

    active = status.get(status_key, "normal")
    active_ok = False
    active_gain = None
    if active != "normal" and active in dests.index:
        row = dests.loc[active]
        active_ok = _switch_conditions_met(
            current_bubble, current_avg_bubble,
            row["nominal_bubble"], row["avg_monthly_bubble"],
            slack=SWITCH_HYSTERESIS,
        )
        active_gain = _switch_gain_percent(current_bubble, row["nominal_bubble"])

    new_state = "normal"
    if best_symbol is not None:
        best_gain = _switch_gain_percent(current_bubble, dests.loc[best_symbol, "nominal_bubble"])
        keep_active = (
            active_ok
            and active != best_symbol
            and best_gain - active_gain < SWITCH_REALERT_MIN_IMPROVEMENT
        )
        new_state = active if keep_active else best_symbol
    elif active_ok:
        new_state = active

    if new_state == active:
        if new_state == "normal":
            logger.debug(
                f"[{commodity}] سوئیچ: فعلی {current_symbol} حباب {current_bubble:+.2f}% "
                f"(میانگین {current_avg_bubble:+.2f}%) — مقصد واجد شرایط پیدا نشد"
            )
        return False

    status[status_key] = new_state
    if new_state != "normal":
        row = dests.loc[new_state]
        gain = _switch_gain_percent(current_bubble, row["nominal_bubble"])
        send_switch_alert(
            bot_token, chat_id, current_symbol, current_bubble, current_avg_bubble,
            new_state, row["nominal_bubble"], row["avg_monthly_bubble"],
            row["close_price"], gain, tz, now, label,
        )
        logger.info(
            f"🔄 [{commodity}] سیگنال سوئیچ: {current_symbol} → {new_state} "
            f"| افزایش طلای واقعی: {gain:+.2f}% "
            f"| فعلی: حباب {current_bubble:+.2f}% (میانگین {current_avg_bubble:+.2f}%) "
            f"| مقصد: حباب {row['nominal_bubble']:+.2f}% (میانگین {row['avg_monthly_bubble']:+.2f}%)"
        )
    return True


def send_switch_alert(bot_token, chat_id, from_symbol, from_bubble, from_avg_bubble,
                       to_symbol, to_bubble, to_avg_bubble, to_price,
                       gold_gain_percent, tz, now, label):
    """ارسال هشدار سیگنال سوئیچ بین دو صندوق هم‌کالا"""
    main_text = f"""
🔄 سیگنال سوئیچ صندوق {label}

📤 از: {from_symbol} (حباب {from_bubble:+.2f}% | میانگین ماهانه {from_avg_bubble:+.2f}%)
📥 به: {to_symbol} (حباب {to_bubble:+.2f}% | میانگین ماهانه {to_avg_bubble:+.2f}%)
💰 قیمت ورودی {to_symbol}: {to_price:,.0f} ریال
🪙 افزایش طلای واقعی (پس از کارمزد): {gold_gain_percent:+.2f}%
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


# ════════════════════════════════════════════════════════════════
# پیام‌های هشدار قیمتی عمومی
# ════════════════════════════════════════════════════════════════


def send_price_alert(bot_token, chat_id, asset_name, price, change_5min, unit="تومان", is_ounce=False,
                     change_vs_yesterday=None):
    """ارسال هشدار نوسان قیمتی"""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    change_text = f"{change_5min:+.2f}%".replace("+-", "−")

    price_formatted = f"${price:,.2f}" if is_ounce else f"{int(round(price)):,} {unit}"

    main_text = f"🚨 هشدار نوسان {asset_name}\n\n💰 قیمت: {price_formatted}\n📊 تغییر نسبت به 1 دقیقه پیش: {change_text}"
    if change_vs_yesterday is not None and pd.notna(change_vs_yesterday):
        main_text += f"\n📅 تغییر نسبت به دیروز: {change_vs_yesterday:+.2f}%"
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


def send_alert_threshold(asset, price, threshold, above, bot_token, chat_id, extra_lines=None):
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
    if extra_lines:
        main_text += f"\n{extra_lines}"

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
