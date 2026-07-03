# main.py
"""اسکریپت اصلی Gold & Silver Market Tracker"""

import sys
import os
import logging
from datetime import datetime
import pytz
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALERT_CHAT_ID,
    TELETHON_API_ID, TELETHON_API_HASH, TELEGRAM_SESSION,
    TIMEZONE, LOG_FORMAT, LOG_FILE, LOG_LEVEL,
    DEFAULT_GOLD_PRICE, DEFAULT_SILVER_PRICE, DEFAULT_DOLLAR_PRICE,
    BULLION_ASSET,
)
from utils.data_fetcher import fetch_light_chart, fetch_market_data, fetch_dollar_prices, fetch_dirham_price
from utils.data_processor import process_market_data
from utils.telegram_sender import send_to_telegram
from utils.holidays import is_iranian_holiday
from utils.sheets_storage import save_to_sheets, read_from_sheets
from utils.alerts import check_and_send_alerts

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

DEFAULT_GLOBAL_PRICE = {"gold": DEFAULT_GOLD_PRICE, "silver": DEFAULT_SILVER_PRICE}
COMMODITY_LABEL = {"gold": "طلا", "silver": "نقره"}


# ════════════════════════════════════════════════════════════════
# داده‌ی دیروز از شیت (per-commodity)
# ════════════════════════════════════════════════════════════════

def get_global_price_yesterday_from_sheet(commodity, today_date):
    """دریافت قیمت جهانی آخرین روز کاری قبل از امروز، از تب یک کالا"""
    try:
        today = datetime.strptime(today_date, "%Y-%m-%d")
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


def get_dollar_yesterday_from_sheet(commodity, today_date):
    """دریافت قیمت دلار آخرین روز کاری قبل از امروز (دلار بین دو تب مشترکه، فقط یک تب کافیه)"""
    try:
        today = datetime.strptime(today_date, "%Y-%m-%d")
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

def process_and_dispatch(commodity, light_chart, market_data, last_trade, dollar_prices,
                          yesterday_close, dirham_price, check_dollar):
    bullion_key = BULLION_ASSET[commodity]

    if not light_chart or light_chart.get("price", 0) <= 0:
        global_price = DEFAULT_GLOBAL_PRICE[commodity]
        logger.warning(f"⚠️ [{commodity}] قیمت جهانی گرفته نشد → پیش‌فرض {global_price}")
    else:
        global_price = light_chart["price"]
        logger.info(f"✅ [{commodity}] قیمت جهانی: {global_price}")

    if not market_data:
        logger.error(f"❌ [{commodity}] داده‌های بازار گرفته نشد — این کالا رد می‌شود")
        return

    today_str = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
    global_yesterday, _, found = get_global_price_yesterday_from_sheet(commodity, today_str)
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
        return

    Fund_df = processed["Fund_df"]
    dfp = processed["dfp"]
    logger.info(f"✅ [{commodity}] پردازش کامل شد - {len(Fund_df)} صندوق")

    total_value = Fund_df["value"].sum() or 1
    fund_change_weighted = (Fund_df["close_price_change_percent"] * Fund_df["value"]).sum() / total_value
    fund_bubble_weighted = (Fund_df["nominal_bubble"] * Fund_df["value"]).sum() / total_value
    fund_final_price_avg = Fund_df["final_price_change"].mean()
    sarane_kharid_w = (Fund_df["sarane_kharid"] * Fund_df["value"]).sum() / total_value
    sarane_forosh_w = (Fund_df["sarane_forosh"] * Fund_df["value"]).sum() / total_value
    ekhtelaf_sarane_w = sarane_kharid_w - sarane_forosh_w
    pol_hagigi_weighted = Fund_df["pol_hagigi"].sum()

    dollar_change = ((last_trade - yesterday_close) / yesterday_close * 100) if yesterday_close else 0
    global_change = ((global_price - global_yesterday) / global_yesterday * 100) if global_yesterday else 0

    if bullion_key in dfp.index:
        shams_change = dfp.loc[bullion_key, "close_price_change_percent"]
        shams_price = dfp.loc[bullion_key, "close_price"]
        shams_date = dfp.loc[bullion_key, "trade_date"]
    else:
        shams_change, shams_price, shams_date = 0, 0, None
        logger.warning(f"⚠️ [{commodity}] دارایی شمش ('{bullion_key}') در dfp پیدا نشد")

    logger.info(f"📈 [{commodity}] دلار: {dollar_change:+.2f}% | انس: {global_change:+.2f}% | شمش: {shams_change:+.2f}%")
    logger.info(f"📈 [{commodity}] صندوق‌ها (وزنی): {fund_change_weighted:+.2f}% | حباب: {fund_bubble_weighted:+.2f}%")
    logger.info(f"💸 [{commodity}] پول حقیقی: {pol_hagigi_weighted:+.2f} م.ت")

    logger.info(f"💾 [{commodity}] ذخیره در Google Sheets...")
    save_to_sheets(commodity, {
        "global_price": global_price,
        "dollar_price": last_trade,
        "shams_price": shams_price,
        "dollar_change": dollar_change,
        "shams_change": shams_change,
        "shams_date": shams_date,
        "fund_change_weighted": fund_change_weighted,
        "fund_final_price_avg": fund_final_price_avg,
        "fund_bubble_weighted": fund_bubble_weighted,
        "sarane_kharid_w": sarane_kharid_w,
        "sarane_forosh_w": -sarane_forosh_w,
        "ekhtelaf_sarane_w": ekhtelaf_sarane_w,
        "pol_hagigi": pol_hagigi_weighted,
    })

    logger.info(f"📤 [{commodity}] ارسال گزارش به تلگرام...")
    success = send_to_telegram(
        commodity=commodity,
        bot_token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID,
        data=processed,
        dollar_prices=dollar_prices,
        global_price=global_price,
        global_yesterday=global_yesterday,
        global_time=None,
        yesterday_close=yesterday_close,
        dirham_price=dirham_price,
    )
    logger.info(f"{'✅' if success else '⚠️'} [{commodity}] ارسال گزارش {'موفق' if success else 'ناموفق'}")

    logger.info(f"🚨 [{commodity}] بررسی هشدارها...")
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
        )
        logger.info(f"✅ [{commodity}] بررسی هشدارها کامل شد")
    except Exception as e:
        logger.error(f"⚠️ [{commodity}] خطا در سیستم هشدارها (ادامه می‌دهیم): {e}")


# ════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════

async def main():
    try:
        logger.info("=" * 60)
        logger.info("🚀 شروع اجرای Gold & Silver Market Tracker")
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
                return

        logger.info(f"🕐 زمان تهران: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALERT_CHAT_ID,
                    TELETHON_API_ID, TELETHON_API_HASH, TELEGRAM_SESSION]):
            logger.error("❌ یکی از متغیرهای محیطی تلگرام پیدا نشد!")
            logger.error("لازم: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALERT_CHAT_ID, "
                          "TELETHON_API_ID, TELETHON_API_HASH, TELEGRAM_SESSION")
            return

        today_str = now.strftime("%Y-%m-%d")

        async with TelegramClient(StringSession(TELEGRAM_SESSION), TELETHON_API_ID, TELETHON_API_HASH) as client:
            logger.info("✅ اتصال به Telethon برقرار شد")

            # ─── دلار — یک‌بار، مشترک بین طلا و نقره ───
            logger.info("💵 دریافت قیمت‌های دلار...")
            dollar_prices = await fetch_dollar_prices(client)

            if not dollar_prices or not dollar_prices.get("last_trade"):
                last_trade = DEFAULT_DOLLAR_PRICE
                dollar_prices = {
                    "last_trade": DEFAULT_DOLLAR_PRICE,
                    "bid": dollar_prices.get("bid", 0) if dollar_prices else 0,
                    "ask": dollar_prices.get("ask", 0) if dollar_prices else 0,
                }
                logger.warning(f"⚠️ قیمت معامله دلار گرفته نشد → پیش‌فرض {DEFAULT_DOLLAR_PRICE:,}")
            else:
                last_trade = dollar_prices["last_trade"]
                logger.info(f"✅ آخرین معامله دلار: {last_trade:,} تومان")

            dollar_yesterday, _, dollar_found = get_dollar_yesterday_from_sheet("gold", today_str)
            yesterday_close = dollar_yesterday if dollar_yesterday else last_trade
            if not dollar_found:
                logger.warning(f"⚠️ قیمت دلار دیروز پیدا نشد → استفاده از قیمت فعلی ({last_trade:,})")

            logger.info("🇦🇪 دریافت قیمت درهم امارات...")
            dirham_price = fetch_dirham_price()

            # ─── fetch موازی طلا+نقره (فقط شبکه) ───
            logger.info("📡 دریافت داده‌های بازار طلا و نقره (موازی)...")
            results = await asyncio.gather(
                fetch_commodity_inputs("gold"),
                fetch_commodity_inputs("silver"),
            )

            # ─── process→save→send→alert — sequential ───
            for i, (commodity, light_chart, market_data) in enumerate(results):
                logger.info("-" * 60)
                logger.info(f"▶️ شروع پردازش {COMMODITY_LABEL[commodity]} ({commodity})")
                process_and_dispatch(
                    commodity=commodity,
                    light_chart=light_chart,
                    market_data=market_data,
                    last_trade=last_trade,
                    dollar_prices=dollar_prices,
                    yesterday_close=yesterday_close,
                    dirham_price=dirham_price,
                    check_dollar=(i == 0),  # فقط بار اول (طلا) دلار چک می‌شه
                )

        logger.info("=" * 60)
        logger.info("✅ اجرای کامل به پایان رسید")
        logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("\n⚠️ برنامه توسط کاربر متوقف شد")

    except Exception as e:
        logger.error("=" * 60)
        logger.error(f"❌ خطای کلی: {e}", exc_info=True)
        logger.error("=" * 60)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 خداحافظ!")
    except Exception as e:
        logger.critical(f"💥 خطای بحرانی: {e}", exc_info=True)
        sys.exit(1)
