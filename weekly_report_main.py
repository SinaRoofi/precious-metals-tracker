# weekly_report_main.py

import logging
import os
import sys
from datetime import datetime

import jdatetime
import pytz

from config import LOG_FILE, LOG_FORMAT, LOG_LEVEL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE
from utils.holidays import is_iranian_holiday
from utils.telegram_sender import send_weekly_report


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
    force_run = os.getenv("FORCE_RUN", "false").lower() == "true"

    logger.info("=" * 60)
    logger.info("📅 شروع اجرای گزارش هفتگی")
    logger.info("=" * 60)

    if is_iranian_holiday(now) and not force_run:
        logger.info(f"🏖️ امروز ({now.strftime('%Y-%m-%d')}) تعطیل است — گزارش هفتگی ارسال نمی‌شود")
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_BOT_TOKEN یا TELEGRAM_CHAT_ID در Secrets تنظیم نشده!")
        return

    for commodity in COMMODITIES:
        logger.info(f"▶️ [{commodity}] در حال ساخت و ارسال گزارش هفتگی...")
        success = send_weekly_report(commodity, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        logger.info(f"{'✅' if success else '⚠️'} [{commodity}] گزارش هفتگی {'ارسال شد' if success else 'ارسال نشد'}")

    logger.info("=" * 60)
    logger.info("✅ اجرای گزارش هفتگی به پایان رسید")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"💥 خطای بحرانی: {e}", exc_info=True)
        sys.exit(1)
