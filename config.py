"""
تنظیمات پروژه Bourse Tracker
"""

import os
from dotenv import load_dotenv

# بارگذاری متغیرهای محیطی از فایل .env
load_dotenv()

# ========================================
# تنظیمات تلگرام
# ========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ERROR_CHAT_ID = os.getenv("ERROR_CHAT_ID", "")  # اختیاری: کانال جدا برای خطاهای فنی (فیلتر خراب)
WATCHLIST_CHAT_ID = os.getenv("WATCHLIST_CHAT_ID", "")  # اختیاری: کانال دوم برای واچ‌لیست شخصی

# ========================================
# تنظیمات API
# ========================================
API_BASE_URL = os.getenv("API_BASE_URL")

# ========================================
# GitHub Gist
# ========================================
GIST_TOKEN = os.getenv("GIST_TOKEN")
GIST_ID = os.getenv("GIST_ID")

# ========================================
# کدهای صنایع مختلف بورس
# ========================================
INDUSTRY_CODES = [
    "01",
    "10",
    "11",
    "13",
    "14",
    "17",
    "19",
    "20",
    "21",
    "22",
    "23",
    "25",
    "27",
    "28",
    "29",
    "31",
    "32",
    "34",
    "38",
    "39",
    "40",
    "42",
    "43",
    "44",
    "47",
    "49",
    "53",
    "54",
    "55",
    "56",
    "57",
    "58",
    "60",
    "64",
    "66",
    "67",
    "70",
    "72",
    "73",
    "74",
]

# نام صنایع (برای هشتگ)
INDUSTRY_NAMES = {
    "01": "زراعت",
    "10": "ذغال_سنگ",
    "11": "استخراج_نفت",
    "13": "کانه_فلزی",
    "14": "سایر_معادن",
    "17": "منسوجات",
    "19": "محصولات_چرمی",
    "20": "محصولات_چوبی",
    "21": "محصولات_کاغذ",
    "22": "انتشار_و_چاپ",
    "23": "فرآورده_نفتی",
    "25": "لاستیک",
    "27": "فلزات_اساسی",
    "28": "محصولات_فلزی",
    "29": "ماشین_آلات",
    "31": "دستگاه_های_برقی",
    "32": "وسایل_ارتباطی",
    "34": "خودرو",
    "38": "قند_و_شکر",
    "39": "چند_رشته_ای_صنعتی",
    "40": "تامین_آب_برق_گاز",
    "42": "غذایی_بجز_قند",
    "43": "مواد_دارویی",
    "44": "شیمیایی",
    "47": "خرده_فروشی",
    "49": "کاشی_سرامیک",
    "53": "سیمان",
    "54": "کانی_غیرفلزی",
    "55": "هتل_رستوران",
    "56": "سرمایه_گذاری",
    "57": "بانک",
    "58": "سایر_مالی",
    "60": "حمل_نقل",
    "64": "رادیویی",
    "66": "بیمه_بازنشسته",
    "67": "اداره_بازارهای_مالی",
    "70": "انبوه_سازی",
    "72": "رایانه",
    "73": "اطلاعات_ارتباطات",
    "74": "فنی_مهندسی",
}

# ========================================
# تنظیمات صندوق‌ها
# ========================================
FUND_TYPES = {
    "index": {
        "slug": "index-funds",
        "name": "صندوق‌های شاخصی",
        "enabled": True,
    },
    "real_state": {
        "slug": "real-state-funds",
        "name": "صندوق‌های املاک",
        "enabled": True,
    },
    "fund_in_fund": {
        "slug": "fund-in-funds",
        "name": "صندوق‌های فراصندوق",
        "enabled": True,
    },
    "classic_stock": {
        "slug": "classic-stock-funds",
        "name": "صندوق‌های سهامی کلاسیک",
        "enabled": True,
    },
    "mixed": {
        "slug": "mixed-funds",
        "name": "صندوق‌های مختلط",
        "enabled": True,
    },
    "energy": {
        "slug": "energy-funds",
        "name": "صندوق‌های انرژی",
        "enabled": True,
    },
    "leveraged": {
        "slug": "leveraged-funds",
        "name": "صندوق‌های اهرمی",
        "enabled": True,
    },
    "sector": {
        "slug": "sector-funds",
        "name": "صندوق‌های بخشی",
        "enabled": True,
    },
}

# ========================================
# تنظیمات فیلترها
# ========================================

# فیلتر 1: قدرت خرید قوی
STRONG_BUYING_CONFIG = {
    "min_value_to_avg_monthly": 3,
    "min_sarane_kharid": 100,  # میلیون تومان
    "min_godrat_kharid": 2,
    "godrat_greater_than_5day": True,  # قدرت خرید > میانگین 5 روز
    "godrat_5day_multiplier": 2,  # ضریب مقایسه با میانگین 5 روزه (فقط وقتی بالا True باشه اثر داره)
}

# فیلتر 2: کراس سرانه خرید
SARANE_CROSS_CONFIG = {
    "sarane_kharid_greater_than_forosh": True,
    "min_value_to_avg_monthly": 0.5,
    "min_sarane_kharid": 100,
    "max_buy_queue_value": 2,  # میلیارد تومان - ارزش سفارش ردیف اول صف خرید باید کمتر از این باشه
}

# فیلتر 3: واچ‌لیست شخصی — عبور از یک آستانه‌ی واحد
PERSONAL_WATCHLIST = [
    "شپنا",
    "فملی",
    "رافزا",
    "دجابر",
    "فزر",
    "حتاید",
    "هجرت",
    
]
PERSONAL_WATCHLIST_THRESHOLD = 2.5  # درصد تغییر قیمت
PERSONAL_WATCHLIST_SKIP_IF_BUY_QUEUE = True

# فیلتر 4: رنج مثبت
range_mosbat = {
    "tick_diff_percent": 2.0,
    "min_value_to_avg_monthly": 0.5,
}

# فیلتر 5: ورود پول حقیقی قوی
POL_HAGIGI_FILTER_CONFIG = {
    "min_pol_to_value_ratio": 0.5,
    "min_sarane_kharid": 100,
    "min_godrat_kharid": 1.5,
}

# فیلتر 6: تیک و ساعت
TICK_FILTER_CONFIG = {
    "first_to_low_ratio": 0.99,
    "last_to_first_ratio": 0.99,
    "tick_diff_percent": 1.0,
}

# فیلتر 7: حجم مشکوک
SUSPICIOUS_VOLUME_CONFIG = {
    "min_value_to_avg_ratio": 2.0,
}

# فیلتر 8: نوسان‌ گیری
SWING_TRADE_CONFIG = {
    "min_allowed_price": -2.8,
    "max_last_change_percent": -1.0,
    "min_godrat_kharid": 2.0,
    "min_sarane_kharid": 100, 
    "min_value_to_avg_monthly": 1.0,
}

# فیلتر 9: نیم ساعت اول (۹:۰۰ تا ۹:۳۰)
FIRST_HOUR_CONFIG = {
    "min_value_to_avg_ratio": 1.0,
    "start_hour": 9,
    "start_minute": 0,
    "end_hour": 9,
    "end_minute": 30,
}

# فیلتر 10: صف خرید با اردر سنگین (بالای ۱۰ میلیارد)
HEAVY_BUY_QUEUE_CONFIG = {
    "min_buy_order": 100,  # میلیون تومان
    "min_buy_queue_value": 10,  # میلیارد تومان
    "price_at_ceiling": True,  # آخرین قیمت = سقف
}

# فیلتر 14: صف خرید ساده (بالای ۱ میلیارد، بدون شرط اردر سنگین)
BUY_QUEUE_SIMPLE_CONFIG = {
    "min_buy_queue_value": 1,  # میلیارد تومان
    "price_at_ceiling": True,  # آخرین قیمت = سقف
}

# فیلتر 11: خرید حقوقی و حقیقی قوی
HOGHOOGHI_HAGHIGHI_STRONG_BUY_CONFIG = {
    "max_pol_hagigi_to_value": -0.3,  
    "min_last_price_change_percent": 0, 
    "min_sarane_kharid": 100,  
    "sarane_kharid_greater_than_forosh": True, 
} 

# فیلتر 12: ماروبوزو صعودی با حجم
BULLISH_MARUBOZU_CONFIG = {
    "min_body_to_range_ratio": 0.9,   # نسبت بدنه به کل رنج روز - هرچی به 1 نزدیک‌تر یعنی سایه کمتر
    "min_intraday_move_percent": 1.0,  # حداقل اندازه‌ی بدنه‌ی کندل (نسبت به قیمت باز شدن امروز، نه دیروز)
    "min_value_to_avg_monthly": 1.0,  
}

# فیلتر 13: اختلاف سرانه بالا (سرانه خرید - سرانه فروش)
SARANE_DIFF_CONFIG = {
    "min_sarane_diff": 100,  # میلیون تومان
    "max_buy_queue_value": 2,  # میلیارد تومان - اگه صف خرید بالای این باشه، حذف می‌شه
}

# ========================================
# زمان‌بندی
# ========================================
MARKET_START_TIME = "09:00"
MARKET_END_TIME = "12:30"

# روزهای کاری (0=شنبه تا 6=جمعه)
WORKING_DAYS = [0, 1, 2, 3, 4]

# ========================================
# تنظیمات لاگ
# ========================================
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


# ========================================
# اعتبارسنجی تنظیمات
# ========================================
def validate_config():
    """بررسی صحت تنظیمات"""
    errors = []

    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN تنظیم نشده است")

    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID تنظیم نشده است")

    if not API_BASE_URL:
        errors.append("API_BASE_URL تنظیم نشده است")

    if not GIST_TOKEN:
        errors.append("GIST_TOKEN تنظیم نشده است")

    if not GIST_ID:
        errors.append("GIST_ID تنظیم نشده است")

    multiplier = STRONG_BUYING_CONFIG.get("godrat_5day_multiplier")
    if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool) or multiplier <= 0:
        errors.append(
            f"STRONG_BUYING_CONFIG['godrat_5day_multiplier'] باید عدد مثبت باشد "
            f"(مقدار فعلی: {multiplier!r})"
        )

    if errors:
        raise ValueError("خطاهای تنظیمات:\n" + "\n".join(errors))

    return True
