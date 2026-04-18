import os
import time
import uuid
import logging
import requests
from datetime import datetime, timezone

from coinbase.rest import RESTClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

PRODUCT_ID = os.getenv("PRODUCT_ID", "BTC-USD")

LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "10"))
MAX_DAILY_LOSS_USD = float(os.getenv("MAX_DAILY_LOSS_USD", "10"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "3"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))

SELL_GAIN_PCT = float(os.getenv("SELL_GAIN_PCT", "0.003"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.002"))

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "")

# SIM-only tracking
sim_cash = 1000.0
sim_btc_holdings = 0.0

# Strategy state
last_buy_price = None
trade_count_today = 0
realized_pnl_today = 0.0
last_trade_ts = 0.0
current_day = datetime.now(timezone.utc).date()

client = None
if LIVE_TRADING:
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        raise ValueError("LIVE_TRADING is true but Coinbase API credentials are missing")
    client = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)


def normalize_response(resp):
    """
    Coinbase SDK may return typed objects instead of plain dicts.
    Convert to dict when possible.
    """
    if hasattr(resp, "to_dict"):
        return resp.to_dict()
    return resp


def get_price():
    """
    Public spot price from Coinbase Exchange public endpoint.
    """
    url = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    return float(data["price"])


def reset_daily_counters_if_needed():
    global current_day, trade_count_today, realized_pnl_today

    today = datetime.now(timezone.utc).date()
    if today != current_day:
        current_day = today
        trade_count_today = 0
        realized_pnl_today = 0.0
        logging.info("Daily counters reset")


def cooldown_active():
    return (time.time() - last_trade_ts) < COOLDOWN_SECONDS


def parse_available_balance(balance_obj):
    """
    available_balance often comes back like:
    {"value": "12.34", "currency": "USD"}
    """
    if isinstance(balance_obj, dict):
        value = balance_obj.get("value")
        if value is not None:
            try:
                return float(value)
            except Exception:
                return 0.0
    try:
        return float(balance_obj)
    except Exception:
        return 0.0


def get_live_balances():
    """
    Returns real available balances from Coinbase.
    """
    if client is None:
        return 0.0, 0.0

    accounts_resp = client.get_accounts()
    accounts_data = normalize_response(accounts_resp)

    usd_available = 0.0
    btc_available = 0.0

    accounts = []
    if isinstance(accounts_data, dict):
        accounts = accounts_data.get("accounts", [])

    for acct in accounts:
        currency = acct.get("currency")
        available_balance = parse_available_balance(acct.get("available_balance", {}))

        if currency == "USD":
            usd_available += available_balance
        elif currency == "BTC":
            btc_available += available_balance

    return usd_available, btc_available


def portfolio_value(current_price):
    if LIVE_TRADING:
        usd_available, btc_available = get_live_balances()
        return usd_available + (btc_available * current_price)

    return sim_cash + (sim_btc_holdings * current_price)


def buy(current_price):
    global sim_cash, sim_btc_holdings
    global last_buy_price, trade_count_today, last_trade_ts

    if cooldown_active():
        logging.info("BUY BLOCKED | Cooldown active")
        return

    if trade_count_today >= MAX_TRADES_PER_DAY:
        logging.info("BUY BLOCKED | Daily trade limit reached")
        return

    if realized_pnl_today <= -MAX_DAILY_LOSS_USD:
        logging.info("BUY BLOCKED | Daily loss limit reached")
        return

    if LIVE_TRADING:
        usd_available, _ = get_live_balances()
        if usd_available < TRADE_SIZE_USD:
            logging.info(
                "LIVE BUY BLOCKED | Available USD too low | USD available: $%.2f | Needed: $%.2f",
                usd_available, TRADE_SIZE_USD
            )
            return

        order = client.market_order_buy(
            client_order_id=str(uuid.uuid4()),
            product_id=PRODUCT_ID,
            quote_size=str(TRADE_SIZE_USD),
        )

        order_data = normalize_response(order)
        logging.info("LIVE BUY RESPONSE | %s", order_data)

        success = False
        if isinstance(order_data, dict):
            success = bool(order_data.get("success"))

        if success:
            last_buy_price = current_price
            trade_count_today += 1
            last_trade_ts = time.time()

            usd_after, btc_after = get_live_balances()
            logging.info(
                "LIVE BUY | Price: $%.2f | USD available: $%.2f | BTC available: %.8f | Trades today: %d",
                current_price, usd_after, btc_after, trade_count_today
            )
        else:
            error_info = order_data.get("error_response", order_data) if isinstance(order_data, dict) else order_data
            logging.error("LIVE BUY FAILED | %s", error_info)

        return

    # SIM mode
    if sim_cash < TRADE_SIZE_USD:
        logging.info("SIM BUY BLOCKED | Not enough cash | Cash: $%.2f", sim_cash)
        return

    btc_bought = TRADE_SIZE_USD / current_price
    sim_cash -= TRADE_SIZE_USD
    sim_btc_holdings += btc_bought
    last_buy_price = current_price
    trade_count_today += 1
    last_trade_ts = time.time()

    logging.info(
        "SIM BUY | Price: $%.2f | BTC bought: %.8f | Cash left: $%.2f | BTC holdings: %.8f | Trades today: %d",
        current_price, btc_bought, sim_cash, sim_btc_holdings, trade_count_today
    )


def sell(current_price, reason):
    global sim_cash, sim_btc_holdings
    global last_buy_price, realized_pnl_today, last_trade_ts

    if LIVE_TRADING:
        _, btc_available = get_live_balances()

        if btc_available <= 0:
            logging.info("LIVE SELL BLOCKED | No BTC available")
            return

        pnl = 0.0
        if last_buy_price is not None:
            pnl = (current_price - last_buy_price) * btc_available

        order = client.market_order_sell(
            client_order_id=str(uuid.uuid4()),
            product_id=PRODUCT_ID,
            base_size=str(round(btc_available, 8)),
        )

        order_data = normalize_response(order)
        logging.info("LIVE SELL RESPONSE | %s", order_data)

        success = False
        if isinstance(order_data, dict):
            success = bool(order_data.get("success"))

        if success:
            realized_pnl_today += pnl
            last_buy_price = None
            last_trade_ts = time.time()

            usd_after, btc_after = get_live_balances()
            logging.info(
                "LIVE SELL | Reason: %s | Price: $%.2f | Est PnL: $%.2f | Daily realized PnL: $%.2f | USD available: $%.2f | BTC available: %.8f",
                reason, current_price, pnl, realized_pnl_today, usd_after, btc_after
            )
        else:
            error_info = order_data.get("error_response", order_data) if isinstance(order_data, dict) else order_data
            logging.error("LIVE SELL FAILED | %s", error_info)

        return

    # SIM mode
    if sim_btc_holdings <= 0:
        return

    usd_received = sim_btc_holdings * current_price
    pnl = 0.0
    if last_buy_price is not None:
        pnl = (current_price - last_buy_price) * sim_btc_holdings

    sim_cash += usd_received
    realized_pnl_today += pnl

    logging.info(
        "SIM SELL | Reason: %s | Price: $%.2f | USD received: $%.2f | Trade PnL: $%.2f | Daily realized PnL: $%.2f | Cash: $%.2f",
        reason, current_price, usd_received, pnl, realized_pnl_today, sim_cash
    )

    sim_btc_holdings = 0.0
    last_buy_price = None
    last_trade_ts = time.time()


def main():
    logging.info("LIVE READY BOT STARTING")
    logging.info("Tracking %s", PRODUCT_ID)
    logging.info("LIVE_TRADING = %s", LIVE_TRADING)
    logging.info("Trade size: $%.2f", TRADE_SIZE_USD)
    logging.info("Daily loss limit: $%.2f", MAX_DAILY_LOSS_USD)
    logging.info("Max trades/day: %d", MAX_TRADES_PER_DAY)
    logging.info("Sell gain pct: %.4f", SELL_GAIN_PCT)
    logging.info("Stop loss pct: %.4f", STOP_LOSS_PCT)

    first_buy_done = False

    while True:
        try:
            reset_daily_counters_if_needed()
            price = get_price()

            if LIVE_TRADING:
                usd_available, btc_available = get_live_balances()

                # First entry only if flat and enough USD available
                if not first_buy_done:
                    if btc_available <= 0 and usd_available >= TRADE_SIZE_USD:
                        buy(price)
                        first_buy_done = True
                    elif btc_available > 0:
                        # Already holding something, do not force a second first buy
                        first_buy_done = True

                # Exit logic
                if btc_available > 0 and last_buy_price is not None:
                    gain_pct = (price - last_buy_price) / last_buy_price
                    loss_pct = (last_buy_price - price) / last_buy_price

                    if gain_pct >= SELL_GAIN_PCT:
                        sell(price, "target hit")
                    elif loss_pct >= STOP_LOSS_PCT:
                        sell(price, "stop loss")

                usd_available, btc_available = get_live_balances()
                logging.info(
                    "BTC Price: $%.2f | USD available: $%.2f | BTC available: %.8f | Portfolio: $%.2f | Daily PnL: $%.2f",
                    price, usd_available, btc_available, portfolio_value(price), realized_pnl_today
                )

            else:
                if not first_buy_done and sim_btc_holdings == 0.0:
                    buy(price)
                    first_buy_done = True

                if sim_btc_holdings > 0 and last_buy_price is not None:
                    gain_pct = (price - last_buy_price) / last_buy_price
                    loss_pct = (last_buy_price - price) / last_buy_price

                    if gain_pct >= SELL_GAIN_PCT:
                        sell(price, "target hit")
                    elif loss_pct >= STOP_LOSS_PCT:
                        sell(price, "stop loss")

                logging.info(
                    "BTC Price: $%.2f | Cash: $%.2f | BTC: %.8f | Portfolio: $%.2f | Daily PnL: $%.2f",
                    price, sim_cash, sim_btc_holdings, portfolio_value(price), realized_pnl_today
                )

        except Exception as e:
            logging.exception("Loop error: %s", e)

        time.sleep(10)


if __name__ == "__main__":
    main()