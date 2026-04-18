import os
import time
import uuid
import logging
import requests
from datetime import datetime

from coinbase.rest import RESTClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

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

cash = 1000.0
btc_holdings = 0.0
last_buy_price = None
trade_count_today = 0
realized_pnl_today = 0.0
last_trade_ts = 0.0
current_day = datetime.utcnow().date()

client = None
if LIVE_TRADING:
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        raise ValueError("LIVE_TRADING is true but Coinbase API credentials are missing")
    client = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)


def get_price():
    url = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    return float(data["price"])


def portfolio_value(current_price):
    return cash + (btc_holdings * current_price)


def reset_daily_counters_if_needed():
    global current_day, trade_count_today, realized_pnl_today
    today = datetime.utcnow().date()
    if today != current_day:
        current_day = today
        trade_count_today = 0
        realized_pnl_today = 0.0
        logging.info("Daily counters reset")


def cooldown_active():
    return (time.time() - last_trade_ts) < COOLDOWN_SECONDS


def buy(current_price):
    global cash, btc_holdings, last_buy_price, trade_count_today, last_trade_ts

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
        order = client.market_order_buy(
            client_order_id=str(uuid.uuid4()),
            product_id=PRODUCT_ID,
            quote_size=str(TRADE_SIZE_USD),
        )

        if order.get("success"):
            logging.info("LIVE BUY | %s", order)
            # Approximate holdings for local tracking using current price
            btc_bought = TRADE_SIZE_USD / current_price
            btc_holdings += btc_bought
            last_buy_price = current_price
            trade_count_today += 1
            last_trade_ts = time.time()
        else:
            logging.error("LIVE BUY FAILED | %s", order)
        return

    if cash < TRADE_SIZE_USD:
        logging.info("SIM BUY SKIPPED | Not enough cash | Cash: $%.2f", cash)
        return

    btc_bought = TRADE_SIZE_USD / current_price
    cash -= TRADE_SIZE_USD
    btc_holdings += btc_bought
    last_buy_price = current_price
    trade_count_today += 1
    last_trade_ts = time.time()

    logging.info(
        "SIM BUY | Price: $%.2f | BTC bought: %.6f | Cash left: $%.2f | BTC holdings: %.6f | Trades today: %d",
        current_price, btc_bought, cash, btc_holdings, trade_count_today
    )


def sell(current_price, reason):
    global cash, btc_holdings, last_buy_price, realized_pnl_today, last_trade_ts

    if btc_holdings <= 0:
        return

    usd_received = btc_holdings * current_price
    pnl = 0.0
    if last_buy_price is not None:
        pnl = (current_price - last_buy_price) * btc_holdings

    if LIVE_TRADING:
        order = client.market_order_sell(
            client_order_id=str(uuid.uuid4()),
            product_id=PRODUCT_ID,
            base_size=str(round(btc_holdings, 8)),
        )

        if order.get("success"):
            realized_pnl_today += pnl
            logging.info("LIVE SELL | Reason: %s | %s", reason, order)
            btc_holdings = 0.0
            last_buy_price = None
            last_trade_ts = time.time()
        else:
            logging.error("LIVE SELL FAILED | %s", order)
        return

    cash += usd_received
    realized_pnl_today += pnl

    logging.info(
        "SIM SELL | Reason: %s | Price: $%.2f | USD received: $%.2f | Trade PnL: $%.2f | Daily realized PnL: $%.2f | Cash: $%.2f",
        reason, current_price, usd_received, pnl, realized_pnl_today, cash
    )

    btc_holdings = 0.0
    last_buy_price = None
    last_trade_ts = time.time()


def main():
    logging.info("LIVE READY BOT STARTING")
    logging.info("Tracking %s", PRODUCT_ID)
    logging.info("LIVE_TRADING = %s", LIVE_TRADING)
    logging.info("Trade size: $%.2f", TRADE_SIZE_USD)
    logging.info("Daily loss limit: $%.2f", MAX_DAILY_LOSS_USD)
    logging.info("Max trades/day: %d", MAX_TRADES_PER_DAY)

    first_buy_done = False

    while True:
        try:
            reset_daily_counters_if_needed()
            price = get_price()

            if not first_buy_done and btc_holdings == 0.0:
                buy(price)
                first_buy_done = True

            elif btc_holdings > 0 and last_buy_price is not None:
                gain_pct = (price - last_buy_price) / last_buy_price
                loss_pct = (last_buy_price - price) / last_buy_price

                if gain_pct >= SELL_GAIN_PCT:
                    sell(price, "target hit")
                elif loss_pct >= STOP_LOSS_PCT:
                    sell(price, "stop loss")

            logging.info(
                "BTC Price: $%.2f | Cash: $%.2f | BTC: %.6f | Portfolio: $%.2f | Daily PnL: $%.2f",
                price, cash, btc_holdings, portfolio_value(price), realized_pnl_today
            )

        except Exception as e:
            logging.exception("Loop error: %s", e)

        time.sleep(10)


if __name__ == "__main__":
    main()