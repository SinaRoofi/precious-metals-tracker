# monthly_report_main.py
#
# این اسکریپت هر روز اجرا می‌شود (چون cron گیت‌هاب اکشن فقط تاریخ میلادی می‌شناسد و
# اول ماه شمسی روی یک تاریخ میلادی ثابت نمی‌افتد)، ولی فقط وقتی که امروز واقعاً
# روز ۱ام یک ماه شمسی باشد گزارش می‌سازد و می‌فرستد؛ در غیر این صورت بی‌خیال می‌شود.

import logging
import os
import sys
from datetime import datetime

import jdatetime
import pytz

from config import LOG_FILE, LOG_FORMAT, LOG_LEVEL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE
from utils.telegram_sender import send_monthly_report


class JalaliFormatter(logging.Formatter):
    """Formatter که %(asctime)s رو با تاریخ و ساعت شمسی (تهران) پر می‌کند — همان منطق main.py"""

    def formatTime(self, record, datefmt=None):
        tehran_tz = pytz.timezone(TIMEZONE)
        dt = datetime.fromtimestamp(record.created, tz=tehran_tz)
        jalali = jdatetime.datetime.fromgregorian(datetime=dt)
        return jalali.strftime("%Y-%m-%d %H:%M:%S")


_formatter = JalaliFormatter(LOG_FORMAT)
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_formatter)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_formatter)

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), handlers=[_file_handler, _stream_handler])
logger = logging.getLogger(__name__)

COMMODITIES = ["gold", "silver"]


def main():
    tehran_tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tehran_tz)
    today_jalali = jdatetime.date.fromgregorian(date=now.date())
    force_run = os.getenv("FORCE_RUN", "false").lower() == "true"

    logger.info("=" * 60)
    logger.info("📅 شروع اجرای گزارش ماهانه")
    logger.info("=" * 60)

    if today_jalali.day != 1 and not force_run:
        logger.info(f"ℹ️ امروز ({today_jalali}) روز اول ماه شمسی نیست — گزارش ماهانه ارسال نمی‌شود")
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_BOT_TOKEN یا TELEGRAM_CHAT_ID در Secrets تنظیم نشده!")
        return

    for commodity in COMMODITIES:
        logger.info(f"▶️ [{commodity}] در حال ساخت و ارسال گزارش ماهانه...")
        success = send_monthly_report(commodity, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        logger.info(f"{'✅' if success else '⚠️'} [{commodity}] گزارش ماهانه {'ارسال شد' if success else 'ارسال نشد'}")

    logger.info("=" * 60)
    logger.info("✅ اجرای گزارش ماهانه به پایان رسید")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"💥 خطای بحرانی: {e}", exc_info=True)
        sys.exit(1)
