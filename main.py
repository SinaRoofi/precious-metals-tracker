# main.py
"""اسکریپت اصلی Gold & Silver Market Tracker"""

import sys
import os
import logging
from datetime import datetime
import time
import config as _config
import pytz
import jdatetime
import pandas as pd
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALERT_CHAT_ID,
    TELETHON_API_ID, TELETHON_API_HASH, TELEGRAM_SESSION,
    TIMEZONE, LOG_FORMAT, LOG_FILE, LOG_LEVEL,
    BULLION_ASSET,
)
from utils.data_fetcher import fetch_light_chart, fetch_market_data, fetch_dollar_prices, fetch_dirham_price
from utils.salaf import fetch_salaf_lines  # عسکه — بعد از ۱۸ آذر ۱۴۰۵ حذف شه
from utils.data_processor import process_market_data
from utils.telegram_sender import send_to_telegram
from utils.holidays import is_iranian_holiday, is_funds_trading_day
from utils.sheets_storage import save_to_sheets, read_from_sheets, SheetsReadError
from utils.log_safety import redact
from utils.alerts import check_and_send_alerts

class JalaliFormatter(logging.Formatter):
    """Formatter که %(asctime)s رو با تاریخ و ساعت شمسی (تهران) پر می‌کند"""

    def formatTime(self, record, datefmt=None):
        tehran_tz = pytz.timezone(TIMEZONE)
        dt = datetime.fromtimestamp(record.created, tz=tehran_tz)
        jalali = jdatetime.datetime.fromgregorian(datetime=dt)
        return jalali.strftime("%Y-%m-%d %H:%M:%S")

    def format(self, record):
        # توکن‌ها (مثلاً داخل URL تو متن خطای requests) هیچ‌وقت وارد فایل/خروجی لاگ نشن
        return redact(super().format(record))


_formatter = JalaliFormatter(LOG_FORMAT)
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_formatter)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_formatter)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    handlers=[_file_handler, _stream_handler],
)
logger = logging.getLogger(__name__)

COMMODITY_LABEL = {"gold": "طلا", "silver": "نقره"}


# ════════════════════════════════════════════════════════════════
# داده‌ی دیروز از شیت (per-commodity)
# ════════════════════════════════════════════════════════════════

def get_global_price_yesterday_from_sheet(commodity, today_date, rows=None):
    """دریافت قیمت جهانی آخرین روز کاری قبل از امروز، از تب یک کالا.
    اگه rows از قبل خونده شده (مثلاً برای دلار روی همون تب) پاس داده بشه، دوباره شیت رو نمی‌خونه."""
    try:
        today = datetime.strptime(today_date, "%Y-%m-%d")
        if rows is None:
            rows = read_from_sheets(commodity, limit=800)

        if not rows:
            logger.warning(f"⚠️ [{commodity}] هیچ رکوردی در شیت پیدا نشد")
            return None, None, False

        for row in reversed(rows):
            if len(row) > 1 and row[0]:
                row_date_str = row[0][:10]
                row_date = datetime.strptime(row_date_str, "%Y-%m-%d")
                if row_date < today:
                    if row[1]:
                        price = float(row[1])
                        days_ago = (today - row_date).days
                        logger.info(f"✅ [{commodity}] قیمت جهانی دیروز: ${price:.2f} ({row_date_str}, {days_ago} روز پیش)")
                        return price, row_date_str, True
                    continue

        logger.warning(f"⚠️ [{commodity}] هیچ رکورد معتبری قبل از {today_date} پیدا نشد")
        return None, None, False

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در خواندن قیمت جهانی دیروز: {e}")
        return None, None, False


def get_dollar_yesterday_from_sheet(commodity, today_date, rows=None):
    """دریافت قیمت دلار آخرین روز کاری قبل از امروز (دلار بین دو تب مشترکه، فقط یک تب کافیه).
    اگه rows از قبل خونده شده پاس داده بشه، دوباره شیت رو نمی‌خونه."""
    try:
        today = datetime.strptime(today_date, "%Y-%m-%d")
        if rows is None:
            rows = read_from_sheets(commodity, limit=800)

        if not rows:
            return None, None, False

        for row in reversed(rows):
            if len(row) > 2 and row[0]:
                row_date_str = row[0][:10]
                row_date = datetime.strptime(row_date_str, "%Y-%m-%d")
                if row_date < today:
                    if row[2]:
                        price = float(row[2])
                        days_ago = (today - row_date).days
                        logger.info(f"✅ قیمت دلار دیروز: {price:,.0f} تومان ({row_date_str}, {days_ago} روز پیش)")
                        return price, row_date_str, True
                    continue

        return None, None, False

    except Exception as e:
        logger.error(f"❌ خطا در خواندن قیمت دلار دیروز: {e}")
        return None, None, False


# ════════════════════════════════════════════════════════════════
# مرحله‌ی fetch — parallel-safe (فقط شبکه، بدون write مشترک)
# ════════════════════════════════════════════════════════════════

async def fetch_commodity_inputs(commodity):
    """فچ همزمان انس جهانی + دارایی‌های داخلی/صندوق‌ها برای یک کالا"""
    light_chart, market_data = await asyncio.gather(
        asyncio.to_thread(fetch_light_chart, commodity),
        asyncio.to_thread(fetch_market_data, commodity),
    )
    return commodity, light_chart, market_data


# ════════════════════════════════════════════════════════════════
# مرحله‌ی process→save→send→alert — عمداً sequential (Gist مشترک)
# ════════════════════════════════════════════════════════════════

def _log_t(commodity, label, t0):
    logger.info(f"⏱️ [{commodity}] {label}: {time.perf_counter() - t0:.1f}s")


def process_and_dispatch(commodity, light_chart, market_data, last_trade, dollar_prices,
                          yesterday_close, dirham_price, check_dollar, preloaded_yesterday_rows=None,
                          salaf_lines=None):
    """پردازش→ذخیره→ارسال→هشدار یک کالا. True = کامل انجام شد؛ False = رد شد (بدون نوشتن داده‌ی ناقص)."""
    bullion_key = BULLION_ASSET[commodity]

    # بدون فالبک: قیمت جهانی نیومد → این کالا کامل رد می‌شه (چیزی تو شیت/تلگرام نمی‌ره)
    if not light_chart or not light_chart.get("price") or light_chart["price"] <= 0:
        logger.error(f"❌ [{commodity}] قیمت جهانی گرفته نشد — این کالا رد می‌شود (بدون ذخیره/ارسال)")
        return False
    global_price = light_chart["price"]
    logger.info(f"✅ [{commodity}] قیمت جهانی: {global_price}")

    tether_price = light_chart.get("tether_price") if light_chart else None
    tether_change_percent = light_chart.get("tether_change_percent") if light_chart else None

    if not market_data:
        logger.error(f"❌ [{commodity}] داده‌های بازار گرفته نشد — این کالا رد می‌شود")
        return False

    today_str = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
    global_yesterday, _, found = get_global_price_yesterday_from_sheet(
        commodity, today_str, rows=preloaded_yesterday_rows
    )
    if not found:
        logger.warning(f"⚠️ [{commodity}] قیمت جهانی دیروز پیدا نشد → تغییر صفر محاسبه می‌شود")
        global_yesterday = None

    processed = process_market_data(
        commodity=commodity,
        market_data=market_data,
        global_price=global_price,
        dollar_last_trade=last_trade,
        yesterday_close=yesterday_close,
        global_price_yesterday=global_yesterday,
    )

    if not processed:
        logger.error(f"❌ [{commodity}] پردازش داده ناموفق — این کالا رد می‌شود")
        return False

    Fund_df = processed["Fund_df"]
    dfp = processed["dfp"]
    logger.info(f"✅ [{commodity}] پردازش کامل شد - {len(Fund_df)} صندوق")

    # روزی که صندوق‌ها معامله ندارن (مثلاً پنج‌شنبه)، داده‌ی صندوق‌ها همون آخرین روز معاملاتیه (کهنه).
    # تنظیمش تو config.py (FUNDS_TRADING_WEEKDAYS): اگه صندوق‌ها اون روز هم باز شدن، فقط اونجا عوض کن.
    funds_open = is_funds_trading_day(datetime.now(pytz.timezone(TIMEZONE)))

    if funds_open and (Fund_df is None or Fund_df.empty or "value" not in Fund_df.columns):
        # API صندوق‌ها ردیفی نداد (روز معاملاتی): داده‌ی ساختگی/صفر نمی‌نویسیم، این کالا رد می‌شه
        logger.error(f"❌ [{commodity}] داده‌ی صندوق‌ها خالی است (روز معاملاتی) — این کالا رد می‌شود (بدون ذخیره/ارسال)")
        return False

    if funds_open:
        total_value = Fund_df["value"].sum()  # ارزش معاملات واقعی (برای ذخیره‌سازی) — می‌تواند صفر باشد
        weighting_denominator = total_value or 1  # فقط برای جلوگیری از تقسیم بر صفر در میانگین‌های وزنی
        fund_change_weighted = (Fund_df["close_price_change_percent"] * Fund_df["value"]).sum() / weighting_denominator
        fund_bubble_weighted = (Fund_df["nominal_bubble"] * Fund_df["value"]).sum() / weighting_denominator
        fund_final_price_avg = Fund_df["final_price_change"].mean()
        sarane_kharid_w = (Fund_df["sarane_kharid"] * Fund_df["value"]).sum() / weighting_denominator
        sarane_forosh_w = (Fund_df["sarane_forosh"] * Fund_df["value"]).sum() / weighting_denominator
        ekhtelaf_sarane_w = sarane_kharid_w - sarane_forosh_w
        pol_hagigi_weighted = Fund_df["pol_hagigi"].sum()
    else:
        logger.info(f"📅 [{commodity}] امروز بازار صندوق‌ها بسته‌ست — ستون‌های صندوق خالی ذخیره می‌شن")
        total_value = fund_change_weighted = fund_bubble_weighted = fund_final_price_avg = None
        sarane_kharid_w = sarane_forosh_w = ekhtelaf_sarane_w = pol_hagigi_weighted = None

    dollar_change = ((last_trade - yesterday_close) / yesterday_close * 100) if yesterday_close else 0
    global_change = ((global_price - global_yesterday) / global_yesterday * 100) if global_yesterday else 0

    # بدون فالبک: داده‌ی شمش نیومد → این کالا رد می‌شه (نه صفر ساختگی تو شیت، نه NaN).
    # نکته: dfp در data_processor با reindex(ASSET_ORDER) ساخته می‌شه، پس کلید شمش همیشه
    # تو ایندکسه؛ «نبودن» شمش یعنی close_price=0 (fillna(0)) و Bubble/تغییر = NaN.
    shams_change = dfp.loc[bullion_key, "close_price_change_percent"]
    shams_price = dfp.loc[bullion_key, "close_price"]
    shams_date = dfp.loc[bullion_key, "trade_date"]
    shams_bubble = dfp.loc[bullion_key, "Bubble"]
    if (pd.isna(shams_price) or shams_price <= 0
            or pd.isna(shams_bubble) or pd.isna(shams_change)):
        logger.error(
            f"❌ [{commodity}] داده‌ی شمش ('{bullion_key}') ناقص/نامعتبر است "
            f"(قیمت={shams_price}, حباب={shams_bubble}, تغییر={shams_change}) — "
            f"این کالا رد می‌شود (بدون ذخیره/ارسال)"
        )
        return False

    logger.info(f"📈 [{commodity}] دلار: {dollar_change:+.2f}% | انس: {global_change:+.2f}% | شمش: {shams_change:+.2f}%")
    if funds_open:
        logger.info(f"📈 [{commodity}] صندوق‌ها (وزنی): {fund_change_weighted:+.2f}% | حباب: {fund_bubble_weighted:+.2f}%")
        logger.info(f"💸 [{commodity}] پول حقیقی: {pol_hagigi_weighted:+.2f} م.ت")

    logger.info(f"💾 [{commodity}] ذخیره در Google Sheets...")
    _t = time.perf_counter()
    saved = save_to_sheets(commodity, {
        "global_price": global_price,
        "dollar_price": last_trade,
        "shams_price": shams_price,
        "dollar_change": dollar_change,
        "global_change": global_change,
        "shams_change": shams_change,
        "shams_date": shams_date,
        "fund_change_weighted": fund_change_weighted,
        "fund_final_price_avg": fund_final_price_avg,
        "fund_bubble_weighted": fund_bubble_weighted,
        "sarane_kharid_w": sarane_kharid_w,
        "sarane_forosh_w": None if sarane_forosh_w is None else -sarane_forosh_w,
        "ekhtelaf_sarane_w": ekhtelaf_sarane_w,
        "pol_hagigi": pol_hagigi_weighted,
        "shams_bubble": shams_bubble,
        "trade_value": total_value,
    })
    _log_t(commodity, "ذخیره در شیت", _t)
    if not saved:
        # ردیف تو شیت نیست: ارسال/هشدار رد می‌شن، چون هشدارها «ردیف قبلی» رو از شیت می‌خونن
        # و بدون ردیف جدید، مقایسه‌ها با ردیف اشتباه انجام می‌شد
        logger.error(f"❌ [{commodity}] ذخیره در شیت ناموفق — ارسال گزارش و هشدارها این ران رد شد")
        return False

    logger.info(f"📤 [{commodity}] ارسال گزارش به تلگرام{'' if funds_open else ' (نسخه‌ی بدون صندوق‌ها)'}...")
    _t = time.perf_counter()
    success = send_to_telegram(
        commodity=commodity,
        bot_token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID,
        data=processed,
        dollar_prices=dollar_prices,
        global_price=global_price,
        global_yesterday=global_yesterday,
        global_time=light_chart.get("time") if light_chart else None,
        yesterday_close=yesterday_close,
        dirham_price=dirham_price,
        tether_price=tether_price,
        tether_change_percent=tether_change_percent,
        salaf_lines=salaf_lines,
        funds_open=funds_open,
    )
    logger.info(f"{'✅' if success else '⚠️'} [{commodity}] ارسال گزارش {'موفق' if success else 'ناموفق'}")
    _log_t(commodity, "ساخت تصاویر + ارسال تلگرام", _t)

    logger.info(f"🚨 [{commodity}] بررسی هشدارها...")
    _t = time.perf_counter()
    try:
        check_and_send_alerts(
            commodity=commodity,
            bot_token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_ALERT_CHAT_ID,
            data=processed,
            dollar_prices=dollar_prices,
            global_price=global_price,
            yesterday_close=yesterday_close,
            global_price_yesterday=global_yesterday,
            check_dollar=check_dollar,
            funds_open=funds_open,
        )
        logger.info(f"✅ [{commodity}] بررسی هشدارها کامل شد")
        _log_t(commodity, "بررسی هشدارها", _t)
    except Exception as e:
        logger.error(f"⚠️ [{commodity}] خطا در سیستم هشدارها (ادامه می‌دهیم): {e}")
        return False
    return True


# ════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════

async def main():
    """True = اجرا سالم (یا تعطیلی عادی)؛ False = چیزی رد شد/شکست خورد → exit code ناصفر تا GitHub قرمز شه."""
    try:
        logger.info("=" * 60)
        logger.info("🚀 شروع اجرای Gold & Silver Market Tracker")
        t_run = time.perf_counter()
        logger.info("=" * 60)

        tehran_tz = pytz.timezone(TIMEZONE)
        now = datetime.now(tehran_tz)

        force_run = os.getenv("FORCE_RUN", "false").lower() == "true"

        if is_iranian_holiday(now):
            if force_run:
                logger.warning(
                    f"⚠️ امروز ({now.strftime('%Y-%m-%d')}) طبق تشخیص سیستم تعطیله، "
                    f"ولی FORCE_RUN=true است — اجرا ادامه پیدا می‌کند (حالت تست)"
                )
            else:
                logger.info(f"🏖️ امروز {now.strftime('%Y-%m-%d')} تعطیل است.")
                return True

        logger.info(f"🕐 زمان تهران: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALERT_CHAT_ID,
                    TELETHON_API_ID, TELETHON_API_HASH, TELEGRAM_SESSION]):
            logger.error("❌ یکی از متغیرهای محیطی تلگرام پیدا نشد!")
            logger.error("لازم: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALERT_CHAT_ID, "
                          "TELETHON_API_ID, TELETHON_API_HASH, TELEGRAM_SESSION")
            return False

        today_str = now.strftime("%Y-%m-%d")

        async with TelegramClient(StringSession(TELEGRAM_SESSION), TELETHON_API_ID, TELETHON_API_HASH) as client:
            logger.info("✅ اتصال به Telethon برقرار شد")

            # ─── همه‌ی کارهای شبکه‌ایِ مستقل هم‌زمان (قبلاً سه مرحله‌ی پشت‌سرهم بود) ───
            # دلار (مشترک بین طلا و نقره)، درهم، عسکه، خواندن تب‌های gold/silver برای قیمت دیروز،
            # و داده‌ی بازار هر دو کالا. هیچ‌کدوم به نتیجه‌ی بقیه وابسته نیست؛ فقط «پردازش» به همه‌شون نیاز داره.
            logger.info("📡 دریافت هم‌زمان: دلار، درهم، عسکه، شیت (gold/silver) و داده‌ی بازار طلا/نقره...")
            t_fetch = time.perf_counter()
            # تاگل‌های کپشن (config.py): اگه خاموش باشن، دریافتشون هم رد می‌شه (یک درخواست کمتر)
            dirham_fetcher = fetch_dirham_price if getattr(_config, "SHOW_DIRHAM_IN_CAPTION", True) else (lambda: None)
            salaf_fetcher = fetch_salaf_lines if getattr(_config, "SHOW_SALAF_IN_CAPTION", True) else (lambda: None)
            fetched = await asyncio.gather(
                fetch_dollar_prices(client),
                asyncio.to_thread(dirham_fetcher),
                asyncio.to_thread(read_from_sheets, "gold", 800, True),
                asyncio.to_thread(read_from_sheets, "silver", 800, True),
                asyncio.to_thread(salaf_fetcher),  # عسکه
                fetch_commodity_inputs("gold"),
                fetch_commodity_inputs("silver"),
                return_exceptions=True,
            )
            logger.info(f"⏱️ دریافت هم‌زمان داده‌ها: {time.perf_counter() - t_fetch:.1f}s")

            if any(isinstance(r, SheetsReadError) for r in fetched):
                # خطای خواندن شیت ≠ شیت خالی: اگه ادامه بدیم «دیروز پیدا نشد» حساب می‌شه و تغییرِ صفرِ ساختگی ذخیره می‌شه
                logger.error("❌ خواندن شیت ناموفق — کل این اجرا رد می‌شود (بدون ذخیره/ارسال)")
                return False
            for r in fetched:
                if isinstance(r, BaseException):
                    raise r  # خطای پیش‌بینی‌نشده: مثل قبل به catch بیرونی می‌ره
            (dollar_prices, dirham_price, gold_yesterday_rows, silver_yesterday_rows,
             salaf_lines, gold_inputs, silver_inputs) = fetched
            results = [gold_inputs, silver_inputs]

            # بدون فالبک: دلار مشترک بین طلا و نقره‌ست؛ نیومد → کل این اجرا رد می‌شه (هیچی تو شیت نمی‌نویسیم)
            if not dollar_prices or not dollar_prices.get("last_trade"):
                logger.error("❌ قیمت معامله دلار گرفته نشد — کل این اجرا رد می‌شود (بدون ذخیره/ارسال)")
                return False
            last_trade = dollar_prices["last_trade"]
            logger.info(f"✅ آخرین معامله دلار: {last_trade:,} تومان")

            dollar_yesterday, _, dollar_found = get_dollar_yesterday_from_sheet(
                "gold", today_str, rows=gold_yesterday_rows
            )
            yesterday_close = dollar_yesterday if dollar_yesterday else last_trade
            if not dollar_found:
                logger.warning(f"⚠️ قیمت دلار دیروز پیدا نشد → استفاده از قیمت فعلی ({last_trade:,})")

            # ─── process→save→send→alert — sequential؛ خطای یک کالا کالای بعدی رو خراب نمی‌کنه ───
            yesterday_rows = {"gold": gold_yesterday_rows, "silver": silver_yesterday_rows}
            failed = []
            for i, (commodity, light_chart, market_data) in enumerate(results):
                logger.info("-" * 60)
                logger.info(f"▶️ شروع پردازش {COMMODITY_LABEL[commodity]} ({commodity})")
                t_c = time.perf_counter()
                try:
                    ok = process_and_dispatch(
                        commodity=commodity,
                        light_chart=light_chart,
                        market_data=market_data,
                        last_trade=last_trade,
                        dollar_prices=dollar_prices,
                        yesterday_close=yesterday_close,
                        dirham_price=dirham_price,
                        check_dollar=(i == 0),  # فقط بار اول (طلا) دلار چک می‌شه
                        preloaded_yesterday_rows=yesterday_rows[commodity],
                        salaf_lines=(salaf_lines if commodity == "gold" else None),
                    )
                except Exception as e:
                    logger.error(f"❌ [{commodity}] خطای پیش‌بینی‌نشده — این کالا رد شد: {e}", exc_info=True)
                    ok = False
                _log_t(commodity, "کل پردازش", t_c)
                if not ok:
                    failed.append(commodity)

        if failed:
            logger.error(f"❌ اجرا با شکست/رد شدن این کالاها تمام شد: {', '.join(failed)}")
            return False

        logger.info("=" * 60)
        logger.info(f"✅ اجرای کامل به پایان رسید (⏱️ مجموع: {time.perf_counter() - t_run:.1f}s)")
        logger.info("=" * 60)
        return True

    except KeyboardInterrupt:
        logger.info("\n⚠️ برنامه توسط کاربر متوقف شد")
        return False

    except Exception as e:
        logger.error("=" * 60)
        logger.error(f"❌ خطای کلی: {e}", exc_info=True)
        logger.error("=" * 60)
        raise


if __name__ == "__main__":
    try:
        sys.exit(0 if asyncio.run(main()) else 1)
    except KeyboardInterrupt:
        print("\n👋 خداحافظ!")
    except Exception as e:
        logger.critical(f"💥 خطای بحرانی: {e}", exc_info=True)
        sys.exit(1)
