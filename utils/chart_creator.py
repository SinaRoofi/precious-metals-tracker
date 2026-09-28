# utils/chart_creator.py

import logging
import pytz
import math
from datetime import datetime
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import io
from PIL import Image, ImageDraw, ImageFont
from utils.sheets_storage import read_from_sheets
from persiantools.jdatetime import JalaliDateTime
from config import (
    FONT_MEDIUM_PATH, FONT_REGULAR_PATH,
    CHART_WIDTH, CHART_HEIGHT, CHART_SCALE,
    COLOR_POSITIVE, COLOR_NEGATIVE, COLOR_BACKGROUND,
    COLOR_GRID, COLOR_GOLD, COLOR_SILVER, CHANNEL_HANDLE,
    TIMEZONE, Y_AXIS_STEP
)

logger = logging.getLogger(__name__)

COMMODITY_LABEL = {"gold": "طلا", "silver": "نقره"}
COMMODITY_COLOR = {"gold": COLOR_GOLD, "silver": COLOR_SILVER}

# میانگین ماهانه (برای خط نقطه‌چین مرجع) — ۲۰ روز گذشته‌ی بسته، هم‌رده با «۲۰»
# که در avg_monthly_bubble زنده‌ی صندوق‌ها هم استفاده می‌شه. هم برای پنل‌های حباب
# (۴ و ۶) و هم برای سرانه‌ی خرید/فروش (پنل ۸) استفاده می‌شه.
MONTHLY_MA_DAYS = 20
MONTHLY_MA_MIN_DAYS = 10

# برای محاسبهٔ میانگین ماهانه باید به اندازهٔ کافی روز گذشته داشته باشیم.
# اسکریپت هر ۱ دقیقه از ۱۲ تا ۱۸ اجرا می‌شه → حدود ۳۶۰ ردیف/روز.
# برای پوشش امن ۲۰–۳۰ روز کاری (با احتساب تعطیلات) حدود ۱۰–۱۲ هزار ردیف لازم است.
# KEEP_DAYS=40 است، پس حداکثر داده موجود ≈ ۱۴۰۰۰ ردیف.
BUBBLE_HISTORY_LOOKBACK_ROWS = 12000

# اجرای اول هر روز: بخش clientType اسنپ‌شات تریدرآرنا (که سرانهٔ خرید/فروش و
# پول حقیقی ازش می‌آد) دیرتر از trading.value آپدیت می‌شه، پس این ۴ ستون تو
# اولین ردیف(های) روز دقیقاً صفرن در حالی که بقیهٔ ستون‌ها (دلار/شمش/حباب) از
# همون لحظه مقدار واقعی دارن. این صفر کاذبِ لحظهٔ باز شدن بازاره، نه یک صفر
# واقعی وسط روز — پس فقط پیشوند ابتدای روز رو NaN می‌کنیم تا خط چارت (پنل ۷ و
# ۸) با یه افت جعلی به صفر شروع نشه؛ داده‌ی ذخیره‌شده در Sheets دست‌نخورده
# می‌مونه (میانگین ماهانه هم عمداً همون‌جوری از Sheets محاسبه می‌شه).
COLD_START_ZERO_COLUMNS = [
    'pol_hagigi', 'sarane_kharid_weighted',
    'sarane_forosh_weighted', 'ekhtelaf_sarane_weighted',
]


def round_to_nearest(value, step=Y_AXIS_STEP):
    """گرد کردن عدد به نزدیک‌ترین مضرب step"""
    return round(value / step) * step


def calculate_y_range_with_steps(data_min, data_max, step=Y_AXIS_STEP):
    """محاسبه محدوده محور Y با گام‌های مشخص"""
    if data_min == 0 and data_max == 0:
        return -step, step

    if data_min == data_max:
        return data_min - step, data_max + step

    y_min = math.floor(data_min / step) * step
    y_max = math.ceil(data_max / step) * step
    margin = step * 0.3
    y_min -= margin
    y_max += margin
    return y_min, y_max


def create_market_charts(commodity):
    """ساخت نمودارهای بازار با 8 subplot برای یک کالا (gold یا silver)"""
    if commodity not in COMMODITY_LABEL:
        raise ValueError(f"کالای نامعتبر: {commodity}")

    label = COMMODITY_LABEL[commodity]
    accent_color = COMMODITY_COLOR[commodity]

    try:
        data_rows = read_from_sheets(commodity, limit=BUBBLE_HISTORY_LOOKBACK_ROWS)
        if not data_rows:
            logger.warning(f"⚠️ [{commodity}] داده‌ای از Sheets دریافت نشد")
            return None

        df = pd.DataFrame(data_rows, columns=[
            'timestamp', 'global_price_usd', 'dollar_price', 'shams_price',
            'dollar_change_percent', 'shams_change_percent',
            'fund_weighted_change_percent', 'fund_final_price_avg',
            'fund_weighted_bubble_percent', 'sarane_kharid_weighted',
            'sarane_forosh_weighted', 'ekhtelaf_sarane_weighted',
            'pol_hagigi', 'shams_bubble_percent', 'trade_value',
            'global_change_percent',
        ])

        df['timestamp'] = pd.to_datetime(df['timestamp'])
        numeric_cols = df.columns[1:]
        df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors='coerce')

        tehran_tz = pytz.timezone(TIMEZONE)
        today = datetime.now(tehran_tz).date()

        # میانگین ماهانه (۲۰ روز گذشته‌ی بسته) حباب صندوق و شمش —
        # از ردیف‌های تاریخچه‌ای که همین الان خوندیم محاسبه می‌شه (بدون خوندن اضافه‌ی Sheet/Gist).
        # limit با BUBBLE_HISTORY_LOOKBACK_ROWS بزرگ‌تر شده تا حتی با اجرای پرتکرار روزانه
        # حداقل ۱۰–۲۰ روز گذشته در دسترس باشه.
        #
        # «مقدار روزانه» = آخرین ردیف اون روز (.last()) — نه میانگین کل روز —
        # هم‌رنگ با قراردادِ همه‌جای پروژه (weekly_report.py، monthly_report.py،
        # alerts.py). این کار یه فایده‌ی جانبی هم داره: چون فقط آخرین ردیفِ هر
        # روزِ گذشته رو در نظر می‌گیریم، صفرِ کاذبِ clientType در لحظه‌ی باز شدن
        # بازار (که فقط رو اولین ردیف‌های هر روز می‌افته) خودبه‌خود اثری رو
        # میانگین ماهانه نداره.
        df_history = df[df['timestamp'].dt.date < today].copy()
        df_history['date'] = df_history['timestamp'].dt.date
        daily_history = df_history.sort_values('timestamp').groupby('date', as_index=False).last()

        daily_fund_bubble = daily_history['fund_weighted_bubble_percent']
        daily_shams_bubble = daily_history['shams_bubble_percent']

        fund_bubble_monthly_avg = (
            daily_fund_bubble.tail(MONTHLY_MA_DAYS).mean()
            if len(daily_fund_bubble) >= MONTHLY_MA_MIN_DAYS else None
        )
        shams_bubble_monthly_avg = (
            daily_shams_bubble.tail(MONTHLY_MA_DAYS).mean()
            if len(daily_shams_bubble) >= MONTHLY_MA_MIN_DAYS else None
        )

        if fund_bubble_monthly_avg is None:
            logger.info(
                f"ℹ️ [{commodity}] خط میانگین ماهانهٔ حباب صندوق رسم نشد — فقط "
                f"{len(daily_fund_bubble)} روز تاریخچهٔ گذشته موجوده (حداقل لازم: {MONTHLY_MA_MIN_DAYS})"
            )
        if shams_bubble_monthly_avg is None:
            logger.info(
                f"ℹ️ [{commodity}] خط میانگین ماهانهٔ حباب شمش رسم نشد — فقط "
                f"{len(daily_shams_bubble)} روز تاریخچهٔ گذشته موجوده (حداقل لازم: {MONTHLY_MA_MIN_DAYS})"
            )

        # میانگین ماهانه‌ی سرانه‌ی خرید/فروش حقیقی — همون daily_history بالا
        # (آخرین ردیف هر روز)، هم‌رنگ با alerts.py که دقیقاً همین محاسبه رو
        # برای فیلتر/هشدار سرانه استفاده می‌کنه.
        daily_kharid = daily_history['sarane_kharid_weighted']
        daily_forosh = daily_history['sarane_forosh_weighted']

        kharid_monthly_avg = (
            daily_kharid.tail(MONTHLY_MA_DAYS).mean()
            if len(daily_kharid) >= MONTHLY_MA_MIN_DAYS else None
        )
        forosh_monthly_avg = (
            daily_forosh.tail(MONTHLY_MA_DAYS).mean()
            if len(daily_forosh) >= MONTHLY_MA_MIN_DAYS else None
        )

        if kharid_monthly_avg is None:
            logger.info(
                f"ℹ️ [{commodity}] خط میانگین ماهانهٔ سرانهٔ خرید رسم نشد — فقط "
                f"{len(daily_kharid)} روز تاریخچهٔ گذشته موجوده (حداقل لازم: {MONTHLY_MA_MIN_DAYS})"
            )
        if forosh_monthly_avg is None:
            logger.info(
                f"ℹ️ [{commodity}] خط میانگین ماهانهٔ سرانهٔ فروش رسم نشد — فقط "
                f"{len(daily_forosh)} روز تاریخچهٔ گذشته موجوده (حداقل لازم: {MONTHLY_MA_MIN_DAYS})"
            )

        df = df[df['timestamp'].dt.date == today].copy()

        if df.empty:
            logger.info(f"ℹ️ [{commodity}] داده‌ای برای امروز پیدا نشد")
            return None

        df = df.sort_values('timestamp').reset_index(drop=True)
        df = mask_cold_start_clienttype(df, commodity)
        jalali_now = JalaliDateTime.now(tehran_tz)
        date_time_str = jalali_now.strftime("%Y/%m/%d - %H:%M")

        fig = make_subplots(
            rows=8, cols=1,
            subplot_titles=(
                f'<b>(%) قیمت اونس {label}</b>',
                '<b>(%) دلار آزاد</b>',
                f'<b>(%) شمش {label} بورس کالا</b>',
                f'<b>(%) حباب شمش {label} بورس کالا</b>',
                f'<b>(%) آخرین قیمت و قیمت پایانی صندوق‌های {label}</b>',
                f'<b>(%) حباب صندوق‌های {label}</b>',
                '<b>ورود پول حقیقی</b>',
                '<b>سرانه خرید و فروش و اختلاف آن</b>'
            ),
            vertical_spacing=0.035,
            shared_xaxes=True
        )

        try:
            ImageFont.truetype(FONT_MEDIUM_PATH, 40)
            chart_font_family = "Vazirmatn-Medium, Vazirmatn, sans-serif"
        except Exception:
            chart_font_family = "Vazirmatn, Arial, sans-serif"

        for annotation in fig['layout']['annotations']:
            annotation.font = dict(size=32, color='#8B949E', family=chart_font_family)

        last_global_change = df['global_change_percent'].iloc[-1]
        last_dollar = df['dollar_change_percent'].iloc[-1]
        last_shams = df['shams_change_percent'].iloc[-1]
        last_fund = df['fund_weighted_change_percent'].iloc[-1]
        last_final = df['fund_final_price_avg'].iloc[-1]
        last_fund_bubble = df['fund_weighted_bubble_percent'].iloc[-1]
        last_shams_bubble = df['shams_bubble_percent'].iloc[-1]
        last_pol = df['pol_hagigi'].iloc[-1]
        last_kharid = df['sarane_kharid_weighted'].iloc[-1]
        last_forosh = df['sarane_forosh_weighted'].iloc[-1]
        last_ekhtelaf = df['ekhtelaf_sarane_weighted'].iloc[-1]

        # ═══════════════════════════════════════════════════════
        # نمودار 1: درصد تغییر انس (مثل دلار — نسبت به دیروز)
        # ═══════════════════════════════════════════════════════
        add_conditional_line(fig, df, 'global_change_percent', 1, positive_color=accent_color)
        set_y_range(fig, df, 'global_change_percent', 1)

        # ═══════════════════════════════════════════════════════
        # نمودار 2-3: دلار و شمش
        # ═══════════════════════════════════════════════════════
        add_conditional_line(fig, df, 'dollar_change_percent', 2)
        set_y_range(fig, df, 'dollar_change_percent', 2)

        add_conditional_line(fig, df, 'shams_change_percent', 3)
        set_y_range(fig, df, 'shams_change_percent', 3)

        # ═══════════════════════════════════════════════════════
        # نمودار 4: حباب شمش بورس کالا (پنل جدید، مستقل، کنار پنل قیمت شمش)
        # ═══════════════════════════════════════════════════════
        add_conditional_line(fig, df, 'shams_bubble_percent', 4)

        if shams_bubble_monthly_avg is not None:
            range_series_4 = pd.concat([df['shams_bubble_percent'], pd.Series([shams_bubble_monthly_avg])])
            set_y_range_for_series(fig, range_series_4, 4)
            fig.add_hline(
                y=shams_bubble_monthly_avg, row=4, col=1,
                line=dict(color='#8B949E', width=2, dash='dash'),
                annotation_text=f'میانگین ماهانه: %{shams_bubble_monthly_avg:+.2f}',
                annotation_position='top right',
                annotation_font=dict(size=18, color='#8B949E', family=chart_font_family),
            )
        else:
            set_y_range(fig, df, 'shams_bubble_percent', 4)

        # ═══════════════════════════════════════════════════════
        # نمودار 5: آخرین قیمت + قیمت پایانی
        # ═══════════════════════════════════════════════════════
        add_conditional_line(fig, df, 'fund_weighted_change_percent', 5)

        fig.add_trace(go.Scatter(
            x=df['timestamp'], y=df['fund_final_price_avg'],
            name='قیمت پایانی',
            line=dict(color='#2196F3', width=4),
            hovertemplate='پایانی: <b>%{y:+.2f}%</b><extra></extra>'
        ), row=5, col=1)

        all_values = pd.concat([
            df['fund_weighted_change_percent'],
            df['fund_final_price_avg']
        ])
        set_y_range_for_series(fig, all_values, 5)
        logger.info(f"✅ [{commodity}] نمودار 5: آخرین={last_fund:+.2f}%, پایانی={last_final:+.2f}%")

        # ═══════════════════════════════════════════════════════
        # نمودار 6: حباب صندوق‌ها (تک‌خط، چون حباب شمش حالا پنل ۴ جدای خودشو داره)
        # قبلاً این پنل هم صندوق هم شمش رو با هم داشت (رنگ هویتی + محور مستقل)؛ چون
        # شمش منتقل شد، دیگه نیازی به اون پیچیدگی نیست — مثل بقیهٔ پنل‌های تک‌خط،
        # رنگ شرطی سبز/قرمز استفاده می‌شه.
        # ═══════════════════════════════════════════════════════
        add_conditional_line(fig, df, 'fund_weighted_bubble_percent', 6)

        if fund_bubble_monthly_avg is not None:
            range_series_6 = pd.concat([df['fund_weighted_bubble_percent'], pd.Series([fund_bubble_monthly_avg])])
            set_y_range_for_series(fig, range_series_6, 6)
            fig.add_hline(
                y=fund_bubble_monthly_avg, row=6, col=1,
                line=dict(color='#8B949E', width=2, dash='dash'),
                annotation_text=f'میانگین ماهانه: %{fund_bubble_monthly_avg:+.2f}',
                annotation_position='top right',
                annotation_font=dict(size=18, color='#8B949E', family=chart_font_family),
            )
        else:
            set_y_range(fig, df, 'fund_weighted_bubble_percent', 6)

        # ═══════════════════════════════════════════════════════
        # نمودار 7: پول حقیقی
        # ═══════════════════════════════════════════════════════
        add_conditional_line(fig, df, 'pol_hagigi', 7)
        set_y_range(fig, df, 'pol_hagigi', 7)

        # ═══════════════════════════════════════════════════════
        # نمودار 8: سرانه با دو محور Y جداگانه
        # ═══════════════════════════════════════════════════════
        fig.add_trace(go.Scatter(
            x=df['timestamp'], y=df['sarane_kharid_weighted'],
            name='خرید حقیقی',
            line=dict(color=COLOR_POSITIVE, width=5),
            hovertemplate='خرید: <b>%{y:.2f}</b><extra></extra>',
            yaxis='y8'
        ), row=8, col=1)

        fig.add_trace(go.Scatter(
            x=df['timestamp'], y=df['sarane_forosh_weighted'],
            name='فروش حقیقی',
            line=dict(color=COLOR_NEGATIVE, width=5),
            hovertemplate='فروش: <b>%{y:.2f}</b><extra></extra>',
            yaxis='y8'
        ), row=8, col=1)

        colors_fill = [
            'rgba(0,230,118,0.75)' if x > 0 else 'rgba(255,23,68,0.75)' if x < 0 else 'rgba(72,79,88,0.75)'
            for x in df['ekhtelaf_sarane_weighted']
        ]

        fig.add_trace(go.Bar(
            x=df['timestamp'], y=df['ekhtelaf_sarane_weighted'],
            name='اختلاف سرانه',
            width=1,
            marker=dict(color=colors_fill, line=dict(color=colors_fill, width=4)),
            hovertemplate='اختلاف: <b>%{y:.2f}</b><extra></extra>',
            yaxis='y14'
        ), row=8, col=1)

        kharid_min = df['sarane_kharid_weighted'].min()
        kharid_max = df['sarane_kharid_weighted'].max()
        forosh_min = df['sarane_forosh_weighted'].min()
        forosh_max = df['sarane_forosh_weighted'].max()

        lines_min = min(kharid_min, forosh_min)
        lines_max = max(kharid_max, forosh_max)

        # میانگین‌های ماهانه هم باید داخل رنج بیفتن، وگرنه خط نقطه‌چین ممکنه
        # بیرون از محدودهٔ دیده‌شدهٔ محور Y بیفته (مثل الگوی پنل‌های حباب).
        if kharid_monthly_avg is not None:
            lines_min = min(lines_min, kharid_monthly_avg)
            lines_max = max(lines_max, kharid_monthly_avg)
        if forosh_monthly_avg is not None:
            lines_min = min(lines_min, forosh_monthly_avg)
            lines_max = max(lines_max, forosh_monthly_avg)

        lines_padding = max(10, (lines_max - lines_min) * 0.15)

        fig.update_yaxes(
            range=[lines_min - lines_padding, lines_max + lines_padding],
            row=8, col=1
        )

        # برخلاف پنل‌های حباب (۴ و ۶)، پنل ۸ دو خط پُررنگ و پُرنوسان داره که
        # عملاً کل ارتفاع پنل رو اشغال می‌کنن (width=5، از نزدیک min تا نزدیک
        # max). یعنی هر annotation ای که به‌روش معمولِ add_hline وصل بشه (روی
        # ارتفاعِ خودِ خط میانگین) بالاخره یه جای روز، زیر همون خط زندهٔ
        # پُرنوسان قایم می‌شه (دقیقاً همون چیزی که گزارش دادید). برای پنل ۸،
        # به‌جای annotation وابسته به مقدار داده، متن رو به یه گوشهٔ ثابت از
        # خودِ پنل (نسبت به domain پنل، نه مقدار y) می‌چسبونیم و پس‌زمینه‌ی
        # تیره می‌دیم — این‌جوری مهم نیست خط زنده از کجا رد بشه، متن همیشه
        # رو یه زمینهٔ توپر و خونا می‌مونه.
        annotation_bg = 'rgba(13,17,23,0.85)'  # هم‌رنگ COLOR_BACKGROUND با کمی شفافیت

        # دو annotation جدا (سبز=خرید، قرمز=فروش)، هر دو گوشهٔ بالا-چپ ولی
        # رو-هم (نه دو سر مخالف پنل) — چون عرض واقعی متنِ رندرشده رو اینجا
        # نمی‌تونیم اندازه بگیریم، «کنار هم» رو با چیدمانِ عمودی (یکی زیر
        # اون یکی) پیاده کردیم تا مطمئن باشیم روی هم نمی‌افتن؛ اگه پهلوی هم
        # رو یه خط افقی مدنظرتونه، بگید تا با تخمین عرض دوباره جابه‌جا کنم.
        if kharid_monthly_avg is not None:
            fig.add_hline(
                y=kharid_monthly_avg, row=8, col=1,
                line=dict(color=COLOR_POSITIVE, width=2, dash='dash'),
            )
            fig.add_annotation(
                text=f'میانگین ماهانه سرانه خرید: {int(kharid_monthly_avg):,}'.replace(',', '٬'),
                xref='x8 domain', yref='y8 domain',
                x=0.01, y=1.09, xanchor='left', yanchor='bottom', showarrow=False,
                font=dict(size=20, color=COLOR_POSITIVE, family=chart_font_family),
                bgcolor=annotation_bg,
            )
        if forosh_monthly_avg is not None:
            fig.add_hline(
                y=forosh_monthly_avg, row=8, col=1,
                line=dict(color=COLOR_NEGATIVE, width=2, dash='dash'),
            )
            fig.add_annotation(
                text=f'میانگین ماهانه سرانه فروش: {int(forosh_monthly_avg):,}'.replace(',', '٬'),
                xref='x8 domain', yref='y8 domain',
                x=0.01, y=1.0, xanchor='left', yanchor='bottom', showarrow=False,
                font=dict(size=20, color=COLOR_NEGATIVE, family=chart_font_family),
                bgcolor=annotation_bg,
            )

        ekhtelaf_min = df['ekhtelaf_sarane_weighted'].min()
        ekhtelaf_max = df['ekhtelaf_sarane_weighted'].max()
        ekhtelaf_padding = max(10, (ekhtelaf_max - ekhtelaf_min) * 0.15)

        fig.update_layout(
            yaxis14=dict(
                overlaying='y8', side='right',
                range=[ekhtelaf_min - ekhtelaf_padding, ekhtelaf_max + ekhtelaf_padding],
                showgrid=False, showticklabels=False, zeroline=False
            )
        )

        # ═══════════════════════════════════════════════════════
        # تنظیمات کلی Layout
        # ═══════════════════════════════════════════════════════
        # یه ردیف اضافه شد (۷->۸)؛ ارتفاع کلی به‌نسبت زیاد می‌شه تا هر پنل جمع‌وجور نمونه.
        # عمداً محلی نگه داشته شده، نه دستکاری CHART_HEIGHT تو config.py (ممکنه جای دیگه هم استفاده بشه).
        extra_row_height = CHART_HEIGHT // 7
        total_chart_height = CHART_HEIGHT + 300 + extra_row_height

        fig.update_layout(
            height=total_chart_height,
            paper_bgcolor=COLOR_BACKGROUND,
            plot_bgcolor=COLOR_BACKGROUND,
            font=dict(color='#C9D1D9', family=chart_font_family, size=25),
            hovermode='x unified',
            showlegend=False,
            margin=dict(l=60, r=120, t=120, b=60),
        )

        fig.add_annotation(
            text=f'<b>📊 روند بازار {label}</b>',
            x=0.98, y=1.04, xref='paper', yref='paper',
            xanchor='right', yanchor='top',
            font=dict(size=40, color=accent_color, family=chart_font_family),
            showarrow=False
        )

        fig.add_annotation(
            text=f'<b>{date_time_str}</b>',
            x=0.02, y=1.04, xref='paper', yref='paper',
            xanchor='left', yanchor='top',
            font=dict(size=40, color='#FFFFFF', family=chart_font_family),
            showarrow=False
        )

        # ═══════════════════════════════════════════════════════
        # برچسب‌های آخرین مقدار
        # ═══════════════════════════════════════════════════════
        global_color = accent_color if last_global_change >= 0 else COLOR_NEGATIVE
        fig.add_annotation(
            text=f'<b>{last_global_change:+.2f}%</b>',
            x=1.01, y=last_global_change, xref='paper', yref='y1',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=global_color, family=chart_font_family),
            showarrow=False
        )

        dollar_color = COLOR_POSITIVE if last_dollar >= 0 else COLOR_NEGATIVE
        fig.add_annotation(
            text=f'<b>{last_dollar:+.2f}%</b>',
            x=1.01, y=last_dollar, xref='paper', yref='y2',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=dollar_color, family=chart_font_family),
            showarrow=False
        )

        shams_color = COLOR_POSITIVE if last_shams >= 0 else COLOR_NEGATIVE
        fig.add_annotation(
            text=f'<b>{last_shams:+.2f}%</b>',
            x=1.01, y=last_shams, xref='paper', yref='y3',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=shams_color, family=chart_font_family),
            showarrow=False
        )

        fund_color = COLOR_POSITIVE if last_fund >= 0 else COLOR_NEGATIVE
        fig.add_annotation(
            text=f'<b>{last_fund:+.2f}%</b>',
            x=1.01, y=last_fund, xref='paper', yref='y5',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=fund_color, family=chart_font_family),
            showarrow=False
        )

        final_color = '#2196F3'
        min_gap = 0.04
        if abs(last_final - last_fund) < min_gap:
            yshift = -50 if last_final > last_fund else 50
        else:
            yshift = 0

        fig.add_annotation(
            text=f'<b>{last_final:+.2f}%</b>',
            x=1.01, y=last_final, xref='paper', yref='y5',
            xanchor='left', yanchor='middle',
            yshift=yshift,
            font=dict(size=28, color=final_color, family=chart_font_family),
            showarrow=False
        )

        shams_bubble_color = COLOR_POSITIVE if last_shams_bubble >= 0 else COLOR_NEGATIVE
        fig.add_annotation(
            text=f'<b>{last_shams_bubble:+.2f}%</b>',
            x=1.01, y=last_shams_bubble, xref='paper', yref='y4',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=shams_bubble_color, family=chart_font_family),
            showarrow=False
        )

        fund_bubble_color = COLOR_POSITIVE if last_fund_bubble >= 0 else COLOR_NEGATIVE
        fig.add_annotation(
            text=f'<b>{last_fund_bubble:+.2f}%</b>',
            x=1.01, y=last_fund_bubble, xref='paper', yref='y6',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=fund_bubble_color, family=chart_font_family),
            showarrow=False
        )

        pol_color = COLOR_POSITIVE if last_pol >= 0 else COLOR_NEGATIVE
        pol_formatted = f"{int(last_pol):+,}"
        fig.add_annotation(
            text=f'<b>{pol_formatted}</b>',
            x=1.01, y=last_pol, xref='paper', yref='y7',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=pol_color, family=chart_font_family),
            showarrow=False
        )

        ekhtelaf_color = COLOR_POSITIVE if last_ekhtelaf >= 0 else COLOR_NEGATIVE
        lines_range = lines_max - lines_min

        kharid_y = lines_max - (lines_range * 0.05)
        forosh_y = lines_min + (lines_range * 0.05)
        ekhtelaf_y = (lines_max + lines_min) / 2

        fig.add_annotation(
            text=f'<b>خ: {int(last_kharid):,}</b>'.replace(',', '٬'),
            x=1.01, y=kharid_y, xref='paper', yref='y8',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=COLOR_POSITIVE, family=chart_font_family),
            showarrow=False
        )

        fig.add_annotation(
            text=f'<b>اخ: {int(last_ekhtelaf):+,}</b>'.replace(',', '٬'),
            x=1.01, y=ekhtelaf_y, xref='paper', yref='y8',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=ekhtelaf_color, family=chart_font_family),
            showarrow=False
        )

        fig.add_annotation(
            text=f'<b>ف: {int(last_forosh):,}</b>'.replace(',', '٬'),
            x=1.01, y=forosh_y, xref='paper', yref='y8',
            xanchor='left', yanchor='middle',
            font=dict(size=28, color=COLOR_NEGATIVE, family=chart_font_family),
            showarrow=False
        )

        # ═══════════════════════════════════════════════════════
        # تنظیمات محورها
        # ═══════════════════════════════════════════════════════
        df['timestamp'] = pd.to_datetime(df['timestamp'])

        start_ts = df['timestamp'].iloc[0]
        end_ts = df['timestamp'].iloc[-1]

        tick_vals = pd.date_range(
            start=start_ts.floor('30min'),
            end=end_ts.ceil('30min'),
            freq='30min'
        ).tolist()

        tick_vals[0] = start_ts
        tick_vals[-1] = end_ts

        logger.info(f"📊 [{commodity}] labels: {len(tick_vals)} | interval: 30 min")

        for i in range(1, 9):
            fig.update_xaxes(
                type='date',
                tickmode='array',
                tickvals=tick_vals,
                tickformat='%H:%M',
                tickangle=-45,
                tickfont=dict(size=25),
                gridcolor=COLOR_GRID,
                showgrid=True,
                zeroline=False,
                showline=True,
                linewidth=1,
                linecolor='#30363D',
                row=i, col=1
            )

            fig.update_yaxes(
                tickfont=dict(size=25),
                gridcolor=COLOR_GRID,
                showgrid=True,
                zeroline=True,
                zerolinecolor='#30363D',
                zerolinewidth=2,
                showline=True,
                linewidth=1,
                linecolor='#30363D',
                row=i, col=1
            )

            if i > 1:
                fig.add_hline(
                    y=0, line_dash='dot', line_color='#484F58', line_width=2,
                    row=i, col=1
                )

        # ═══════════════════════════════════════════════════════
        # تبدیل به تصویر و واترمارک
        # ═══════════════════════════════════════════════════════
        img_bytes = fig.to_image(
            format='png', width=CHART_WIDTH, height=total_chart_height, scale=CHART_SCALE
        )
        img = Image.open(io.BytesIO(img_bytes)).convert('RGBA')

        try:
            draw = ImageDraw.Draw(img)
            font = ImageFont.truetype(FONT_REGULAR_PATH, 46)
            text = CHANNEL_HANDLE.replace("@", "")
            bbox = draw.textbbox((0, 0), text, font=font)
            w = bbox[2] - bbox[0]
            x = img.width - w - 25
            y = int(img.height * 0.85)
            draw.text((x, y), text, fill=(201, 209, 217, 160), font=font)
        except Exception as e:
            logger.warning(f"⚠️ خطا در واترمارک: {e}")

        output = io.BytesIO()
        img.save(output, format='PNG', optimize=True, quality=92)
        output.seek(0)
        logger.info(f"✅ [{commodity}] نمودارهای بازار ساخته شدند")
        return output.getvalue()

    except Exception as e:
        logger.error(f'❌ [{commodity}] خطا در ساخت نمودار: {e}', exc_info=True)
        return None


def mask_cold_start_clienttype(df, commodity):
    """
    فقط پیشوندِ ابتدای روز رو که پول حقیقی و هر سه سرانه هم‌زمان صفرن (تاخیر
    آپدیت clientType در اسنپ‌شات تریدرآرنا، نه صفر واقعی) با NaN جایگزین
    می‌کنه — تا خط چارت پنل ۷/۸ به‌جای شروع با یه افت جعلی به صفر، درست از
    اولین مقدار واقعی شروع بشه. df باید از قبل بر اساس timestamp مرتب و
    reset_index شده باشه (ترتیبِ ردیف‌ها مبنای تشخیص «پیشوند» است، نه ایندکس).
    """
    df = df.copy()
    is_cold = (df[COLD_START_ZERO_COLUMNS] == 0).all(axis=1)
    leading_cold = is_cold.cumprod().astype(bool)

    n_masked = int(leading_cold.sum())
    if n_masked == len(df):
        # کل روز صفره (clientType کل روز آپدیت نشده) — این دیگه صفر کاذبِ
        # لحظهٔ باز شدن نیست، یه خرابی جدی‌تره. اگه همه رو NaN کنیم، min/max
        # و annotation های پایین‌دستی رو می‌شکنه؛ پس دست‌نخورده برمی‌گردونیم و
        # فقط لاگ می‌کنیم تا جداگانه بررسی بشه.
        logger.warning(
            f"⚠️ [{commodity}] تمام {n_masked} ردیف امروز صفرِ clientType دارن "
            f"— به‌جای صفر کاذبِ اول روز، این می‌تونه یه خرابی واقعی داده باشه؛ ماسک نشد"
        )
        return df
    if n_masked:
        df.loc[leading_cold, COLD_START_ZERO_COLUMNS] = float('nan')
        logger.info(
            f"ℹ️ [{commodity}] {n_masked} ردیف ابتدای روز (صفر کاذب clientType "
            f"در اولین اجرا) از پنل پول حقیقی/سرانه حذف شد"
        )
    return df


def set_y_range(fig, df, column, row, padding_percent=0.3):
    """تنظیم محدوده محور Y"""
    col_min = df[column].min()
    col_max = df[column].max()
    padding = 0.1 if col_min == col_max else (col_max - col_min) * padding_percent
    fig.update_yaxes(range=[col_min - padding, col_max + padding], row=row, col=1)


def set_y_range_for_series(fig, series, row, padding_percent=0.3):
    """تنظیم محدوده محور Y برای یک Series"""
    col_min = series.min()
    col_max = series.max()
    padding = 0.1 if col_min == col_max else (col_max - col_min) * padding_percent
    fig.update_yaxes(range=[col_min - padding, col_max + padding], row=row, col=1)


def add_conditional_line(fig, df, column, row, positive_color=COLOR_POSITIVE):
    """رسم خط با تغییر رنگ در محل عبور از صفر (بالای صفر: positive_color، زیر صفر: قرمز)"""
    for i in range(len(df) - 1):
        curr_val = df[column].iloc[i]
        next_val = df[column].iloc[i + 1]
        curr_time = df['timestamp'].iloc[i]
        next_time = df['timestamp'].iloc[i + 1]

        # NaN (مثلاً صفر کاذب clienType ماسک‌شده در اول روز) نباید به‌عنوان
        # منفی رنگ بشه یا خط جعلی به صفر بکشه — این بازه رو کامل رد می‌کنیم
        # تا مثل Scatter معمولی، یه گپ (شکاف) تو خط بیفته.
        if pd.isna(curr_val) or pd.isna(next_val):
            continue

        color = positive_color if curr_val >= 0 else COLOR_NEGATIVE

        if (curr_val >= 0 and next_val < 0) or (curr_val < 0 and next_val >= 0):
            t = abs(curr_val) / (abs(curr_val) + abs(next_val))
            cross_time = curr_time + (next_time - curr_time) * t

            fig.add_trace(go.Scatter(
                x=[curr_time, cross_time], y=[curr_val, 0],
                mode='lines',
                line=dict(color=color, width=5, shape='spline'),
                showlegend=False, hoverinfo='skip'
            ), row=row, col=1)

            color_next = COLOR_NEGATIVE if next_val < 0 else positive_color
            fig.add_trace(go.Scatter(
                x=[cross_time, next_time], y=[0, next_val],
                mode='lines',
                line=dict(color=color_next, width=5, shape='spline'),
                showlegend=False, hoverinfo='skip'
            ), row=row, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=[curr_time, next_time], y=[curr_val, next_val],
                mode='lines',
                line=dict(color=color, width=5, shape='spline'),
                showlegend=False,
                hovertemplate='<b>%{y:+.2f}%</b><extra></extra>' if i == 0 else None
            ), row=row, col=1)
