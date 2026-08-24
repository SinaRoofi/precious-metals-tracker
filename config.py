"""تنظیمات و ثابت‌های پروژه Gold & Silver Market Tracker"""

import os

# ════════════════════════════════════════════════════════════════
# 🚨 آستانه‌های هشدار قیمتی
# ════════════════════════════════════════════════════════════════

DOLLAR_HIGH = 203_000
DOLLAR_LOW = 202_000

# --- طلا ---
SHAMS_HIGH = 30_000_000
SHAMS_LOW = 29_500_000

GOLD_HIGH = 4800
GOLD_LOW = 4600

# --- نقره ---
SILVER_SHAMS_HIGH = 500_000
SILVER_SHAMS_LOW = 400_000

SILVER_HIGH = 70
SILVER_LOW = 60

# --- هشدار قیمتی نمادهای صندوق 
FUND_PRICE_ALERTS = {
    "گنج": {"high": 193_263, "low": 191_115},
}

ALERT_THRESHOLD_PERCENT = {
    "dollar": 0.5,
    "gold": 0.5,
    "silver": 1.0,
}
EKHTELAF_THRESHOLD = 20

# 🎯 مقادیر پیش‌فرض (Fallback)
DEFAULT_GOLD_PRICE = 4604
DEFAULT_DOLLAR_PRICE = 198_000
DEFAULT_SILVER_PRICE = 69

# 🎈 آستانه‌های هشدار حباب
BUBBLE_SHARP_CHANGE_THRESHOLD = 1

# ✅ آستانه‌های هشدار پول حقیقی
POL_SHARP_CHANGE_THRESHOLD = 100

# 📌 هندل کانال تلگرام — مشترک برای طلا و نقره
CHANNEL_HANDLE = "@PreciousMetals_IR"
ALERT_CHANNEL_HANDLE = "@ALERT_METALS"

# ارزش روزانه دلار تومان
VALUE_DIFF = 111_000
LOW_VALUE = 162
VALUE = 285
HIGH_VALUE = 581

# ════════════════════════════════════════════════════════════════
# 🔐 متغیرهای محیطی (Environment Variables)
# ════════════════════════════════════════════════════════════════

GIST_ID = os.getenv("GIST_ID")
GIST_TOKEN = os.getenv("GIST_TOKEN")
ALERT_STATUS_FILE = "alert_status.json"
MESSAGE_ID_FILE = "message_id.json"

SHEET_ID = os.getenv("SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SHEETS_SERVICE_ACCOUNT")

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
TELETHON_API_ID = int(os.getenv('TELETHON_API_ID', 0))
TELETHON_API_HASH = os.getenv('TELETHON_API_HASH')
TELEGRAM_SESSION = os.getenv('TELEGRAM_SESSION')
TELEGRAM_ALERT_CHAT_ID = os.getenv('TELEGRAM_ALERT_CHAT_ID')

# ════════════════════════════════════════════════════════════════
# 📡 کانال‌های تلگرام
# ════════════════════════════════════════════════════════════════

TELEGRAM_CHANNELS = {
    'dollar': 'dollar_tehran3bze',
}

# ════════════════════════════════════════════════════════════════
# 🌐 API URLs — nested بر اساس کالا
# ════════════════════════════════════════════════════════════════

API_URLS = {
    'gold': {
        'intrinsic': 'https://rahavard365.com/api/v2/gold/intrinsic-values',
        'light_charts': 'https://rahavard365.com/api/v2/gold/light-charts',
        # ⚠️ ۲۰۲۶/۰۵/۲۵: ساختار سایت تریدرآرنا عوض شد. اندپوینت قدیمی
        # (data/industries-stocks-csv/gold-funds) حذف شده و الان یک
        # snapshot JSON تودرتو برمی‌گرداند (نه دیگر آرایه‌ی ایندکس‌دار).
        # timeframe=12 پارامتر بازه‌ی ریسک است؛ همانی که سایت خودش می‌فرستد.
        'funds': 'https://tradersarena.ir/data/industries/gold-funds/snapshot',
    },
    'silver': {
        'intrinsic': 'https://rahavard365.com/api/v2/silver/intrinsic-values',
        'light_charts': 'https://rahavard365.com/api/v2/silver/light-charts',
        'funds': 'https://tradersarena.ir/data/industries/silver-funds/snapshot',
    },
}

# پارامتر timeframe برای اندپوینت جدید snapshot صندوق‌های تریدرآرنا
FUNDS_SNAPSHOT_TIMEFRAME = 12

# ════════════════════════════════════════════════════════════════
# ⏰ تنظیمات زمانی
# ════════════════════════════════════════════════════════════════

TIMEZONE = 'Asia/Tehran'

# ════════════════════════════════════════════════════════════════
# 🎨 تنظیمات نمودارها
# ════════════════════════════════════════════════════════════════

CHART_WIDTH = 1400
CHART_HEIGHT = 2200
CHART_SCALE = 2
WEEKLY_CHART_HEIGHT = 3000  # 6 subplot (بازده دلار/اونس/شمش، ارزش معاملات، حباب شمش، حباب صندوق‌ها، پول حقیقی تجمعی، سرانه)
MONTHLY_CHART_WIDTH = 2000  # پهن‌تر از هفتگی چون ~۲۲-۲۶ تیک روزانه باید جا بشه
MONTHLY_CHART_HEIGHT = WEEKLY_CHART_HEIGHT  # همون ۶ پنل، فقط بازه‌ی زمانی متفاوت

TREEMAP_WIDTH = 1350
TREEMAP_HEIGHT = 1350
TREEMAP_SCALE = 2

COLOR_POSITIVE = '#00E676'
COLOR_NEGATIVE = '#FF1744'
COLOR_NEUTRAL = '#2C2C2C'
COLOR_BACKGROUND = '#0D1117'
COLOR_GRID = '#21262D'
COLOR_GOLD = '#FFD700'
COLOR_SILVER = '#C0C0C0'

TREEMAP_COLORSCALE = [
    [0.0, "#E57373"], [0.1, "#D85C5C"], [0.2, "#C94444"],
    [0.3, "#A52A2A"], [0.4, "#6B1A1A"],
    [0.5, "#2C2C2C"],
    [0.6, "#1B5E20"], [0.7, "#2E7D32"], [0.8, "#43A047"],
    [0.9, "#5CB860"], [1.0, "#66BB6A"],
]

Y_AXIS_STEP = 50

# ════════════════════════════════════════════════════════════════
# 📝 دارایی‌های داخلی هر کالا (ترتیب = ترتیب ایندکس در calculate_values)
# ════════════════════════════════════════════════════════════════

BULLION_ASSET = {
    'gold': 'شمش-طلا',
    'silver': 'شمش-نقره',
}

ASSET_ORDER = {
    'gold': [
        "طلا-گرم-18-عیار",
        "طلا-گرم-24-عیار",
        "شمش-طلا",
        "سطلا",
        "سکه-امامی-طرح-جدید",
        "سکه-بهار-آزادی-طرح-قدیم",
        "طلا-مظنه-آبشده-تهران",
        "سکه0312پ01",
        "سکه0411پ05",
        "سکه0412پ03",
        "نیم-سکه",
        "ربع-سکه",
        "سکه-1-گرمی",
    ],
    'silver': [
        "شمش-نقره",
        "نقره-گرمی-999",
    ],
}

# ════════════════════════════════════════════════════════════════
# 💰 ضرایب فرمول ارزش ذاتی (Value) — از calculate_values موجود استخراج شده
# هر آیتم متناظر با همون ایندکس در ASSET_ORDER[commodity] است
# فرمول: Value = ((purity * dollar * global_price) / TROY_OZ) * weight * scale
# ════════════════════════════════════════════════════════════════

TROY_OZ = 31.1034768

PRICING_FACTORS = {
    'gold': [
        {'purity': 0.75,  'weight': 1,       'scale': 10},    # طلا-گرم-18-عیار
        {'purity': 0.995, 'weight': 1,       'scale': 10},    # طلا-گرم-24-عیار
        {'purity': 0.995, 'weight': 1,       'scale': 1},     # شمش-طلا
        {'purity': 0.9,   'weight': 8.133,   'scale': 10},    # سطلا
        {'purity': 0.9,   'weight': 8.133,   'scale': 10},    # سکه-امامی-طرح-جدید
        {'purity': 0.9,   'weight': 8.133,   'scale': 10},    # سکه-بهار-آزادی-طرح-قدیم
        {'purity': 0.705, 'weight': 4.6083,  'scale': 10},    # طلا-مظنه-آبشده-تهران
        {'purity': 0.9,   'weight': 8.133,   'scale': 0.01},  # سکه0312پ01
        {'purity': 0.9,   'weight': 8.133,   'scale': 0.01},  # سکه0411پ05
        {'purity': 0.9,   'weight': 8.133,   'scale': 0.01},  # سکه0412پ03
        {'purity': 0.9,   'weight': 4.0665,  'scale': 10},    # نیم-سکه
        {'purity': 0.9,   'weight': 2.03225, 'scale': 10},    # ربع-سکه
        {'purity': 0.9,   'weight': 1,       'scale': 10},    # سکه-1-گرمی
    ],
    'silver': [
        {'purity': 0.999, 'weight': 1, 'scale': 10},  # شمش-نقره
        {'purity': 0.999, 'weight': 1, 'scale': 10},  # نقره-گرمی-999
    ],
}

# ════════════════════════════════════════════════════════════════
# 🔤 مسیر فونت‌ها
# ════════════════════════════════════════════════════════════════

FONT_BOLD_PATH = "assets/fonts/Vazirmatn-Bold.ttf"
FONT_MEDIUM_PATH = "assets/fonts/Vazirmatn-Medium.ttf"
FONT_REGULAR_PATH = "assets/fonts/Vazirmatn-Regular.ttf"

# ════════════════════════════════════════════════════════════════
# 🔄 تنظیمات Retry و Network
# ════════════════════════════════════════════════════════════════

MAX_RETRIES = 2
RETRY_DELAY = 3
REQUEST_TIMEOUT = (5, 15)

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ════════════════════════════════════════════════════════════════
# 📊 تنظیمات Google Sheets — یک Spreadsheet، دو تب (Gold / Silver)
# ════════════════════════════════════════════════════════════════

SHEET_NAMES = {
    'gold': 'Gold',
    'silver': 'Silver',
}

STANDARD_HEADER = [
    'timestamp',
    'global_price_usd',
    'dollar_price',
    'shams_price',
    'dollar_change_percent',
    'shams_change_percent',
    'fund_weighted_change_percent',
    'fund_final_price_avg',
    'fund_weighted_bubble_percent',
    'sarane_kharid_weighted',
    'sarane_forosh_weighted',
    'ekhtelaf_sarane_weighted',
    'pol_hagigi',
    'shams_bubble_percent',
    'trade_value',  # ⚠️ ستون جدید — همیشه در انتها اضافه شود، نه وسط (سازگاری با ردیف‌های قدیمی)
]

KEEP_DAYS = 40  # باید کل یک ماه شمسی (تا ۳۱ روز) رو پوشش بده، برای گزارش ماهانه

# ════════════════════════════════════════════════════════════════
# 📝 تنظیمات Logging
# ════════════════════════════════════════════════════════════════

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_FILE = 'market_tracker.log'
LOG_LEVEL = 'INFO'

# ════════════════════════════════════════════════════════════════
# 📌 تنظیمات Telegram Message
# ════════════════════════════════════════════════════════════════

MAX_MESSAGES_TO_CHECK = 10000
MESSAGE_BATCH_SIZE = 100
