# utils/alerts.py

import json
import logging
import time
import requests
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
    GIST_ID,
    GIST_TOKEN,
    ALERT_STATUS_FILE,
    ALERT_CHANNEL_HANDLE,
    REQUEST_TIMEOUT,
    TIMEZONE,
    POL_SHARP_CHANGE_THRESHOLD,
)
from utils.sheets_storage import read_from_sheets

logger = logging.getLogger(__name__)
FUND_ALERTS_FILE = "fund_alerts.json"

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


def get_fund_alerts():
    """دریافت تاریخچه هشدارهای صندوق‌ها (طلا و نقره در یک فایل، namespaced با فیلد commodity)"""
    try:
        if not GIST_ID or not GIST_TOKEN:
            return {}

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}"}
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if r.status_code == 200 and FUND_ALERTS_FILE in r.json()["files"]:
            return json.loads(r.json()["files"][FUND_ALERTS_FILE]["content"])

    except Exception as e:
        logger.error(f"خطا در خواندن fund_alerts: {e}")

    return {}


def save_fund_alerts(fund_alerts):
    """ذخیره تاریخچه هشدارهای صندوق‌ها"""
    try:
        if not GIST_ID or not GIST_TOKEN:
            return

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}"}

        requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    FUND_ALERTS_FILE: {
                        "content": json.dumps(fund_alerts, ensure_ascii=False, indent=2)
                    }
                }
            },
            timeout=REQUEST_TIMEOUT,
        )

    except Exception as e:
        logger.error(f"خطا در ذخیره fund_alerts: {e}")


def cleanup_old_alerts(alerts_dict, max_days=7):
    """پاک‌سازی بهینه‌شده داده‌های قدیمی‌تر از 7 روز"""
    if not alerts_dict:
        return {}

    try:
        tz = pytz.timezone(TIMEZONE)
        cutoff = (datetime.now(tz) - timedelta(days=max_days)).strftime("%Y-%m-%d")

        if all(d >= cutoff for d in alerts_dict.keys()):
            return alerts_dict

        cleaned = {d: items for d, items in alerts_dict.items() if d >= cutoff}
        removed = len(alerts_dict) - len(cleaned)

        if removed > 0:
            logger.info(f"{removed} روز قدیمی از تاریخچه پاک شد")

        return cleaned

    except Exception as e:
        logger.error(f"خطا در پاکسازی: {e}")
        return alerts_dict


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
        "sarane_kharid": None,
        "bubble_weighted": None,
        "pol_hagigi": None,
    }

    try:
        rows = read_from_sheets(commodity, limit=3)

        if len(rows) < 2:
            logger.warning(f"[{commodity}] داده کافی برای مقایسه نیست")
            return empty

        prev_row = rows[-2]
        last_row = rows[-1]

        try:
            prev_time = datetime.strptime(prev_row[0][:19], "%Y-%m-%d %H:%M:%S")
            last_time = datetime.strptime(last_row[0][:19], "%Y-%m-%d %H:%M:%S")
            time_diff = (last_time - prev_time).total_seconds() / 60

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
            "sarane_kharid": (
                float(last_row[9]) if len(last_row) > 9 and last_row[9] else None
            ),
            "bubble_weighted": (
                float(prev_row[8]) if len(prev_row) > 8 and prev_row[8] else None
            ),
            "pol_hagigi": (
                float(prev_row[12]) if len(prev_row) > 12 and prev_row[12] else None
            ),
        }

    except Exception as e:
        logger.error(f"[{commodity}] خطا در خواندن وضعیت قبلی: {e}")
        return empty


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

    changed = False
    bubble_status_changed = False
    pol_status_changed = False
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    # نوسان ۵ دقیقه‌ای دلار — فقط یک‌بار در کل ران
    if check_dollar and prev["dollar_price"] and prev["dollar_price"] > 0:
        change = (current_dollar - prev["dollar_price"]) / prev["dollar_price"] * 100
        if abs(change) >= ALERT_THRESHOLD_PERCENT:
            send_price_alert(bot_token, chat_id, "دلار", current_dollar, change, "تومان")

    # نوسان ۵ دقیقه‌ای شمش
    if prev["shams_price"] and prev["shams_price"] > 0:
        change = (current_shams - prev["shams_price"]) / prev["shams_price"] * 100
        if abs(change) >= ALERT_THRESHOLD_PERCENT:
            shams_divisor = 10 if commodity == "silver" else 1
            shams_unit = "تومان" if commodity == "silver" else "ریال"
            send_price_alert(
                bot_token, chat_id, f"شمش {label}",
                current_shams / shams_divisor, change, shams_unit,
            )

    # نوسان ۵ دقیقه‌ای انس جهانی
    if prev["global_price"] and prev["global_price"] > 0:
        change = (current_ounce - prev["global_price"]) / prev["global_price"] * 100
        if abs(change) >= ALERT_THRESHOLD_PERCENT:
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
                diff, current_pol, label,
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
        status, tz, now, commodity, label,
    )
    if pol_status_changed:
        changed = True

    # هشدار صندوق‌های فعال و کراس سرانه
    check_active_funds_alert(bot_token, chat_id, df_funds, tz, now, commodity, label)
    check_sarane_cross_alert(bot_token, chat_id, df_funds, tz, now, commodity, label)

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

    if changed or bubble_status_changed or pol_status_changed:
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
🎈 هشدار حباب {label} {dir_emoji}

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


def check_pol_alerts(bot_token, chat_id, current_pol, prev_pol, status, tz, now, commodity, label):
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
            rows = read_from_sheets(commodity, limit=3)
            if len(rows) >= 2:
                prev_row = rows[-2]
                last_row = rows[-1]

                prev_time = datetime.strptime(prev_row[0][:19], "%Y-%m-%d %H:%M:%S")
                last_time = datetime.strptime(last_row[0][:19], "%Y-%m-%d %H:%M:%S")

                if prev_time.date() == last_time.date():
                    pol_change = current_pol - prev_pol
                    if abs(pol_change) >= POL_SHARP_CHANGE_THRESHOLD:
                        send_pol_sharp_change_alert(
                            bot_token, chat_id, prev_pol, current_pol, pol_change, tz, now, label
                        )
                else:
                    logger.debug(f"[{commodity}] پول حقیقی در روزهای مختلف - هشدار ارسال نمیشه")
        except Exception as e:
            logger.warning(f"[{commodity}] خطا در بررسی تاریخ پول حقیقی: {e}")

    return status_changed


def send_pol_state_alert(bot_token, chat_id, pol_value, state, tz, now, label):
    """ارسال هشدار کراس صفر پول حقیقی"""
    if state == "positive":
        direction, dir_emoji, description = "مثبت", "🟢", "پول حقیقی مثبت شد"
    else:
        direction, dir_emoji, description = "منفی", "🔴", "پول حقیقی منفی شد"

    main_text = f"""
💸 هشدار پول حقیقی {label} {dir_emoji}

{description}
💰 پول حقیقی: {pol_value:+,.0f} میلیارد تومان
📊 وضعیت: {direction}
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_pol_sharp_change_alert(bot_token, chat_id, prev_value, curr_value, change, tz, now, label):
    """ارسال هشدار تغییر شدید پول حقیقی"""
    direction = "ورود" if change > 0 else "خروج"
    dir_emoji = "📈" if change > 0 else "📉"
    change_text = f"{abs(change):,.0f}"

    main_text = f"""
🚨 تغییر شدید پول حقیقی {label} {dir_emoji}

⏱ {direction} در 1 دقیقه: {change_text} میلیارد تومان
🔴 قبلی: {prev_value:+,.0f} م.ت
🟢 فعلی: {curr_value:+,.0f} م.ت
""".strip()

    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


# ════════════════════════════════════════════════════════════════
# صندوق‌های فعال و کراس سرانه — namespaced با commodity
# ════════════════════════════════════════════════════════════════


def check_active_funds_alert(bot_token, chat_id, df_funds, tz, now, commodity, label):
    """بررسی و ارسال هشدار صندوق‌های فعال"""
    try:
        if df_funds.empty:
            return

        latest_row = read_from_sheets(commodity, limit=1)
        if not latest_row:
            logger.warning(f"[{commodity}] هیچ داده‌ای از شیت دریافت نشد")
            return

        latest_row = latest_row[-1]
        sarane_kol = float(latest_row[9]) if len(latest_row) > 9 and latest_row[9] else 0

        active_funds = df_funds[
            (df_funds["value_to_avg_ratio"] >= 150)
            & (df_funds["pol_to_value_ratio"] >= 0.3)
            & (df_funds["ekhtelaf_sarane"] > 0)
            & (df_funds["sarane_kharid"] >= sarane_kol)
        ].copy()

        if active_funds.empty:
            logger.debug(f"[{commodity}] هیچ صندوق فعالی با شرایط سخت خرید پیدا نشد")
            return

        active_funds = active_funds.sort_values("value", ascending=False)
        active_funds["sarane_kharid_diff"] = active_funds["sarane_kharid"] - sarane_kol

        fund_alerts = get_fund_alerts()
        fund_alerts = cleanup_old_alerts(fund_alerts)

        today = now.strftime("%Y-%m-%d")
        today_list = fund_alerts.get(today, [])
        already_sent = {
            item["symbol"] for item in today_list
            if item.get("commodity") == commodity and item.get("alert_type") == "هشدار سخت خرید"
        }
        new_symbols = [s for s in active_funds.index if s not in already_sent]

        if not new_symbols:
            logger.debug(f"[{commodity}] همه صندوق‌های فعال امروز قبلاً هشدار دادن")
            return

        for sym in new_symbols:
            today_list.append({"symbol": sym, "alert_type": "هشدار سخت خرید", "commodity": commodity})
        fund_alerts[today] = today_list
        save_fund_alerts(fund_alerts)

        logger.info(f"[{commodity}] هشدار سخت خرید: {len(new_symbols)} صندوق جدید → {', '.join(new_symbols)}")

        funds_text = ""
        for symbol, row in active_funds.loc[new_symbols].iterrows():
            value_str = f"{row['value']:.0f} م.ت ({row['value_to_avg_ratio']:.0f}%)"
            pol_str = f"{row['pol_hagigi']:+.0f} م.ت ({row['pol_to_value_ratio']*100:+.1f}%)"
            sarane_str = f"{row['sarane_kharid']:.0f}M (+{row['sarane_kharid_diff']:.0f}M)"
            ekhtelaf_str = f"{row['ekhtelaf_sarane']:+.0f}M"

            funds_text += f"""
📌 {symbol}
💰 ارزش معاملات: {value_str}
💸 ورود پول حقیقی: {pol_str}
🟢 سرانه خرید: {sarane_str}
📊 اختلاف سرانه: {ekhtelaf_str}
🎈 حباب: {row['nominal_bubble']:+.1f}%

"""

        main_text = f"🚨 هشدار سخت خرید — {label}\n\n{funds_text}".strip()
        footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
        send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")

    except Exception as e:
        logger.error(f"[{commodity}] خطا در بررسی صندوق‌های فعال: {e}")


def check_sarane_cross_alert(bot_token, chat_id, df_funds, tz, now, commodity, label):
    """بررسی و ارسال هشدار کراس سرانه"""
    try:
        if df_funds.empty:
            return

        positive_cross = df_funds[df_funds["sarane_kharid"] > df_funds["sarane_forosh"]].copy()
        negative_cross = df_funds[df_funds["sarane_forosh"] > df_funds["sarane_kharid"]].copy()

        fund_alerts = get_fund_alerts()
        fund_alerts = cleanup_old_alerts(fund_alerts)

        today = now.strftime("%Y-%m-%d")
        today_list = fund_alerts.get(today, [])

        already_sent_positive = {
            item["symbol"] for item in today_list
            if item.get("commodity") == commodity and item.get("alert_type") == "کراس مثبت"
        }
        already_sent_negative = {
            item["symbol"] for item in today_list
            if item.get("commodity") == commodity and item.get("alert_type") == "کراس منفی"
        }

        new_positive = [s for s in positive_cross.index if s not in already_sent_positive]
        new_negative = [s for s in negative_cross.index if s not in already_sent_negative]

        if new_positive:
            positive_cross = positive_cross.loc[new_positive].sort_values("value", ascending=False)
            for sym in new_positive:
                today_list.append({"symbol": sym, "alert_type": "کراس مثبت", "commodity": commodity})

            logger.info(f"🟢 [{commodity}] کراس مثبت: {len(new_positive)} صندوق → {', '.join(new_positive)}")

            funds_text = ""
            for symbol, row in positive_cross.iterrows():
                pol_ratio = (row["pol_hagigi"] / row["value"] * 100) if row["value"] > 0 else 0
                funds_text += f"""
📌 {symbol}
💹 تغییر قیمت: {row["close_price_change_percent"]:+.1f}%
🎈 حباب: {row["nominal_bubble"]:+.1f}%
🟢 سرانه خرید: {row["sarane_kharid"]:,.0f}M
🔴 سرانه فروش: {row["sarane_forosh"]:,.0f}M
💰 ارزش معاملات: {row["value"]:.0f} م.ت ({row["value_to_avg_ratio"]*100:.0f}%)
💸 پول حقیقی: {row["pol_hagigi"]:+,.0f} م.ت ({pol_ratio:+.1f}%)

"""
            main_text = f"🟢 هشدار کراس مثبت سرانه — {label}\n{funds_text}".strip()
            footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
            send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")

        if new_negative:
            negative_cross = negative_cross.loc[new_negative].sort_values("value", ascending=False)
            for sym in new_negative:
                today_list.append({"symbol": sym, "alert_type": "کراس منفی", "commodity": commodity})

            logger.info(f"🔴 [{commodity}] کراس منفی: {len(new_negative)} صندوق → {', '.join(new_negative)}")

            funds_text = ""
            for symbol, row in negative_cross.iterrows():
                pol_ratio = (row["pol_hagigi"] / row["value"] * 100) if row["value"] > 0 else 0
                funds_text += f"""
📌 {symbol}
💹 تغییر قیمت: {row["close_price_change_percent"]:+.1f}%
🎈 حباب: {row["nominal_bubble"]:+.1f}%
🔴 سرانه فروش: {row["sarane_forosh"]:,.0f}M
🟢 سرانه خرید: {row["sarane_kharid"]:,.0f}M
💰 ارزش معاملات: {row["value"]:,.0f} م.ت ({row["value_to_avg_ratio"]*100:.1f}%)
💸 پول حقیقی: {row["pol_hagigi"]:+,.0f} م.ت ({pol_ratio:+.1f}%)

"""
            main_text = f"🔴 هشدار کراس منفی سرانه — {label}\n{funds_text}".strip()
            footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
            send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")

        if new_positive or new_negative:
            fund_alerts[today] = today_list
            save_fund_alerts(fund_alerts)

    except Exception as e:
        logger.error(f"[{commodity}] خطا در بررسی کراس سرانه: {e}")


# ════════════════════════════════════════════════════════════════
# پیام‌های هشدار قیمتی عمومی
# ════════════════════════════════════════════════════════════════


def send_price_alert(bot_token, chat_id, asset_name, price, change_5min, unit="تومان", is_ounce=False):
    """ارسال هشدار نوسان قیمتی"""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    change_text = f"{change_5min:+.2f}%".replace("+-", "−")

    price_formatted = f"${price:,.2f}" if is_ounce else f"{int(round(price)):,} {unit}"

    main_text = f"🚨 هشدار نوسان {asset_name}\n\n💰 قیمت: {price_formatted}\n📊 تغییر: {change_text}"
    footer = f"\n🕐 {get_jalali_timestamp(now)}\n🔗 {ALERT_CHANNEL_HANDLE}"
    send_alert_message(bot_token, chat_id, f"{main_text}\n{footer}")


def send_alert_ekhtelaf_fast(bot_token, chat_id, prev_val, curr_val, diff, pol_hagigi, label):
    """ارسال هشدار تغییر شدید اختلاف سرانه"""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    direction = "افزایش شدید (مثبت)" if diff > 0 else "کاهش شدید (منفی)"
    dir_emoji = "🟢" if diff > 0 else "🔴"
    diff_text = f"{diff:+.0f}".replace("+-", "−")
    pol_text = f"{pol_hagigi:+,.0f}".replace("+-", "−")

    main_text = (
        f"🚨 هشدار اختلاف سرانه — {label}\n\n{dir_emoji} {direction}\n"
        f"⏱ تغییر: {diff_text} میلیون تومان\n💰 پول حقیقی: {pol_text} میلیارد تومان"
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
        unit, asset_emoji = "", ""
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
