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
import tempfile
import threading

import certifi
import requests
from requests.adapters import HTTPAdapter

try:  # امضای TLS/HTTP2 شبیه کروم؛ بدون اون رهاورد requests ساده رو «ربات» می‌دونه و بدنه رو ~۶۰ ثانیه نگه می‌داره
    from curl_cffi import requests as curl_requests
    try:
        from curl_cffi.requests.exceptions import RequestException as _CurlError
    except ImportError:  # نسخه‌های قدیمی‌تر curl_cffi
        from curl_cffi.requests.errors import RequestsError as _CurlError
except ImportError:  # pragma: no cover
    curl_requests = None
    _CurlError = None

logger = logging.getLogger(__name__)

RAHAVARD_PREFIX = "https://rahavard365.com"
EXTRA_CA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "certs", "isrg-root-ye.pem")
# SHA-256 گواهی ریشه‌ی «ISRG Root YE». قبل از استفاده با صفحه‌ی رسمی https://letsencrypt.org/certificates/ تطبیق بده.
EXTRA_CA_SHA256 = "e14ffcad5b0025731006caa43a121a22d8e9700f4fb9cf852f02a708aa5d5666"

# پروفایل مرورگر برای curl_cffi؛ هدرها باید با همین پروفایل (کروم ۱۲۰) هم‌خوان باشن، نه UA ناقص قبلی
IMPERSONATE = "chrome120"
RAHAVARD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Referer": "https://rahavard365.com/",
    "Origin": "https://rahavard365.com",
}

_bundle_lock = threading.Lock()
_bundle_path = None


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


def _combined_ca_bundle():
    """certifi + ریشه‌ی اضافه (با چک اثرانگشت) در یک فایل موقت؛ اگه ریشه معتبر نبود فقط certifi.
    تأیید گواهی هیچ‌وقت خاموش نمی‌شه (verify=False استفاده نمی‌کنیم)."""
    global _bundle_path
    with _bundle_lock:
        if _bundle_path and os.path.exists(_bundle_path):
            return _bundle_path
        if not _extra_ca_ok(EXTRA_CA_PATH, EXTRA_CA_SHA256):
            return certifi.where()
        with open(certifi.where(), "r", encoding="ascii", errors="ignore") as f:
            base = f.read()
        with open(EXTRA_CA_PATH, "r", encoding="ascii") as f:
            extra = f.read()
        fd, path = tempfile.mkstemp(prefix="ca-rahavard-", suffix=".pem")
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(base.rstrip("\n") + "\n" + extra.strip() + "\n")
        _bundle_path = path
        return path


def _rahavard_get(url, params=None, timeout=None, **_ignored):
    """GET با امضای کروم. خطاها رو به requests.exceptions تبدیل می‌کنه تا except های قبلیِ کد کار کنن."""
    try:
        with curl_requests.Session(impersonate=IMPERSONATE, verify=_combined_ca_bundle()) as s:
            resp = s.get(url, params=params, headers=RAHAVARD_HEADERS, timeout=timeout)
    except _CurlError as e:
        raise requests.exceptions.RequestException(f"curl_cffi: {e}") from e
    if resp.status_code >= 400:
        raise requests.exceptions.HTTPError(f"HTTP {resp.status_code} برای {url}")
    return resp


def trusted_get(url, **kwargs):
    """rahavard365.com → curl_cffi (امضای کروم + ریشه‌ی اضافه)؛ بقیه‌ی آدرس‌ها → requests عادی."""
    if url.startswith(RAHAVARD_PREFIX) and curl_requests is not None:
        return _rahavard_get(url, **kwargs)
    if url.startswith(RAHAVARD_PREFIX):
        logger.warning("⚠️ curl_cffi نصب نیست؛ رهاورد با requests ساده زده می‌شه (ممکنه ~۶۰ ثانیه معطل بشه)")
    with requests.Session() as session:
        mount_extra_trust(session)
        return session.get(url, **kwargs)
