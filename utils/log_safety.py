# utils/log_safety.py
"""ماسک‌کردن توکن‌ها/سکرت‌ها در لاگ (لاگ موقع شکست به‌عنوان artifact آپلود می‌شه و GitHub محتوای artifact رو ماسک نمی‌کنه).

متن خطای requests شامل URL کامل است و URL تلگرام شامل /bot<TOKEN>/ است."""

import os
import re

_PATTERNS = [
    re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"),   # توکن بات تلگرام داخل URL
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),     # توکن‌های classic/oauth گیت‌هاب
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),   # fine-grained PAT
]
_SECRET_ENV_NAMES = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_SESSION", "TELETHON_API_HASH", "GIST_TOKEN")
_MASK = "***"


def redact(text):
    """هر مقدار سکرت (از env) و هر الگوی توکن‌مانند رو با *** عوض می‌کنه."""
    if not text:
        return text
    for name in _SECRET_ENV_NAMES:
        val = os.environ.get(name)
        if val and len(val) >= 8:
            text = text.replace(val, _MASK)
    for pat in _PATTERNS:
        text = pat.sub(_MASK, text)
    return text
