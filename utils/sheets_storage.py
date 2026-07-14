# utils/sheets_storage.py
"""ماژول مدیریت ذخیره‌سازی داده‌ها در Google Sheets - دو تب (Gold/Silver)"""

import json
import logging
from datetime import datetime, timedelta
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import SHEET_ID, SERVICE_ACCOUNT_JSON, TIMEZONE, KEEP_DAYS, SHEET_NAMES, STANDARD_HEADER

logger = logging.getLogger(__name__)

if not SHEET_ID or not SERVICE_ACCOUNT_JSON:
    raise Exception("⚠️ SHEET_ID یا SHEETS_SERVICE_ACCOUNT در Secrets تنظیم نشده!")

NUM_COLS = len(STANDARD_HEADER)  # 14
LAST_COL_LETTER = "N"  # ستون چهاردهم — اگه STANDARD_HEADER عوض شد باید این هم عوض بشه
LEGACY_NUM_COLS = NUM_COLS - 1  # 13 — طرح قدیمی، قبل از اضافه‌شدن shams_bubble_percent


def _sheet_name(commodity):
    if commodity not in SHEET_NAMES:
        raise ValueError(f"کالای نامعتبر: {commodity}")
    return SHEET_NAMES[commodity]


def get_sheets_service():
    """اتصال به Google Sheets API"""
    try:
        creds_info = json.loads(SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        return build('sheets', 'v4', credentials=credentials, cache_discovery=False)
    except Exception as e:
        logger.error(f"❌ خطا در اتصال به Google Sheets: {e}")
        raise


def _ensure_sheet_tab(service, sheet_name):
    """مطمئن می‌شه تب sheet_name وجود داره؛ اگه نبود می‌سازدش. sheetId عددی رو برمی‌گردونه."""
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()

    for sheet in meta.get("sheets", []):
        props = sheet["properties"]
        if props["title"] == sheet_name:
            return props["sheetId"]

    logger.info(f"📝 تب '{sheet_name}' وجود ندارد، در حال ساخت...")
    response = service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    ).execute()
    new_sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]
    logger.info(f"✅ تب '{sheet_name}' ساخته شد")
    return new_sheet_id


def ensure_header(commodity):
    """بررسی و ایجاد/آپدیت خودکار هدر برای تب یک کالا"""
    sheet_name = _sheet_name(commodity)
    try:
        service = get_sheets_service()
        _ensure_sheet_tab(service, sheet_name)

        rng = f"{sheet_name}!A1:{LAST_COL_LETTER}1"
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=rng
        ).execute()

        existing_values = result.get('values', [])
        existing_header = existing_values[0] if existing_values else []

        if not existing_header:
            logger.info(f"📝 [{commodity}] هدر وجود ندارد، در حال ساخت...")
            service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID, range=rng,
                valueInputOption='RAW', body={'values': [STANDARD_HEADER]}
            ).execute()
            logger.info(f"✅ [{commodity}] هدر جدید ساخته شد ({NUM_COLS} ستون)")
            return True

        if len(existing_header) == NUM_COLS:
            logger.debug(f"✓ [{commodity}] هدر معتبر است ({NUM_COLS} ستون)")
            return True

        logger.warning(f"⚠️ [{commodity}] هدر نامعتبر ({len(existing_header)} ستون)")
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=rng,
            valueInputOption='RAW', body={'values': [STANDARD_HEADER]}
        ).execute()
        logger.info(f"✅ [{commodity}] هدر آپدیت شد")
        return True

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در بررسی/ساخت هدر: {e}", exc_info=True)
        return False


def is_today(date_str):
    """چک می‌کنه که تاریخ داده شده مال امروز هست یا نه"""
    try:
        tz = pytz.timezone(TIMEZONE)
        today = datetime.now(tz).strftime('%Y-%m-%d')
        return date_str == today
    except Exception:
        return False


def save_to_sheets(commodity, row_dict):
    """
    ذخیره یک ردیف جدید در تب مربوط به یک کالا (Gold یا Silver)

    Args:
        commodity: 'gold' یا 'silver'
        row_dict: دیکشنری حاوی داده‌های یک ردیف:
            - global_price: قیمت جهانی انس (دلار)
            - dollar_price: قیمت دلار (تومان)
            - shams_price: قیمت شمش (ریال)
            - dollar_change: درصد تغییر دلار
            - shams_change: درصد تغییر شمش
            - shams_date: تاریخ معاملات شمش (اختیاری)
            - fund_change_weighted: میانگین وزنی تغییر صندوق‌ها
            - fund_final_price_avg: میانگین ساده قیمت پایانی (اختیاری، پیش‌فرض 0)
            - fund_bubble_weighted: میانگین وزنی حباب
            - sarane_kharid_w: سرانه خرید
            - sarane_forosh_w: سرانه فروش
            - ekhtelaf_sarane_w: اختلاف سرانه
            - pol_hagigi: پول حقیقی (میلیارد تومان، اختیاری، پیش‌فرض 0)
            - shams_bubble: درصد حباب شمش (dfp.loc[bullion_key, "Bubble"]، اختیاری، پیش‌فرض 0)
    """
    sheet_name = _sheet_name(commodity)
    try:
        ensure_header(commodity)
        service = get_sheets_service()
        tz = pytz.timezone(TIMEZONE)
        timestamp = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')

        shams_change = row_dict['shams_change']
        shams_date = row_dict.get('shams_date', None)

        if shams_date and not is_today(shams_date):
            logger.warning(f"⚠️ [{commodity}] داده شمش مال امروز نیست (تاریخ: {shams_date})")
            shams_change = 0.0

        new_row = [
            timestamp,
            round(row_dict['global_price'], 2),
            int(row_dict['dollar_price']),
            int(row_dict['shams_price']),
            round(row_dict['dollar_change'], 2),
            round(shams_change, 2),
            round(row_dict['fund_change_weighted'], 2),
            round(row_dict.get('fund_final_price_avg', 0), 2),
            round(row_dict['fund_bubble_weighted'], 2),
            round(row_dict['sarane_kharid_w'], 2),
            round(row_dict['sarane_forosh_w'], 2),
            round(row_dict['ekhtelaf_sarane_w'], 2),
            round(row_dict.get('pol_hagigi', 0), 2),
            round(row_dict.get('shams_bubble', 0), 2),
        ]

        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{sheet_name}!A:{LAST_COL_LETTER}",
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': [new_row]}
        ).execute()

        logger.info(f"✅ [{commodity}] داده در Sheet ذخیره شد: {timestamp}")

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در ذخیره‌سازی در Google Sheet: {e}", exc_info=True)


def read_from_sheets(commodity, limit=1000):
    """
    خواندن داده‌ها از تب مربوط به یک کالا

    Args:
        commodity: 'gold' یا 'silver'
        limit: حداکثر تعداد ردیف‌های برگشتی (پیش‌فرض 1000)

    Returns:
        list: لیستی از ردیف‌ها (هر ردیف یک لیست 13 عنصری)
    """
    sheet_name = _sheet_name(commodity)
    try:
        ensure_header(commodity)
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{sheet_name}!A:{LAST_COL_LETTER}"
        ).execute()

        values = result.get('values', [])
        if not values:
            logger.warning(f"⚠️ [{commodity}] Sheet خالی است")
            return []

        data_rows = values[1:]
        valid_rows = []
        invalid_count = 0

        for row in data_rows:
            if len(row) == NUM_COLS:
                valid_rows.append(row)
            elif len(row) == LEGACY_NUM_COLS:
                # ردیف قدیمی (قبل از اضافه‌شدن shams_bubble_percent) — به‌جای دور انداختن،
                # با مقدار خنثی پد می‌کنیم تا تاریخچه‌ی گزارش هفتگی از دست نره.
                valid_rows.append(row + [""])
            else:
                invalid_count += 1

        if invalid_count > 0:
            logger.warning(f"⚠️ [{commodity}] {invalid_count} ردیف نامعتبر نادیده گرفته شد")

        if len(valid_rows) > limit:
            valid_rows = valid_rows[-limit:]

        logger.info(f"✅ [{commodity}] {len(valid_rows)} ردیف از Sheet خوانده شد")
        return valid_rows

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در خواندن از Google Sheet: {e}", exc_info=True)
        return []


def clear_old_data(commodity, keep_days=None):
    """پاک کردن داده‌های قدیمی‌تر از X روز در تب یک کالا"""
    if keep_days is None:
        keep_days = KEEP_DAYS

    sheet_name = _sheet_name(commodity)
    try:
        service = get_sheets_service()
        sheet_id = _ensure_sheet_tab(service, sheet_name)
        tz = pytz.timezone(TIMEZONE)
        cutoff_date = datetime.now(tz) - timedelta(days=keep_days)

        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{sheet_name}!A:{LAST_COL_LETTER}"
        ).execute()

        values = result.get('values', [])
        if len(values) <= 1:
            logger.info(f"ℹ️ [{commodity}] داده‌ای برای پاکسازی وجود ندارد")
            return

        first_valid_row = 2
        for i, row in enumerate(values[1:], start=2):
            if not row or len(row) < 1:
                continue
            try:
                row_date = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
                row_date = tz.localize(row_date)
                if row_date >= cutoff_date:
                    first_valid_row = i
                    break
            except Exception:
                continue

        if first_valid_row > 2:
            rows_to_delete = first_valid_row - 2
            service.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={
                    'requests': [{
                        'deleteDimension': {
                            'range': {
                                'sheetId': sheet_id,
                                'dimension': 'ROWS',
                                'startIndex': 1,
                                'endIndex': first_valid_row - 1
                            }
                        }
                    }]
                }
            ).execute()
            logger.info(f"🗑️ [{commodity}] {rows_to_delete} ردیف قدیمی پاک شد")
        else:
            logger.info(f"✅ [{commodity}] داده قدیمی برای پاک کردن پیدا نشد")

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در پاک‌سازی: {e}", exc_info=True)


def clear_invalid_rows(commodity):
    """پاک کردن ردیف‌هایی که تعداد ستون درستی ندارن، در تب یک کالا"""
    sheet_name = _sheet_name(commodity)
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{sheet_name}!A:{LAST_COL_LETTER}"
        ).execute()

        values = result.get('values', [])
        if len(values) <= 1:
            logger.info(f"ℹ️ [{commodity}] فقط هدر وجود دارد")
            return

        header = values[0]
        valid_rows = [header]
        invalid_count = 0

        for row in values[1:]:
            if len(row) == NUM_COLS:
                valid_rows.append(row)
            else:
                invalid_count += 1

        if invalid_count == 0:
            logger.info(f"✅ [{commodity}] همه ردیف‌ها معتبرند")
            return

        logger.info(f"🧹 [{commodity}] در حال پاکسازی {invalid_count} ردیف نامعتبر...")

        rng = f"{sheet_name}!A:{LAST_COL_LETTER}"
        service.spreadsheets().values().clear(spreadsheetId=SHEET_ID, range=rng).execute()
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=rng,
            valueInputOption='RAW', body={'values': valid_rows}
        ).execute()

        logger.info(f"✅ [{commodity}] {invalid_count} ردیف نامعتبر پاک شد")

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در پاکسازی: {e}", exc_info=True)


def get_sheet_stats(commodity):
    """دریافت آمار تب یک کالا"""
    try:
        rows = read_from_sheets(commodity, limit=10000)
        if not rows:
            return {"total_rows": 0, "oldest": None, "newest": None}

        timestamps = [row[0] for row in rows if len(row) > 0]

        return {
            "total_rows": len(rows),
            "oldest": timestamps[0] if timestamps else None,
            "newest": timestamps[-1] if timestamps else None,
        }
    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در دریافت آمار: {e}")
        return {"total_rows": 0, "oldest": None, "newest": None}
