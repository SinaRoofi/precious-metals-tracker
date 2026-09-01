import sys
import logging
from datetime import datetime
import jdatetime
import pytz
import asyncio
import time
import pandas as pd

from config import (
    MARKET_START_TIME,
    MARKET_END_TIME,
    API_BASE_URL,
    ERROR_CHAT_ID,
    GIST_ID,
    GIST_TOKEN,
    PERSONAL_WATCHLIST,
    WATCHLIST_CHAT_ID,
    validate_config,
)
from utils.holidays import is_trading_day
from utils.data_fetcher import UnifiedDataFetcher
from utils.data_processor import BourseDataProcessor
from utils.alerts import TelegramAlert
from utils.gist_alert_manager import GistAlertManager, WATCHLIST_COPY_SUFFIX

# ===========================
# تنظیم timezone تهران
# ===========================
TEHRAN_TZ = pytz.timezone("Asia/Tehran")


# ===========================
# تنظیم logging به وقت تهران
# ===========================
def tehran_time(*args):
    return datetime.now(TEHRAN_TZ).timetuple()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bourse_tracker.log", encoding="utf-8"),
    ],
)
logging.Formatter.converter = tehran_time
logger = logging.getLogger(__name__)

# ===========================
# تعداد سهام در هر پیام بر اساس فیلتر
# ===========================
STOCKS_PER_MESSAGE_MAP = {
    "filter_1_strong_buying": 5,
    "filter_2_sarane_cross": 5,
    "filter_3_watchlist": 5,
    "filter_4_range_mosbat": 5,
    "filter_5_pol_hagigi_ratio": 5,
    "filter_6_tick_time": 5,
    "filter_7_suspicious_volume": 5,
    "filter_8_swing_trade": 5,
    "filter_9_first_hour": 5,
    "filter_10_heavy_buy_queue": 5,
    "filter_11_hoghooghi_haghighi_strong_buy": 5,
    "filter_12_bullish_marubozu": 5,
    "filter_13_sarane_diff": 5,
    "filter_14_buy_queue_simple": 5,
}

# ===========================
# نگاشت فیلتر به ستون کلیدی برای Daily Summary
# فقط فیلترهایی که در summary نمایش داده می‌شن
# ===========================
FILTER_VALUE_COLUMN = {
    "filter_1_strong_buying":                  "godrat_kharid",
    "filter_2_sarane_cross":                   "sarane_kharid",
    "filter_5_pol_hagigi_ratio":               "pol_hagigi_to_avg_monthly_value",
    "filter_7_suspicious_volume":              "value_to_avg_monthly_value",
    "filter_10_heavy_buy_queue":               "buy_queue_value",
    "filter_11_hoghooghi_haghighi_strong_buy": "sarane_kharid",
    "filter_12_bullish_marubozu":               "intraday_move_percent",
    "filter_13_sarane_diff":                    "sarane_diff",
    "filter_14_buy_queue_simple":               "buy_queue_value",
}


# ===========================
# توابع کمکی
# ===========================
def is_market_open() -> bool:
    """بررسی اینکه آیا بازار باز است یا نه (به وقت تهران)"""
    now = datetime.now(TEHRAN_TZ)
    logger.info(f"🕐 زمان تهران: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    if not is_trading_day(now):
        logger.info("امروز روز معاملاتی نیست (آخر هفته یا تعطیل رسمی)")
        return False

    current_time = now.strftime("%H:%M")
    if not (MARKET_START_TIME <= current_time <= MARKET_END_TIME):
        logger.info(f"خارج از ساعات کاری بازار (ساعت تهران: {current_time})")
        return False

    jnow = jdatetime.datetime.fromgregorian(datetime=now.replace(tzinfo=None))
    logger.info(f"✅ بازار باز است - {jnow.strftime('%Y-%m-%d')} {current_time}")
    return True


def chunk_dataframe(df, filter_name):
    """تقسیم DataFrame به چانک‌های کوچکتر بر اساس فیلتر"""
    chunk_size = STOCKS_PER_MESSAGE_MAP.get(filter_name, 5)
    for i in range(0, len(df), chunk_size):
        yield df.iloc[i : i + chunk_size]


# ===========================
# ارسال هشدارها - نسخه Parallel
# ===========================

# توجه: WATCHLIST_COPY_SUFFIX از utils.gist_alert_manager ایمپورت می‌شه (نه اینجا
# تعریف می‌شه) چون daily_summary_generator.py هم برای رفع دوبار-شمارش نمادهای
# واچ‌لیستی در گزارش «نمادهای پرتکرار» بهش نیاز داره — نگه‌داری در یک‌جا از
# عدم‌همگامی بین دو فایل جلوگیری می‌کنه.


async def _queue_filter_tasks(
    all_tasks: list,
    alert: TelegramAlert,
    alert_manager: GistAlertManager,
    df: pd.DataFrame,
    filter_name: str,
    dedup_type: str,
    chat_id: str,
    channel_label: str,
    value_col: str,
    skipped_count: int,
) -> int:
    """
    df رو chunk می‌کنه، برای هر نماد dedup چک می‌کنه، و task ارسال برای
    chunkهایی که چیزی برای ارسال دارن به all_tasks اضافه می‌کنه.
    مقدار جدید skipped_count رو برمی‌گردونه (چون int تو پایتون immutable هست).
    """
    for chunk_idx, chunk_df in enumerate(chunk_dataframe(df, filter_name), 1):
        symbols_to_send = []

        for _, row in chunk_df.iterrows():
            symbol = row["symbol"]
            if not await alert_manager.should_send_alert(symbol, dedup_type):
                logger.info(f"⏭️  {symbol}: قبلاً امروز ارسال شده ({dedup_type})")
                skipped_count += 1
            else:
                symbols_to_send.append(symbol)

        if symbols_to_send:
            chunk_to_send = chunk_df[chunk_df["symbol"].isin(symbols_to_send)]
            task = alert.send_filter_alert(
                chunk_to_send, filter_name, chat_id=chat_id, channel_label=channel_label
            )
            all_tasks.append(
                (task, symbols_to_send, dedup_type, filter_name, chunk_idx, chunk_to_send, value_col)
            )
            logger.info(
                f"📋 Task ایجاد شد برای {filter_name} [{dedup_type}] گروه {chunk_idx}: "
                f"{len(symbols_to_send)} سهم"
            )
        else:
            logger.info(f"⏭️  {filter_name} [{dedup_type}] گروه {chunk_idx}: همه قبلاً ارسال شده‌اند")

    return skipped_count


async def send_alerts_for_filters_async(
    alert: TelegramAlert,
    alert_manager: GistAlertManager,
    filters_results: dict,
    api_name: str,
    personal_watchlist: set = frozenset(),
    watchlist_chat_id: str = "",
) -> tuple:
    """
    ارسال هشدارها برای فیلترهای یک API به صورت کاملاً موازی

    Args:
        alert: شیء TelegramAlert
        alert_manager: شیء GistAlertManager
        filters_results: دیکشنری نتایج فیلترها
        api_name: نام API (برای لاگ)
        personal_watchlist: مجموعه نمادهای واچ‌لیست شخصی
        watchlist_chat_id: چت آیدی کانال دوم (واچ‌لیست شخصی)

    روتینگ:
        - filter_3_watchlist: فقط به watchlist_chat_id می‌ره (هرگز کانال اصلی)
        - بقیه‌ی فیلترها: طبق روال به کانال اصلی + اگه symbol تو
          personal_watchlist باشه، یک کپی هم به watchlist_chat_id

    Returns:
        tuple: (تعداد ارسال شده, تعداد رد شده)
    """
    sent_count = 0
    skipped_count = 0

    logger.info(f"\n{'='*60}")
    logger.info(f"📤 ارسال هشدارهای {api_name}")
    logger.info(f"{'='*60}")

    all_tasks = []

    for filter_name, filtered_df in filters_results.items():
        if filtered_df.empty:
            logger.info(f"فیلتر {filter_name}: نتیجه‌ای یافت نشد")
            continue

        logger.info(f"\n🔍 پردازش فیلتر {filter_name}: {len(filtered_df)} سهم")
        value_col = FILTER_VALUE_COLUMN.get(filter_name)

        if filter_name == "filter_3_watchlist":
            # فقط کانال دوم - هرگز کانال اصلی
            if not watchlist_chat_id:
                logger.warning(
                    "⚠️ WATCHLIST_CHAT_ID تنظیم نشده - نتایج فیلتر 3 نادیده گرفته شد"
                )
                continue
            skipped_count = await _queue_filter_tasks(
                all_tasks, alert, alert_manager, filtered_df, filter_name,
                dedup_type=filter_name, chat_id=watchlist_chat_id,
                channel_label="WatchList", value_col=value_col,
                skipped_count=skipped_count,
            )
            continue

        # بقیه‌ی فیلترها: کانال اصلی طبق روال
        skipped_count = await _queue_filter_tasks(
            all_tasks, alert, alert_manager, filtered_df, filter_name,
            dedup_type=filter_name, chat_id=None, channel_label=None,
            value_col=value_col, skipped_count=skipped_count,
        )

        # کپی نمادهای واچ‌لیست شخصی به کانال دوم
        if watchlist_chat_id and personal_watchlist:
            watchlist_rows = filtered_df[filtered_df["symbol"].isin(personal_watchlist)]
            if not watchlist_rows.empty:
                skipped_count = await _queue_filter_tasks(
                    all_tasks, alert, alert_manager, watchlist_rows, filter_name,
                    dedup_type=f"{filter_name}{WATCHLIST_COPY_SUFFIX}",
                    chat_id=watchlist_chat_id, channel_label="WatchList",
                    value_col=value_col, skipped_count=skipped_count,
                )

    if all_tasks:
        logger.info(f"\n🚀 شروع ارسال موازی {len(all_tasks)} پیام...")

        tasks_only = [task for task, _, _, _, _, _, _ in all_tasks]
        results = await asyncio.gather(*tasks_only, return_exceptions=True)

        successful_marks = []

        for result, (_, symbols, dedup_type, filter_name, chunk_idx, chunk_to_send, value_col) in zip(results, all_tasks):
            if isinstance(result, Exception):
                logger.error(
                    f"❌ خطا در ارسال {filter_name} [{dedup_type}] گروه {chunk_idx}: {result}"
                )
            elif result:
                # استخراج value، is_fund، صنعت و درصد تغییر قیمت پایانی برای هر نماد
                # از chunk_to_send (industry_name و final_price_change_percent برای
                # گزارش خلاصه‌ی روزانه لازم‌ان: «برترین صنایع» و نمایش درصد کنار نماد)
                for s in symbols:
                    val = None
                    is_fund = None
                    industry_name = None
                    price_change_percent = None
                    row = chunk_to_send[chunk_to_send["symbol"] == s]
                    if not row.empty:
                        if value_col and value_col in chunk_to_send.columns:
                            try:
                                val = float(row.iloc[0][value_col])
                            except (ValueError, TypeError):
                                val = None
                        if "is_fund" in chunk_to_send.columns:
                            is_fund_val = row.iloc[0]["is_fund"]
                            if pd.notna(is_fund_val):
                                is_fund = bool(is_fund_val)
                        if "industry_name" in chunk_to_send.columns:
                            industry_val = row.iloc[0]["industry_name"]
                            if pd.notna(industry_val):
                                industry_name = str(industry_val)
                        if "final_price_change_percent" in chunk_to_send.columns:
                            try:
                                price_val = row.iloc[0]["final_price_change_percent"]
                                if pd.notna(price_val):
                                    price_change_percent = float(price_val)
                            except (ValueError, TypeError):
                                price_change_percent = None
                    successful_marks.append(
                        (s, dedup_type, val, is_fund, industry_name, price_change_percent)
                    )

                sent_count += len(symbols)
                logger.info(
                    f"✅ {filter_name} [{dedup_type}] گروه {chunk_idx}: {len(symbols)} سهم ارسال شد"
                )
            else:
                logger.error(f"❌ {filter_name} [{dedup_type}] گروه {chunk_idx}: خطا در ارسال")

        if successful_marks:
            logger.info(f"📝 علامت‌گذاری {len(successful_marks)} هشدار در Gist...")
            await alert_manager.mark_multiple_as_sent(successful_marks)

    return sent_count, skipped_count


# ===========================
# تابع اصلی
# ===========================
async def main_async():
    logger.info("=" * 80)
    logger.info("🚀 شروع Bourse Tracker")
    logger.info("=" * 80)

    try:
        t_run_start = time.time()
        validate_config()
        logger.info("✅ تنظیمات معتبر است")

        if not is_market_open():
            logger.info("⏸️  بازار بسته است. خروج از برنامه.")
            return

        logger.info("\n📥 شروع دریافت داده از API...")
        fetcher = UnifiedDataFetcher(api1_base_url=API_BASE_URL)
        df_raw = fetcher.fetch_all_data()

        alert = TelegramAlert()

        # اگه کل fetch شکست خورده باشه (مثلاً API 404/409 بده یا سایت schema رو
        # عوض کرده باشه) نباید بی‌سروصدا "موفق" تموم بشه - چون فرقی با
        # "بازار امروز سیگنالی نداشت" نداره و کسی متوجه نمی‌شه.
        if df_raw is None or df_raw.empty:
            logger.error("❌ هیچ داده‌ای از API دریافت نشد - fetch کاملاً شکست خورد")
            if ERROR_CHAT_ID:
                await alert.send_message(
                    "🔴 <b>خطای بحرانی: دریافت داده کاملاً شکست خورد</b>\n\n"
                    "هیچ رکوردی از tradersarena.ir دریافت نشد (۴۰۴/۴۰۹/تغییر schema/قطعی).\n"
                    "این اجرا هیچ فیلتری اجرا نکرد و هیچ هشداری بررسی نشد.\n"
                    "لاگ کامل رو تو GitHub Actions چک کن.",
                    chat_id=ERROR_CHAT_ID,
                )
            else:
                logger.warning(
                    "⚠️ ERROR_CHAT_ID تنظیم نشده — این خطای بحرانی فقط در لاگ ثبت شد"
                )
            return

        t_process_start = time.time()
        logger.info("\n🔄 شروع پردازش داده‌ها...")
        processor = BourseDataProcessor()
        df = processor.process_all_data(df_raw)

        logger.info("\n🔍 اعمال فیلترها...")
        all_results = processor.apply_all_filters(df)
        t_process_end = time.time()
        logger.info(f"⏱️ پردازش + فیلترها: {t_process_end - t_process_start:.1f}s")

        logger.info("\n📤 شروع ارسال هشدارها به تلگرام...")
        alert_manager = GistAlertManager(GIST_TOKEN, GIST_ID)

        # ذخیره‌ی یک‌باره‌ی تعداد کل نماد هر صنعت (universe) — برای نرمال‌سازی
        # «برترین صنایع» در گزارش خلاصه‌ی روزانه (درصد مشارکت به‌جای عدد خام).
        # save_industry_universe خودش idempotent هست، پس فراخوانی مکرر طی روز
        # بی‌خطره و فقط یک‌بار واقعاً می‌نویسه.
        if "industry_name" in df.columns:
            universe_df = df if "is_fund" not in df.columns else df[~df["is_fund"].fillna(False)]
            # json.dumps نمی‌تونه numpy.int64 رو serialize کنه، پس صریحاً int می‌کنیم
            industry_universe = {
                str(name): int(count)
                for name, count in universe_df["industry_name"].dropna().value_counts().items()
            }
            if industry_universe:
                await alert_manager.save_industry_universe(industry_universe)

        # هشدار فوری اگه یکی از فیلترها امروز خطا داده باشه (کانال جدا، نه کانال اصلی)
        if processor.failed_filters:
            failed_list = "، ".join(processor.failed_filters)
            logger.error(f"⚠️ فیلترهای خطادار این اجرا: {failed_list}")
            if ERROR_CHAT_ID:
                await alert.send_message(
                    f"⚠️ <b>خطا در اجرای فیلتر</b>\n\n"
                    f"فیلترهای زیر امروز اجرا نشدن (احتمالاً به‌خاطر تغییر schema API):\n"
                    f"<code>{failed_list}</code>\n\n"
                    f"لاگ کامل رو تو GitHub Actions چک کن.",
                    chat_id=ERROR_CHAT_ID,
                )
            else:
                logger.warning(
                    "⚠️ ERROR_CHAT_ID تنظیم نشده — هشدار خطای فیلتر فقط در لاگ ثبت شد"
                )

        personal_watchlist = set(PERSONAL_WATCHLIST)
        if not WATCHLIST_CHAT_ID:
            logger.warning(
                "⚠️ WATCHLIST_CHAT_ID تنظیم نشده — فیلتر 3 و کپی واچ‌لیست شخصی غیرفعال می‌مونن"
            )

        total_sent = 0
        total_skipped = 0
        t_send_start = time.time()

        if all_results:
            sent, skipped = await send_alerts_for_filters_async(
                alert, alert_manager, all_results, "همه‌ی فیلترها (1-11)",
                personal_watchlist, WATCHLIST_CHAT_ID,
            )
            total_sent += sent
            total_skipped += skipped

        t_send_end = time.time()
        logger.info(f"⏱️ فاز ارسال هشدارها: {t_send_end - t_send_start:.1f}s")

        stats = await alert_manager.get_today_stats()
        logger.info("\n" + "=" * 80)
        logger.info("📊 گزارش نهایی:")
        logger.info(f"  • تاریخ: {stats['date']}")
        logger.info(f"  • هشدارهای ارسال شده (این اجرا): {total_sent}")
        logger.info(f"  • هشدارهای رد شده (اسپم): {total_skipped}")
        logger.info(f"  • مجموع هشدارهای امروز: {stats['total_alerts']}")
        logger.info("  • آمار بر اساس نوع هشدار:")
        for alert_type, count in stats["alerts_by_type"].items():
            logger.info(f"    - {alert_type}: {count}")
        logger.info(f"  • Gist: {alert_manager.get_gist_url()}")
        logger.info(f"  • ⏱️ زمان کل اجرا: {time.time() - t_run_start:.1f}s")
        logger.info("=" * 80)
        logger.info("✅ اجرا با موفقیت به پایان رسید")

    except KeyboardInterrupt:
        logger.info("\n⚠️  اجرا توسط کاربر متوقف شد")
        sys.exit(0)

    except Exception as e:
        logger.error(f"\n❌ خطای غیرمنتظره: {e}", exc_info=True)
        sys.exit(1)


def main():
    """نقطه ورود اصلی برنامه"""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
