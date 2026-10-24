# utils/sheets_storage.py
"""ماژول مدیریت ذخیره‌سازی داده‌ها در Google Sheets - دو تب (Gold/Silver)"""

import functools
import json
import logging
import math
import threading
from datetime import datetime, timedelta
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import SHEET_ID, SERVICE_ACCOUNT_JSON, TIMEZONE, KEEP_DAYS, SHEET_NAMES, STANDARD_HEADER

logger = logging.getLogger(__name__)

if not SHEET_ID or not SERVICE_ACCOUNT_JSON:
    raise Exception("⚠️ SHEET_ID یا SHEETS_SERVICE_ACCOUNT در Secrets تنظیم نشده!")

NUM_COLS = len(STANDARD_HEADER)  # 16
LAST_COL_LETTER = "P"  # ستون شانزدهم — اگه STANDARD_HEADER عوض شد باید این هم عوض بشه
LEGACY_NUM_COLS = NUM_COLS - 1       # 15 — قبل از اضافه‌شدن global_change_percent
LEGACY_NUM_COLS_V0 = NUM_COLS - 2    # 14 — قبل از اضافه‌شدن trade_value
LEGACY_NUM_COLS_V00 = NUM_COLS - 3   # 13 — طرح اولیه، قبل از اضافه‌شدن shams_bubble_percent


class SheetsReadError(Exception):
    """خواندن از شیت شکست خورد (≠ شیت خالی). برای جلوگیری از نوشتن «تغییرِ صفرِ ساختگی»."""


def _sheet_name(commodity):
    if commodity not in SHEET_NAMES:
        raise ValueError(f"کالای نامعتبر: {commodity}")
    return SHEET_NAMES[commodity]


# سرویس googleapiclient روی httplib2 سوار است که thread-safe نیست؛ دو تماس هم‌زمان از دو thread
# روی همین سرویس مشترک (کش‌شده) می‌تونه کل پروسه رو با Segmentation fault بکشه.
# پس همه‌ی عملیات شیت پشت یک قفل سراسری سریال می‌شن (RLock چون ensure_header از داخل read/save صدا زده می‌شه).
_API_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _API_LOCK:
            return fn(*args, **kwargs)
    return wrapper


_service = None  # کش سراسری در سطح پروسه — یک‌بار در هر اجرای main.py ساخته می‌شه
_headers_ensured = set()  # {commodity} — برای جلوگیری از چک تکراری هدر/تب در هر خوندن/نوشتن


def get_sheets_service():
    """اتصال به Google Sheets API — نتیجه در سطح پروسه کش می‌شه تا credentials/token فقط یک‌بار ساخته بشه"""
    global _service
    if _service is not None:
        return _service
    try:
        creds_info = json.loads(SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        _service = build('sheets', 'v4', credentials=credentials, cache_discovery=False)
        return _service
    except Exception as e:
        logger.error(f"❌ خطا در اتصال به Google Sheets: {e}")
        raise


def _ensure_sheet_tab(service, sheet_name):
    """مطمئن می‌شه تب sheet_name وجود داره؛ اگه نبود می‌سازدش. sheetId عددی رو برمی‌گردونه."""
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute(num_retries=3)

    for sheet in meta.get("sheets", []):
        props = sheet["properties"]
        if props["title"] == sheet_name:
            return props["sheetId"]

    logger.info(f"📝 تب '{sheet_name}' وجود ندارد، در حال ساخت...")
    response = service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    ).execute(num_retries=3)
    new_sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]
    logger.info(f"✅ تب '{sheet_name}' ساخته شد")
    return new_sheet_id


@_locked
def ensure_header(commodity):
    """بررسی و ایجاد/آپدیت خودکار هدر برای تب یک کالا — فقط بار اول در هر پروسه واقعاً چک می‌کنه"""
    if commodity in _headers_ensured:
        return True

    sheet_name = _sheet_name(commodity)
    try:
        service = get_sheets_service()
        _ensure_sheet_tab(service, sheet_name)

        rng = f"{sheet_name}!A1:{LAST_COL_LETTER}1"
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=rng
        ).execute(num_retries=3)

        existing_values = result.get('values', [])
        existing_header = existing_values[0] if existing_values else []

        if not existing_header:
            logger.info(f"📝 [{commodity}] هدر وجود ندارد، در حال ساخت...")
            service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID, range=rng,
                valueInputOption='RAW', body={'values': [STANDARD_HEADER]}
            ).execute(num_retries=3)
            logger.info(f"✅ [{commodity}] هدر جدید ساخته شد ({NUM_COLS} ستون)")
            _headers_ensured.add(commodity)
            return True

        if len(existing_header) == NUM_COLS:
            logger.debug(f"✓ [{commodity}] هدر معتبر است ({NUM_COLS} ستون)")
            _headers_ensured.add(commodity)
            return True

        logger.warning(f"⚠️ [{commodity}] هدر نامعتبر ({len(existing_header)} ستون)")
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=rng,
            valueInputOption='RAW', body={'values': [STANDARD_HEADER]}
        ).execute(num_retries=3)
        logger.info(f"✅ [{commodity}] هدر آپدیت شد")
        _headers_ensured.add(commodity)
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


def _clean_number(v, nd=2):
    """None/NaN/inf/غیرعددی → '' (خونه‌ی خالی)؛ وگرنه float خالصِ گردشده (numpy → float برای JSON).
    NaN توی بدنه‌ی JSON توکن غیراستانداردِ NaN تولید می‌کنه و ردیف رو خراب می‌کنه."""
    if v is None:
        return ''
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ''
    if not math.isfinite(f):
        return ''
    return round(f, nd)


def _required_number(row_dict, key):
    """فیلد الزامی: باید عددِ متناهی و > 0 باشه، وگرنه ValueError (بدون نوشتن ردیف)."""
    v = _clean_number(row_dict.get(key), 6)
    if v == '' or v <= 0:
        raise ValueError(f"مقدار الزامی '{key}' نامعتبر است: {row_dict.get(key)!r}")
    return v


def _build_row(row_dict, timestamp):
    """ساخت ردیف شیت (۱۶ ستون) بدون هیچ جایگزینِ ساختگی؛ مقدار نامعتبرِ اختیاری → خونه‌ی خالی."""
    shams_change = row_dict.get('shams_change')
    shams_date = row_dict.get('shams_date', None)

    if shams_date and not is_today(shams_date):
        logger.warning(f"⚠️ داده شمش مال امروز نیست (تاریخ: {shams_date})")
        shams_change = 0.0

    # ستون‌های صندوق می‌تونن None باشن (روزی که صندوق‌ها معامله ندارن، مثلاً پنج‌شنبه):
    # خونه‌ی خالی نوشته می‌شه تا تو میانگین‌ها/گزارش‌ها NaN بشه و نه صفر یا مقدار کهنه.
    return [
        timestamp,
        round(_required_number(row_dict, 'global_price'), 2),
        int(_required_number(row_dict, 'dollar_price')),
        int(_required_number(row_dict, 'shams_price')),
        _clean_number(row_dict.get('dollar_change')),
        _clean_number(shams_change),
        _clean_number(row_dict.get('fund_change_weighted')),
        _clean_number(row_dict.get('fund_final_price_avg')),
        _clean_number(row_dict.get('fund_bubble_weighted')),
        _clean_number(row_dict.get('sarane_kharid_w')),
        _clean_number(row_dict.get('sarane_forosh_w')),
        _clean_number(row_dict.get('ekhtelaf_sarane_w')),
        _clean_number(row_dict.get('pol_hagigi')),
        _clean_number(row_dict.get('shams_bubble')),
        _clean_number(row_dict.get('trade_value')),
        _clean_number(row_dict.get('global_change')),
    ]


@_locked
def save_to_sheets(commodity, row_dict):
    """
    ذخیره یک ردیف جدید در تب مربوط به یک کالا (Gold یا Silver).

    ورودی (row_dict): global_price, dollar_price, shams_price (الزامی، > 0)؛ بقیه اختیاری
    (None/NaN → خونه‌ی خالی): dollar_change, shams_change, shams_date, fund_change_weighted,
    fund_final_price_avg, fund_bubble_weighted, sarane_kharid_w, sarane_forosh_w,
    ekhtelaf_sarane_w, pol_hagigi, shams_bubble, trade_value, global_change.

    Returns:
        True اگه ردیف واقعاً نوشته شد؛ False در هر شکستی (تا caller بتونه ارسال/هشدار رو رد کنه).
    """
    sheet_name = _sheet_name(commodity)
    try:
        ensure_header(commodity)
        service = get_sheets_service()
        tz = pytz.timezone(TIMEZONE)
        timestamp = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')

        new_row = _build_row(row_dict, timestamp)

        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{sheet_name}!A:{LAST_COL_LETTER}",
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': [new_row]}
        ).execute(num_retries=2)  # append غیرایدمپوتنته؛ تعداد retry عمداً کمتره

        logger.info(f"✅ [{commodity}] داده در Sheet ذخیره شد: {timestamp}")
        return True

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در ذخیره‌سازی در Google Sheet: {e}", exc_info=True)
        return False


@_locked
def read_from_sheets(commodity, limit=1000, raise_on_error=False):
    """
    خواندن داده‌ها از تب مربوط به یک کالا

    Args:
        commodity: 'gold' یا 'silver'
        limit: حداکثر تعداد ردیف‌های برگشتی (پیش‌فرض 1000)
        raise_on_error: اگه True، خطای خواندن به‌جای [] به‌صورت SheetsReadError بالا میاد

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
        ).execute(num_retries=3)

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
                # ردیف قدیمی (قبل از اضافه‌شدن global_change_percent) — پد با مقدار خنثی
                valid_rows.append(row + [""])
            elif len(row) == LEGACY_NUM_COLS_V0:
                # ردیف قدیمی‌تر (قبل از اضافه‌شدن trade_value)
                valid_rows.append(row + ["", ""])
            elif len(row) == LEGACY_NUM_COLS_V00:
                # ردیف خیلی قدیمی (قبل از shams_bubble_percent و trade_value)
                valid_rows.append(row + ["", "", ""])
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
        if raise_on_error:
            raise SheetsReadError(str(e)) from e
        return []


@_locked
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
        ).execute(num_retries=3)

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
            ).execute(num_retries=3)
            logger.info(f"🗑️ [{commodity}] {rows_to_delete} ردیف قدیمی پاک شد")
        else:
            logger.info(f"✅ [{commodity}] داده قدیمی برای پاک کردن پیدا نشد")

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در پاک‌سازی: {e}", exc_info=True)
