# utils/data_processor.py
"""پردازش و تحلیل داده‌های بازار طلا و نقره (commodity-generic)"""

import pandas as pd
import numpy as np
import logging
from config import ASSET_ORDER, PRICING_FACTORS, TROY_OZ

pd.set_option("future.no_silent_downcasting", True)
logger = logging.getLogger(__name__)

pd.options.display.float_format = "{:,.2f}".format


# ==============================================================================
# ورودی اصلی
# ==============================================================================

def process_market_data(
    commodity, market_data, global_price, dollar_last_trade,
    yesterday_close=None, global_price_yesterday=None,
):
    """
    پردازش کامل داده‌ی یک کالا (gold یا silver).

    Args:
        commodity: 'gold' یا 'silver'
        market_data: خروجی fetch_market_data → {'intrinsic_data':..., 'funds_data':...}
        global_price: قیمت جهانی انس (از fetch_light_chart)
        dollar_last_trade: دلار بازار آزاد (از fetch_dollar_prices/تلگرام)
        yesterday_close: قیمت دلار دیروز (اختیاری)
        global_price_yesterday: قیمت جهانی دیروز (اختیاری)
    """
    try:
        if commodity not in ASSET_ORDER or commodity not in PRICING_FACTORS:
            raise ValueError(f"کالای نامعتبر یا بدون تنظیمات: {commodity}")

        intrinsic_json = market_data["intrinsic_data"]["data"]
        funds_raw = market_data["funds_data"]

        assets_df = pd.DataFrame(intrinsic_json["assets"])
        warehouse_df = pd.DataFrame(intrinsic_json["warehouse_receipt_systems"])

        assets_df = flatten_entities(assets_df, "related_entities")
        warehouse_df = flatten_entities(warehouse_df, "related_entities")

        assets_df.drop(
            [
                "entity_id", "type", "asset_id", "short_name",
                "intrinsic_value", "price_bubble", "price_bubble_percent",
                "calculated_usdirr", "name",
            ],
            axis=1, inplace=True, errors="ignore",
        )
        assets_df.set_index("slug", inplace=True)

        warehouse_df.drop(
            [
                "entity_id", "type", "asset_id", "short_name",
                "intrinsic_value", "price_bubble", "price_bubble_percent",
                "calculated_usdirr", "trade_symbol", "name", "value", "volume",
            ],
            axis=1, inplace=True, errors="ignore",
        )
        warehouse_df.set_index("slug", inplace=True)

        dfp = pd.concat([warehouse_df, assets_df])
        dfp = dfp[~dfp.index.duplicated(keep="first")]

        dfp["trade_date"] = dfp["last_trade_time"].str[:10]
        dfp["last_trade_time"] = dfp["last_trade_time"].str[11:19]

        dfp["close_price_change_percent"] = (
            pd.to_numeric(dfp["close_price_change_percent"], errors="coerce") * 100
        ).round(2)

        dfp = dfp.reindex(ASSET_ORDER[commodity])
        dfp.insert(1, "Value", np.nan)
        dfp["pricing_dollar"] = np.nan
        dfp[f"pricing_{commodity}"] = np.nan

        dfp = calculate_values(dfp, commodity, global_price, dollar_last_trade)

        Fund_df = process_funds_data(funds_raw, commodity)

        return {
            "dfp": dfp,
            "Fund_df": Fund_df,
            "commodity": commodity,
            "global_price": global_price,
            "dollar_last_trade": dollar_last_trade,
            "yesterday_close": yesterday_close,
            "global_price_yesterday": global_price_yesterday,
        }

    except Exception as e:
        logger.error(f"❌ [{commodity}] خطا در پردازش داده‌ها: {e}", exc_info=True)
        return None


def flatten_entities(df, list_col="related_entities"):
    if list_col in df.columns:
        return pd.json_normalize(
            df.to_dict(orient="records"),
            list_col,
            meta=[col for col in df.columns if col != list_col],
            errors="ignore",
        )
    return df


# ==============================================================================
# صندوق‌ها (تریدرآرنا) — column_mapping مشترک بین طلا/نقره (تأیید‌شده)
# ==============================================================================

def process_funds_data(data, commodity):
    """پردازش داده‌ی صندوق‌های یک کالا از تریدرآرنا با mapping ثابت ایندکس→نام."""

    if not data or len(data) == 0:
        logger.warning(f"⚠️ [{commodity}] داده‌ی صندوق‌ها خالی است")
        return pd.DataFrame()

    column_mapping = {
        0: "id", 1: "symbol", 2: "volume", 3: "value",
        4: "first_price", 5: "first_price_change_percent",
        6: "high_price", 7: "high_price_change_percent",
        8: "low_price", 9: "low_price_change_percent",
        10: "close_price", 11: "close_price_change_percent",
        12: "final_price", 13: "final_price_change_percent",
        14: "close_final_diff", 15: "volitility",
        16: "sarane_kharid", 17: "sarane_forosh", 18: "buy_power",
        19: "pol_hagigi", 20: "buy_order_value", 21: "sell_order_value",
        22: "buy_sell_order_sum", 23: "5day_avg_pol_hagigi",
        24: "20day_avg_pol_hagigi", 25: "60day_avg_pol_hagigi",
        26: "5day_pol_hagigi", 27: "20day_pol_hagigi", 28: "60day_pol_hagigi",
        29: "5day_buy_power", 30: "20day_buy_power",
        31: "avg_monthly_value", 32: "value_to_avg_ratio",
        35: "weekly_return", 36: "monthly_return", 37: "3_month_return",
        38: "net_asset", 40: "NAV", 41: "nominal_bubble",
        42: "NAV_change_percent", 43: "avg_monthly_bubble",
        49: "category", 50: "isin",
    }

    extracted_data = []
    for row in data:
        extracted_row = {}
        for idx, col_name in column_mapping.items():
            extracted_row[col_name] = row[idx] if idx < len(row) else None
        extracted_data.append(extracted_row)

    Fund_df = pd.DataFrame(extracted_data)
    Fund_df = Fund_df.set_index("symbol")

    Fund_df["value"] = pd.to_numeric(Fund_df["value"], errors="coerce") / 10_000_000_000
    Fund_df["sarane_kharid"] = pd.to_numeric(Fund_df["sarane_kharid"], errors="coerce") / 10_000_000
    Fund_df["sarane_forosh"] = pd.to_numeric(Fund_df["sarane_forosh"], errors="coerce") / 10_000_000
    Fund_df["pol_hagigi"] = pd.to_numeric(Fund_df["pol_hagigi"], errors="coerce") / 10_000_000_000

    Fund_df["avg_monthly_value"] = (
        Fund_df["avg_monthly_value"].replace("-", pd.NA)
        .pipe(pd.to_numeric, errors="coerce") / 10_000_000_000
    )

    Fund_df["NAV_change_percent"] = pd.to_numeric(
        Fund_df["NAV_change_percent"], errors="coerce"
    ).round(2)

    for col in ["weekly_return", "monthly_return", "3_month_return"]:
        if col in Fund_df.columns:
            Fund_df[col] = pd.to_numeric(Fund_df[col], errors="coerce").round(2)

    Fund_df["net_asset"] = (
        Fund_df["net_asset"].replace("-", pd.NA)
        .pipe(pd.to_numeric, errors="coerce").fillna(0) / 10_000_000_000
    )

    Fund_df["ekhtelaf_sarane"] = Fund_df["sarane_kharid"] - Fund_df["sarane_forosh"]
    Fund_df["pol_to_value_ratio"] = (
        (Fund_df["pol_hagigi"] / Fund_df["avg_monthly_value"].replace(0, pd.NA)) * 100
    ).round(2)

    Fund_df["final_price_change"] = pd.to_numeric(
        Fund_df["final_price_change_percent"], errors="coerce"
    ).round(2)

    Fund_df["value_to_avg_ratio"] = pd.to_numeric(
        Fund_df["value_to_avg_ratio"], errors="coerce"
    ).round(2)

    Fund_df["avg_monthly_bubble"] = pd.to_numeric(
        Fund_df["avg_monthly_bubble"], errors="coerce"
    ).round(2)

    Fund_df.sort_values(by="value", ascending=False, inplace=True)

    final_columns = [
        "close_price", "NAV", "nominal_bubble", "avg_monthly_bubble",
        "NAV_change_percent", "close_price_change_percent", "final_price_change",
        "weekly_return", "monthly_return", "3_month_return", "net_asset",
        "sarane_kharid", "sarane_forosh", "ekhtelaf_sarane", "pol_hagigi",
        "pol_to_value_ratio", "value", "avg_monthly_value", "value_to_avg_ratio",
    ]
    existing_columns = [col for col in final_columns if col in Fund_df.columns]
    Fund_df = Fund_df[existing_columns]

    logger.info(f"✅ [{commodity}] Fund_df پردازش شد - {len(Fund_df)} صندوق با {len(Fund_df.columns)} ستون")
    return Fund_df


# ==============================================================================
# فرمول ارزش ذاتی (Value) — commodity-generic
# ==============================================================================

def calculate_values(dfp, commodity, global_price, dollar_last_trade):
    """
    Value = (dollar_last_trade * global_price / TROY_OZ) * purity * weight * scale
    ضرایب per-asset از config.PRICING_FACTORS[commodity] خوانده می‌شوند
    (ترتیب باید با ASSET_ORDER[commodity] هم‌راستا باشد).
    """
    factors = PRICING_FACTORS[commodity]

    if len(factors) != len(dfp):
        logger.warning(
            f"⚠️ [{commodity}] تعداد PRICING_FACTORS ({len(factors)}) "
            f"با تعداد دارایی‌ها ({len(dfp)}) برابر نیست"
        )

    base = (dollar_last_trade * global_price) / TROY_OZ

    for i, f in enumerate(factors):
        if i >= len(dfp.index):
            break
        idx = dfp.index[i]
        dfp.loc[idx, "Value"] = base * f["purity"] * f["weight"] * f["scale"]

    dfp["Bubble"] = ((dfp["close_price"] - dfp["Value"]) / dfp["Value"]) * 100

    price_col = f"pricing_{commodity}"

    # pricing_dollar / pricing_{commodity}: فقط برای ۵ دارایی اصلی
    # (برای کالاهایی با کمتر از ۵ دارایی مثل نقره، خودکار روی همه اعمال می‌شود)
    for i in range(min(5, len(dfp))):
        idx = dfp.index[i]
        f = factors[i]
        factor = f["purity"] * f["weight"]
        scale = f["scale"]
        close = dfp.loc[idx, "close_price"]

        dfp.loc[idx, "pricing_dollar"] = (close * TROY_OZ) / (global_price * factor) / scale
        dfp.loc[idx, price_col] = ((close / factor) * TROY_OZ) / dollar_last_trade / scale

    cols = ["Value", "close_price", "pricing_dollar", price_col]
    dfp = dfp.copy()
    dfp[cols] = dfp[cols].fillna(0).astype(int)

    dfp = dfp[
        [
            "close_price", "Value", "Bubble", "close_price_change_percent",
            "pricing_dollar", price_col, "trade_date", "last_trade_time",
        ]
    ]

    return dfp
