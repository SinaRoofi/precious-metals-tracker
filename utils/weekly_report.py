# utils/weekly_report.py

import io
import logging
from datetime import datetime, timedelta

import pandas as pd
import pytz
from PIL import Image, ImageDraw, ImageFont
from persiantools.jdatetime import JalaliDateTime
from plotly.subplots import make_subplots

from config import (
    CHANNEL_HANDLE,
    CHART_SCALE,
    CHART_WIDTH,
    COLOR_BACKGROUND,
    COLOR_GOLD,
    COLOR_GRID,
    COLOR_NEGATIVE,
    COLOR_POSITIVE,
    COLOR_SILVER,
    FONT_MEDIUM_PATH,
    FONT_REGULAR_PATH,
    STANDARD_HEADER,
    TIMEZONE,
    WEEKLY_CHART_HEIGHT,
)
from utils.chart_creator import add_conditional_line, set_y_range, set_y_range_for_series
from utils.sheets_storage import read_from_sheets

logger = logging.getLogger(__name__)

COMMODITY_LABEL = {"gold": "طلا", "silver": "نقره"}
COMMODITY_COLOR = {"gold": COLOR_GOLD, "silver": COLOR_SILVER}

# datetime.date.weekday(): شنبه=5, یکشنبه=6, دوشنبه=0, سه‌شنبه=1, چهارشنبه=2
PERSIAN_WEEKDAY_NAME = {5: "شنبه", 6: "یک‌شنبه", 0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه"}

# ─── ایندکس ردیف‌های subplot (به‌جای عدد ثابت، برای خوانایی و جلوگیری از خطا هنگام تغییر ترتیب) ───
ROW_TRADE_VALUE = 1
ROW_SHAMS_BUBBLE = 2
ROW_FUND_BUBBLE = 3
ROW_POL_HAGIGI = 4
ROW_SARANE = 5
WEEKLY_ROW_COUNT = 5

MA_SHORT_WINDOW = 5
MA_LONG_WINDOW = 22
# حداقل تعداد روز تاریخچه که برای میانگین ۲۲ روزه واکشی می‌کنیم (روزهای هفته جاری + حاشیه تعطیلات)
TRADE_VALUE_HISTORY_LOOKBACK_ROWS = 3000


def _current_trading_week_range(now=None):
    """بازه‌ی شنبه تا امروز (حداکثر چهارشنبه) هفته‌ی جاری را برمی‌گرداند."""
    tz = pytz.timezone(TIMEZONE)
    today = (now or datetime.now(tz)).date()
    days_since_saturday = (today.weekday() - 5) % 7
    week_start = today - timedelta(days=days_since_saturday)
    return week_start, today


def _load_daily_history(commodity):
    """
    کل تاریخچه‌ی موجود در Sheets را می‌خواند و به یک ردیف در روز (آخرین snapshot) تقلیل می‌دهد،
    سپس میانگین‌های متحرک ۵ و ۲۲ روزه‌ی ارزش معاملات را روی کل تاریخچه محاسبه می‌کند —
    این کار لازم است چون در ابتدای هفته باید میانگین با داده‌ی روزهای هفته‌ی قبل هم حساب شود.
    """
    rows = read_from_sheets(commodity, limit=TRADE_VALUE_HISTORY_LOOKBACK_ROWS)
    if not rows:
        logger.warning(f"⚠️ [{commodity}] داده‌ای از Sheets دریافت نشد")
        return None

    df = pd.DataFrame(rows, columns=STANDARD_HEADER)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    numeric_cols = df.columns[1:]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")

    df["date"] = df["timestamp"].dt.date
    # .last() روی هر گروهِ‌روز یعنی «آخرین ردیف اون روز» برای همه‌ی ستون‌ها اعمال می‌شه —
    # یعنی pol_hagigi، sarane_kharid_weighted، sarane_forosh_weighted و بقیه، همگی
    # «آخرین مقدار همون روز» می‌شن، نه جمع/میانگین چند اجرای داخل‌روزی.
    daily = df.sort_values("timestamp").groupby("date", as_index=False).last()
    daily = daily.sort_values("date").reset_index(drop=True)

    # میانگین متحرک روی کمترین تعداد روز موجود هم محاسبه می‌شود (min_periods=1)؛
    # یعنی در ابتدای تاریخچه (کمتر از ۵ یا ۲۲ روز داده) میانگین روی همان چند روز موجود است،
    # نه NaN — تا خط نمودار از همون هفته‌ی اول رسم بشه.
    daily["trade_value_ma5"] = daily["trade_value"].rolling(window=MA_SHORT_WINDOW, min_periods=1).mean()
    daily["trade_value_ma22"] = daily["trade_value"].rolling(window=MA_LONG_WINDOW, min_periods=1).mean()

    return daily


def _load_week_dataframe(commodity):
    """تاریخچه‌ی کامل را می‌خواند (برای میانگین‌های متحرک) و فقط هفته‌ی جاری را برای نمایش برمی‌گرداند."""
    daily = _load_daily_history(commodity)
    if daily is None:
        return None

    week_start, week_end = _current_trading_week_range()
    daily = daily[(daily["date"] >= week_start) & (daily["date"] <= week_end)].copy()
    if daily.empty:
        logger.info(f"ℹ️ [{commodity}] داده‌ای برای هفته‌ی جاری ({week_start}..{week_end}) پیدا نشد")
        return None

    daily["pol_hagigi_cumulative"] = daily["pol_hagigi"].cumsum()
    daily["day_label"] = daily["date"].apply(lambda d: PERSIAN_WEEKDAY_NAME.get(d.weekday(), str(d)))

    return daily.reset_index(drop=True)


def _render_weekly_chart(commodity, daily):
    """ساخت نمودار هفتگی (5 subplot) از یک DataFrame روزانه‌ی از‌پیش‌بارگذاری‌شده؛ خروجی PNG bytes یا None."""
    label = COMMODITY_LABEL[commodity]
    accent_color = COMMODITY_COLOR[commodity]

    try:
        tehran_tz = pytz.timezone(TIMEZONE)
        jalali_now = JalaliDateTime.now(tehran_tz)
        date_time_str = jalali_now.strftime("%Y/%m/%d - %H:%M")

        try:
            ImageFont.truetype(FONT_MEDIUM_PATH, 40)
            chart_font_family = "Vazirmatn-Medium, Vazirmatn, sans-serif"
        except Exception:
            chart_font_family = "Vazirmatn, Arial, sans-serif"

        fig = make_subplots(
            rows=WEEKLY_ROW_COUNT, cols=1,
            subplot_titles=(
                "<b>ارزش معاملات و میانگین ۵ و ۲۲ روزه</b>",
                f"<b>حباب شمش {label} و میانگینش (%)</b>",
                "<b>حباب صندوق‌ها و میانگینش (%)</b>",
                "<b>ورود پول حقیقی تجمعی هفته</b>",
                "<b>سرانه خرید و فروش و اختلاف آن</b>",
            ),
            vertical_spacing=0.08,
            shared_xaxes=True,
        )

        for annotation in fig["layout"]["annotations"]:
            annotation.font = dict(size=32, color="#8B949E", family=chart_font_family)

        # ─── ردیف ۱: ارزش معاملات + میانگین ۵ و ۲۲ روزه ───
        fig.add_trace(dict(
            type="bar", x=daily["timestamp"], y=daily["trade_value"],
            name="ارزش معاملات", marker=dict(color="rgba(33,150,243,0.55)"),
            hovertemplate="ارزش معاملات: <b>%{y:,.0f}</b><extra></extra>",
        ), row=ROW_TRADE_VALUE, col=1)

        fig.add_trace(dict(
            type="scatter", x=daily["timestamp"], y=daily["trade_value_ma5"],
            name=f"میانگین {MA_SHORT_WINDOW} روزه", mode="lines",
            line=dict(color=COLOR_POSITIVE, width=4, dash="dot", shape="spline"),
            hovertemplate="MA5: <b>%{y:,.0f}</b><extra></extra>",
        ), row=ROW_TRADE_VALUE, col=1)

        fig.add_trace(dict(
            type="scatter", x=daily["timestamp"], y=daily["trade_value_ma22"],
            name=f"میانگین {MA_LONG_WINDOW} روزه", mode="lines",
            line=dict(color="#FFA726", width=4, dash="dash", shape="spline"),
            hovertemplate="MA22: <b>%{y:,.0f}</b><extra></extra>",
        ), row=ROW_TRADE_VALUE, col=1)

        trade_value_series = pd.concat([
            daily["trade_value"], daily["trade_value_ma5"], daily["trade_value_ma22"],
        ])
        set_y_range_for_series(fig, trade_value_series, ROW_TRADE_VALUE)

        # ─── ردیف ۲: حباب شمش + میانگینش ───
        shams_avg = daily["shams_bubble_percent"].mean()
        fund_avg = daily["fund_weighted_bubble_percent"].mean()
        x_range = [daily["timestamp"].min(), daily["timestamp"].max()]

        fig.add_trace(dict(
            type="scatter", x=daily["timestamp"], y=daily["shams_bubble_percent"],
            name=f"حباب شمش {label}", mode="lines+markers",
            line=dict(color=accent_color, width=5, shape="spline"),
            marker=dict(size=10),
            hovertemplate=f"شمش {label}: <b>%{{y:+.2f}}%</b><extra></extra>",
        ), row=ROW_SHAMS_BUBBLE, col=1)

        fig.add_trace(dict(
            type="scatter", x=x_range, y=[shams_avg, shams_avg],
            name=f"میانگین حباب شمش {label}", mode="lines",
            line=dict(color=accent_color, width=3, dash="dot"),
            hovertemplate=f"میانگین شمش {label}: <b>{shams_avg:+.2f}%</b><extra></extra>",
        ), row=ROW_SHAMS_BUBBLE, col=1)

        shams_min, shams_max = daily["shams_bubble_percent"].min(), daily["shams_bubble_percent"].max()
        shams_min, shams_max = min(shams_min, shams_avg), max(shams_max, shams_avg)
        shams_padding = 0.5 if shams_min == shams_max else (shams_max - shams_min) * 0.3
        fig.update_yaxes(range=[shams_min - shams_padding, shams_max + shams_padding], row=ROW_SHAMS_BUBBLE, col=1)

        # ─── ردیف ۳: حباب صندوق‌ها + میانگینش ───
        fig.add_trace(dict(
            type="scatter", x=daily["timestamp"], y=daily["fund_weighted_bubble_percent"],
            name="حباب صندوق‌ها", mode="lines+markers",
            line=dict(color="#2196F3", width=5, shape="spline"),
            marker=dict(size=10),
            hovertemplate="صندوق‌ها: <b>%{y:+.2f}%</b><extra></extra>",
        ), row=ROW_FUND_BUBBLE, col=1)

        fig.add_trace(dict(
            type="scatter", x=x_range, y=[fund_avg, fund_avg],
            name="میانگین حباب صندوق‌ها", mode="lines",
            line=dict(color="#2196F3", width=3, dash="dot"),
            hovertemplate=f"میانگین صندوق‌ها: <b>{fund_avg:+.2f}%</b><extra></extra>",
        ), row=ROW_FUND_BUBBLE, col=1)

        fund_min, fund_max = daily["fund_weighted_bubble_percent"].min(), daily["fund_weighted_bubble_percent"].max()
        fund_min, fund_max = min(fund_min, fund_avg), max(fund_max, fund_avg)
        fund_padding = 0.5 if fund_min == fund_max else (fund_max - fund_min) * 0.3
        fig.update_yaxes(range=[fund_min - fund_padding, fund_max + fund_padding], row=ROW_FUND_BUBBLE, col=1)

        fig.update_layout(showlegend=False)

        # ─── ردیف ۴: پول حقیقی تجمعی ───
        add_conditional_line(fig, daily, "pol_hagigi_cumulative", ROW_POL_HAGIGI)
        set_y_range(fig, daily, "pol_hagigi_cumulative", ROW_POL_HAGIGI)

        # ─── ردیف ۵: سرانه خرید/فروش + اختلاف ───
        fig.add_trace(dict(
            type="scatter", x=daily["timestamp"], y=daily["sarane_kharid_weighted"],
            name="خرید حقیقی", mode="lines+markers",
            line=dict(color=COLOR_POSITIVE, width=5, shape="spline"),
            marker=dict(size=10),
            hovertemplate="خرید: <b>%{y:.2f}</b><extra></extra>",
            yaxis="y" + str(ROW_SARANE),
        ), row=ROW_SARANE, col=1)

        fig.add_trace(dict(
            type="scatter", x=daily["timestamp"], y=daily["sarane_forosh_weighted"],
            name="فروش حقیقی", mode="lines+markers",
            line=dict(color=COLOR_NEGATIVE, width=5, shape="spline"),
            marker=dict(size=10),
            hovertemplate="فروش: <b>%{y:.2f}</b><extra></extra>",
            yaxis="y" + str(ROW_SARANE),
        ), row=ROW_SARANE, col=1)

        colors_fill = [
            "rgba(0,230,118,0.75)" if x > 0 else "rgba(255,23,68,0.75)" if x < 0 else "rgba(72,79,88,0.75)"
            for x in daily["ekhtelaf_sarane_weighted"]
        ]
        ekhtelaf_axis = "y" + str(ROW_SARANE + 10)
        fig.add_trace(dict(
            type="bar", x=daily["timestamp"], y=daily["ekhtelaf_sarane_weighted"],
            name="اختلاف سرانه", width=1000 * 60 * 60 * 20,
            marker=dict(color=colors_fill, line=dict(color=colors_fill, width=4)),
            hovertemplate="اختلاف: <b>%{y:.2f}</b><extra></extra>",
            yaxis=ekhtelaf_axis,
        ), row=ROW_SARANE, col=1)

        kharid_min, kharid_max = daily["sarane_kharid_weighted"].min(), daily["sarane_kharid_weighted"].max()
        forosh_min, forosh_max = daily["sarane_forosh_weighted"].min(), daily["sarane_forosh_weighted"].max()
        lines_min, lines_max = min(kharid_min, forosh_min), max(kharid_max, forosh_max)
        lines_padding = max(10, (lines_max - lines_min) * 0.2)
        fig.update_yaxes(range=[lines_min - lines_padding, lines_max + lines_padding], row=ROW_SARANE, col=1)

        ekhtelaf_min = daily["ekhtelaf_sarane_weighted"].min()
        ekhtelaf_max = daily["ekhtelaf_sarane_weighted"].max()
        ekhtelaf_padding = max(10, (ekhtelaf_max - ekhtelaf_min) * 0.2)
        fig.update_layout(**{
            ekhtelaf_axis.replace("y", "yaxis"): dict(
                overlaying="y" + str(ROW_SARANE), side="right",
                range=[ekhtelaf_min - ekhtelaf_padding, ekhtelaf_max + ekhtelaf_padding],
                showgrid=False, showticklabels=False, zeroline=False,
            )
        })

        # ─── Layout کلی ───
        fig.update_layout(
            height=WEEKLY_CHART_HEIGHT,
            paper_bgcolor=COLOR_BACKGROUND,
            plot_bgcolor=COLOR_BACKGROUND,
            font=dict(color="#C9D1D9", family=chart_font_family, size=25),
            hovermode="x unified",
            margin=dict(l=60, r=120, t=140, b=60),
            barmode="overlay",
        )

        fig.add_annotation(
            text=f"<b>📅 گزارش هفتگی بازار {label}</b>",
            x=0.98, y=1.05, xref="paper", yref="paper",
            xanchor="right", yanchor="top",
            font=dict(size=40, color=accent_color, family=chart_font_family),
            showarrow=False,
        )
        fig.add_annotation(
            text=f"<b>{date_time_str}</b>",
            x=0.02, y=1.05, xref="paper", yref="paper",
            xanchor="left", yanchor="top",
            font=dict(size=36, color="#FFFFFF", family=chart_font_family),
            showarrow=False,
        )

        # ═══════════════════════════════════════════════════════
        # برچسب‌های آخرین مقدار در انتهای هر نمودار (مشابه گزارش روزانه)
        # ═══════════════════════════════════════════════════════
        last_shams = daily["shams_bubble_percent"].iloc[-1]
        last_fund = daily["fund_weighted_bubble_percent"].iloc[-1]
        last_pol_cum = daily["pol_hagigi_cumulative"].iloc[-1]
        last_kharid = daily["sarane_kharid_weighted"].iloc[-1]
        last_forosh = daily["sarane_forosh_weighted"].iloc[-1]
        last_ekhtelaf = daily["ekhtelaf_sarane_weighted"].iloc[-1]
        last_trade_value = daily["trade_value"].iloc[-1]
        last_ma5 = daily["trade_value_ma5"].iloc[-1]
        last_ma22 = daily["trade_value_ma22"].iloc[-1]

        label_font = dict(size=28, color="#8B949E", family=chart_font_family)

        # ردیف ۱: ارزش معاملات + میانگین ۵ و ۲۲ روزه
        fig.add_annotation(
            text=f"<b>{last_trade_value:,.0f}</b>",
            x=1.01, y=last_trade_value, xref="paper", yref=f"y{ROW_TRADE_VALUE}",
            xanchor="left", yanchor="middle",
            font=dict(size=28, color="#2196F3", family=chart_font_family), showarrow=False,
        )
        fig.add_annotation(
            text=f"<b>MA{MA_SHORT_WINDOW}: {last_ma5:,.0f}</b>",
            x=1.01, y=last_ma5, xref="paper", yref=f"y{ROW_TRADE_VALUE}",
            xanchor="left", yanchor="middle",
            font=dict(size=26, color=COLOR_POSITIVE, family=chart_font_family), showarrow=False,
        )
        fig.add_annotation(
            text=f"<b>MA{MA_LONG_WINDOW}: {last_ma22:,.0f}</b>",
            x=1.01, y=last_ma22, xref="paper", yref=f"y{ROW_TRADE_VALUE}",
            xanchor="left", yanchor="middle",
            font=dict(size=26, color="#FFA726", family=chart_font_family), showarrow=False,
        )

        # ردیف ۲: حباب شمش (مقدار آخر + میانگین هفته)
        shams_color = COLOR_POSITIVE if last_shams >= 0 else COLOR_NEGATIVE
        fig.add_annotation(
            text=f"<b>{last_shams:+.2f}%</b>",
            x=1.01, y=last_shams, xref="paper", yref=f"y{ROW_SHAMS_BUBBLE}",
            xanchor="left", yanchor="middle",
            font=dict(size=28, color=shams_color, family=chart_font_family), showarrow=False,
        )
        fig.add_annotation(
            text=f"<b>میانگین: {shams_avg:+.2f}%</b>",
            x=1.01, y=shams_avg, xref="paper", yref=f"y{ROW_SHAMS_BUBBLE}",
            xanchor="left", yanchor="middle", font=label_font, showarrow=False,
        )

        # ردیف ۳: حباب صندوق‌ها (مقدار آخر + میانگین هفته)
        fund_color = COLOR_POSITIVE if last_fund >= 0 else COLOR_NEGATIVE
        fig.add_annotation(
            text=f"<b>{last_fund:+.2f}%</b>",
            x=1.01, y=last_fund, xref="paper", yref=f"y{ROW_FUND_BUBBLE}",
            xanchor="left", yanchor="middle",
            font=dict(size=28, color=fund_color, family=chart_font_family), showarrow=False,
        )
        fig.add_annotation(
            text=f"<b>میانگین: {fund_avg:+.2f}%</b>",
            x=1.01, y=fund_avg, xref="paper", yref=f"y{ROW_FUND_BUBBLE}",
            xanchor="left", yanchor="middle", font=label_font, showarrow=False,
        )

        # ردیف ۴: پول حقیقی تجمعی
        pol_color = COLOR_POSITIVE if last_pol_cum >= 0 else COLOR_NEGATIVE
        fig.add_annotation(
            text=f"<b>{int(last_pol_cum):+,}</b>".replace(",", "٬"),
            x=1.01, y=last_pol_cum, xref="paper", yref=f"y{ROW_POL_HAGIGI}",
            xanchor="left", yanchor="middle",
            font=dict(size=28, color=pol_color, family=chart_font_family), showarrow=False,
        )

        # ردیف ۵: سرانه خرید/فروش/اختلاف
        lines_range = lines_max - lines_min
        kharid_y = lines_max - (lines_range * 0.05)
        forosh_y = lines_min + (lines_range * 0.05)
        ekhtelaf_y = (lines_max + lines_min) / 2
        ekhtelaf_color = COLOR_POSITIVE if last_ekhtelaf >= 0 else COLOR_NEGATIVE

        fig.add_annotation(
            text=f"<b>خ: {int(last_kharid):,}</b>".replace(",", "٬"),
            x=1.01, y=kharid_y, xref="paper", yref=f"y{ROW_SARANE}",
            xanchor="left", yanchor="middle",
            font=dict(size=28, color=COLOR_POSITIVE, family=chart_font_family), showarrow=False,
        )
        fig.add_annotation(
            text=f"<b>اخ: {int(last_ekhtelaf):+,}</b>".replace(",", "٬"),
            x=1.01, y=ekhtelaf_y, xref="paper", yref=f"y{ROW_SARANE}",
            xanchor="left", yanchor="middle",
            font=dict(size=28, color=ekhtelaf_color, family=chart_font_family), showarrow=False,
        )
        fig.add_annotation(
            text=f"<b>ف: {int(last_forosh):,}</b>".replace(",", "٬"),
            x=1.01, y=forosh_y, xref="paper", yref=f"y{ROW_SARANE}",
            xanchor="left", yanchor="middle",
            font=dict(size=28, color=COLOR_NEGATIVE, family=chart_font_family), showarrow=False,
        )

        # محور X: یک تیک به‌ازای هر روز، با اسم روز هفته
        fig.update_xaxes(
            type="date",
            tickmode="array",
            tickvals=daily["timestamp"].tolist(),
            ticktext=daily["day_label"].tolist(),
            tickfont=dict(size=26),
            gridcolor=COLOR_GRID, showgrid=True, zeroline=False,
            showline=True, linewidth=1, linecolor="#30363D",
        )
        for i in range(1, WEEKLY_ROW_COUNT + 1):
            fig.update_yaxes(
                tickfont=dict(size=25), gridcolor=COLOR_GRID, showgrid=True,
                zeroline=True, zerolinecolor="#30363D", zerolinewidth=2,
                showline=True, linewidth=1, linecolor="#30363D",
                row=i, col=1,
            )
            fig.add_hline(y=0, line_dash="dot", line_color="#484F58", line_width=2, row=i, col=1)

        img_bytes = fig.to_image(format="png", width=CHART_WIDTH, height=WEEKLY_CHART_HEIGHT, scale=CHART_SCALE)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

        try:
            draw = ImageDraw.Draw(img)
            font = ImageFont.truetype(FONT_REGULAR_PATH, 46)
            text = CHANNEL_HANDLE.replace("@", "")
            bbox = draw.textbbox((0, 0), text, font=font)
            w = bbox[2] - bbox[0]
            x = img.width - w - 25
            y = int(img.height * 0.9)
            draw.text((x, y), text, fill=(201, 209, 217, 160), font=font)
        except Exception as e:
            logger.warning(f"⚠️ خطا در واترمارک: {e}")

        output = io.BytesIO()
        img.save(output, format="PNG", optimize=True, quality=92)
        output.seek(0)
        logger.info(f"✅ [{commodity}] گزارش هفتگی ساخته شد ({len(daily)} روز)")
        return output.getvalue()

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در ساخت گزارش هفتگی: {e}", exc_info=True)
        return None


def build_weekly_package(commodity):
    """
    نقطه‌ی ورود عمومی ماژول. داده‌ی هفته را یک‌بار می‌خواند و هم عکس هم کپشن را برمی‌گرداند.

    Returns:
        dict با کلیدهای 'image_bytes' و 'caption'، یا None اگر داده‌ی کافی نبود
        (حداقل ۲ روز کاری لازم است تا نمودار روند معنا داشته باشد).
    """
    if commodity not in COMMODITY_LABEL:
        raise ValueError(f"کالای نامعتبر: {commodity}")

    daily = _load_week_dataframe(commodity)
    if daily is None or len(daily) < 2:
        logger.warning(f"⚠️ [{commodity}] برای گزارش هفتگی حداقل ۲ روز داده لازم است")
        return None

    image_bytes = _render_weekly_chart(commodity, daily)
    if image_bytes is None:
        return None

    return {
        "image_bytes": image_bytes,
        "caption": build_weekly_caption(commodity, daily),
    }


def build_weekly_caption(commodity, daily_df):
    """کپشن متنی خلاصه‌ی هفته برای ارسال همراه عکس."""
    label = COMMODITY_LABEL[commodity]
    last = daily_df.iloc[-1]
    week_start, week_end = _current_trading_week_range()

    total_pol = daily_df["pol_hagigi"].sum()
    avg_bubble_fund = daily_df["fund_weighted_bubble_percent"].mean()
    avg_bubble_shams = daily_df["shams_bubble_percent"].mean()

    return f"""
📅 <b>گزارش هفتگی بازار {label}</b>
🗓 {week_start.strftime('%Y-%m-%d')} تا {week_end.strftime('%Y-%m-%d')} ({len(daily_df)} روز کاری)

🎈 میانگین حباب شمش: {avg_bubble_shams:+.2f}%
🎈 میانگین حباب صندوق‌ها: {avg_bubble_fund:+.2f}%
💸 پول حقیقی تجمعی هفته: {total_pol:+,.0f} م.ت
📊 سرانه خرید آخرین روز: {last['sarane_kharid_weighted']:,.0f}
📊 سرانه فروش آخرین روز: {last['sarane_forosh_weighted']:,.0f}
⚖️ اختلاف سرانه آخرین روز: {last['ekhtelaf_sarane_weighted']:+,.0f}
💰 ارزش معاملات آخرین روز: {last['trade_value']:,.0f} (MA۵: {last['trade_value_ma5']:,.0f} | MA۲۲: {last['trade_value_ma22']:,.0f})

🔗 {CHANNEL_HANDLE}
""".strip()
