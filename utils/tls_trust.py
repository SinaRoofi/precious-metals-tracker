"""اعتماد محدود به ریشه‌ی «ISRG Root YE» (سلسله‌ی جدید Let's Encrypt) فقط برای درخواست‌های rahavard365.com.

چرا: rahavard365.com با گواهی معتبر Let's Encrypt کار می‌کنه، ولی زنجیره‌ای که می‌فرسته به ریشه‌ی جدید
ISRG Root YE ختم می‌شه (خودامضا، و هنوز تو بسته‌ی CA بسیاری از ابزارها نیست). نتیجه: certifi/curl روی runner و
سیستم‌ها خطای «self-signed certificate in certificate chain» می‌دن.

راه‌حل امن: تأیید گواهی خاموش نمی‌شه؛ فقط همین «یک ریشه»‌ی مشخص (با اثرانگشت SHA-256 ثابت) به بسته‌ی CA معمولی (certifi)
اضافه می‌شه و فقط برای آدرس‌های https://rahavard365.com. اگه فایل ریشه نبود یا اثرانگشتش فرق داشت، هیچ اعتماد
اضافه‌ای داده نمی‌شه و رفتار همون رفتار قبلیه. وقتی تلگرام/ابزارها ریشه رو بشناسن یا سایت زنجیره‌ی cross-signed بده،
این فایل بی‌اثر و قابل حذفه.
"""

import hashlib
import logging
import os
import ssl

import certifi
import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

RAHAVARD_PREFIX = "https://rahavard365.com"
EXTRA_CA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "certs", "isrg-root-ye.pem")
# SHA-256 گواهی ریشه‌ی «ISRG Root YE». قبل از استفاده با صفحه‌ی رسمی https://letsencrypt.org/certificates/ تطبیق بده.
EXTRA_CA_SHA256 = "e14ffcad5b0025731006caa43a121a22d8e9700f4fb9cf852f02a708aa5d5666"


def _extra_ca_ok(path, expected_sha256):
    """True اگه فایل هست و اثرانگشتش دقیقاً همونه (expected_sha256=None یعنی بدون چک؛ فقط برای تست)."""
    try:
        with open(path, "r", encoding="ascii") as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
    except (OSError, ValueError) as e:
        logger.warning(f"⚠️ فایل ریشه‌ی اضافه خونده نشد ({e}) — اعتماد اضافه داده نمی‌شه")
        return False
    if expected_sha256 and hashlib.sha256(der).hexdigest() != expected_sha256.lower():
        logger.error("❌ اثرانگشت فایل ریشه با مقدار انتظار نمی‌خونه — اعتماد اضافه داده نمی‌شه")
        return False
    return True


class _ExtraCAAdapter(HTTPAdapter):
    """certifi + یک ریشه‌ی اضافه‌ی مشخص (نه جایگزین certifi)."""

    def __init__(self, ca_path, *args, **kwargs):
        self._ca_path = ca_path
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.load_verify_locations(cafile=self._ca_path)
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


def mount_extra_trust(session, prefix=RAHAVARD_PREFIX, ca_path=EXTRA_CA_PATH, expected_sha256=EXTRA_CA_SHA256):
    """ریشه‌ی اضافه رو فقط برای آدرس‌هایی که با prefix شروع می‌شن فعال می‌کنه. همون session رو برمی‌گردونه."""
    if _extra_ca_ok(ca_path, expected_sha256):
        session.mount(prefix, _ExtraCAAdapter(ca_path))
    return session


def trusted_get(url, **kwargs):
    """requests.get با session موقت که برای rahavard365.com ریشه‌ی اضافه رو داره (برای بقیه‌ی آدرس‌ها عادی)."""
    with requests.Session() as session:
        mount_extra_trust(session)
        return session.get(url, **kwargs)
