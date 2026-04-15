from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from coinbase.rest import RESTClient

PUBLIC_CANDLES_URL = "https://api.exchange.coinbase.com/products"
STATE_FILE = "coinbase_bot_state.json"
LOG_FILE = "coinbase_ready_bot.log"


@dataclass
class Config:
    api_key: str
    api_secret: str
    product_id: str = "BTC-USD"
    quote_currency: str = "USD"
    base_currency: str = "BTC"
    dry_run: bool = True
    granularity_seconds: int = 300
    candle_span_seconds: int = 300 * 300
    fast_ema: int = 9
    slow_ema: int = 21
    max_position_fraction: float = 0.10
    max_quote_per_buy: float = 25.0
    min_quote_per_buy: float = 10.0
    stop_loss_pct: float = 0.03
    cooldown_minutes: int = 30
    poll_seconds: int = 60


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_config() -> Config:
    load_dotenv()

    api_key = os.getenv("COINBASE_API_KEY", "").strip()
    api_secret = os.getenv("COINBASE_API_SECRET", "").strip()
    product_id = os.getenv("PRODUCT_ID", "BTC-USD").strip().upper()
    dry_run = os.getenv("DRY_RUN", "true").strip().lower() == "true"

    if not api_key or not api_secret:
        raise ValueError("Missing COINBASE_API_KEY or COINBASE_API_SECRET")

    parts = product_id.split("-")
    if len(parts) != 2:
        raise ValueError("PRODUCT_ID must look like BTC-USD")

    return Config(
        api_key=api_key,
        api_secret=api_secret,
        product_id=product_id,
        base_currency=parts[0],
        quote_currency=parts[1],
        dry_run=dry_run,
    )


def load_state() -> dict:
    if not Path(STATE_FILE).exists():
        return {"entry_price": None, "last_trade_ts": None}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def decimal_str(value: float, places: int) -> str:
    pattern = "1." + ("0" * places)
    return str(Decimal(str(value)).quantize(Decimal(pattern), rounding=ROUND_DOWN))


def get_public_candles(product_id: str, granularity_seconds: int, candle_span_seconds: int) -> pd.DataFrame:
    end = utc_now()
    start = end - timedelta(seconds=candle_span_seconds)

    url = PUBLIC_CANDLES_URL.format(product_id=product_id)
    params = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "granularity": granularity_seconds,
    }

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()

    payload = r.json()
    if not payload:
        raise ValueError(f"No candle data returned for {product_id}")

    df = pd.DataFrame(payload, columns=["time", "low", "high", "open", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

    for col in ["low", "high", "open", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("time").reset_index(drop=True)
    return df


def add_indicators(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=slow, adjust=False).mean()
    out["signal"] = (out["ema_fast"] > out["ema_slow"]).astype(int)
    out["cross"] = out["signal"].diff().fillna(0)
    return out


def latest_signal(df: pd.DataFrame) -> tuple[int, float]:
    row = df.iloc[-1]
    return int(row["cross"]), float(row["close"])


def get_available_balances(client: RESTClient) -> dict[str, float]:
    balances: dict[str, float] = {}
    response = client.get_accounts()

    accounts = []
    if isinstance(response, dict):
        accounts = response.get("accounts", [])
    else:
        accounts = getattr(response, "accounts", [])

    for acct in accounts:
        if isinstance(acct, dict):
            currency = acct.get("currency")
            available = ((acct.get("available_balance") or {}).get("value"))
        else:
            currency = getattr(acct, "currency", None)
            available_balance = getattr(acct, "available_balance", None)
            available = getattr(available_balance, "value", None) if available_balance else None

        if currency and available is not None:
            try:
                balances[currency] = float(available)
            except Exception:
                pass

    return balances


def get_quote_to_spend(quote_balance: float, max_fraction: float, max_quote_per_buy: float) -> float:
    spend = quote_balance * max_fraction
    spend = min(spend, max_quote_per_buy)
    return max(spend, 0.0)


def cooldown_active(last_trade_ts: str | None, cooldown_minutes: int) -> bool:
    if not last_trade_ts:
        return False
    last = datetime.fromisoformat(last_trade_ts)
    seconds = (utc_now() - last.astimezone(timezone.utc)).total_seconds()
    return seconds < cooldown_minutes * 60


def stop_loss_hit(entry_price: float | None, current_price: float, stop_loss_pct: float) -> bool:
    if entry_price is None:
        return False
    return current_price <= entry_price * (1 - stop_loss_pct)


def place_market_buy(client: RESTClient, cfg: Config, quote_size: float):
    return client.market_order_buy(
        client_order_id=str(uuid.uuid4()),
        product_id=cfg.product_id,
        quote_size=decimal_str(quote_size, 2),
    )


def place_market_sell(client: RESTClient, cfg: Config, base_size: float):
    return client.market_order_sell(
        client_order_id=str(uuid.uuid4()),
        product_id=cfg.product_id,
        base_size=decimal_str(base_size, 8),
    )


def main() -> None:
    setup_logging()
    cfg = load_config()
    state = load_state()

    client = RESTClient(api_key=cfg.api_key, api_secret=cfg.api_secret)

    logging.info("Starting bot")
    logging.info("Product: %s", cfg.product_id)
    logging.info("Dry run: %s", cfg.dry_run)

    while True:
        try:
            df = get_public_candles(cfg.product_id, cfg.granularity_seconds, cfg.candle_span_seconds)
            df = add_indicators(df, cfg.fast_ema, cfg.slow_ema)
            cross, current_price = latest_signal(df)

            balances = get_available_balances(client)
            base_qty = balances.get(cfg.base_currency, 0.0)
            quote_balance = balances.get(cfg.quote_currency, 0.0)

            logging.info(
                "Signal | price=%.2f | cross=%s | %s=%.8f | %s=%.2f",
                current_price,
                cross,
                cfg.base_currency,
                base_qty,
                cfg.quote_currency,
                quote_balance,
            )

            if cooldown_active(state.get("last_trade_ts"), cfg.cooldown_minutes):
                logging.info("Cooldown active")
                time.sleep(cfg.poll_seconds)
                continue

            if base_qty > 0 and stop_loss_hit(state.get("entry_price"), current_price, cfg.stop_loss_pct):
                logging.warning("Stop loss triggered")
                if cfg.dry_run:
                    logging.info("DRY RUN | Would SELL %.8f %s", base_qty, cfg.base_currency)
                else:
                    result = place_market_sell(client, cfg, base_qty)
                    logging.info("SELL RESULT | %s", result)
                state["entry_price"] = None
                state["last_trade_ts"] = utc_now().isoformat()
                save_state(state)
                time.sleep(cfg.poll_seconds)
                continue

            if cross > 0 and base_qty <= 0:
                quote_to_spend = get_quote_to_spend(
                    quote_balance=quote_balance,
                    max_fraction=cfg.max_position_fraction,
                    max_quote_per_buy=cfg.max_quote_per_buy,
                )

                if quote_to_spend < cfg.min_quote_per_buy:
                    logging.info("Buy skipped | not enough quote balance")
                else:
                    if cfg.dry_run:
                        logging.info("DRY RUN | Would BUY %s worth %.2f", cfg.product_id, quote_to_spend)
                    else:
                        result = place_market_buy(client, cfg, quote_to_spend)
                        logging.info("BUY RESULT | %s", result)
                    state["entry_price"] = current_price
                    state["last_trade_ts"] = utc_now().isoformat()
                    save_state(state)

            elif cross < 0 and base_qty > 0:
                if cfg.dry_run:
                    logging.info("DRY RUN | Would SELL %.8f %s", base_qty, cfg.base_currency)
                else:
                    result = place_market_sell(client, cfg, base_qty)
                    logging.info("SELL RESULT | %s", result)
                state["entry_price"] = None
                state["last_trade_ts"] = utc_now().isoformat()
                save_state(state)

            time.sleep(cfg.poll_seconds)

        except KeyboardInterrupt:
            logging.info("Stopped by user")
            break
        except Exception as e:
            logging.exception("Loop error: %s", e)
            time.sleep(30)


if __name__ == "__main__":
    main()