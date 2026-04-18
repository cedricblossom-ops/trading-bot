import time
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

PRODUCT_ID = "BTC-USD"

STARTING_CASH = 1000.0
TRADE_SIZE_USD = 100.0

SELL_GAIN_PCT = 0.0005
STOP_LOSS_PCT = 0.0005

cash = STARTING_CASH
btc_holdings = 0.0
last_buy_price = None
trade_count = 0


def get_price():
    url = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    return float(data["price"])


def portfolio_value(current_price):
    return cash + (btc_holdings * current_price)


def buy(current_price):
    global cash, btc_holdings, last_buy_price, trade_count

    if cash < TRADE_SIZE_USD:
        logging.info("SIM BUY SKIPPED | Not enough cash | Cash: $%.2f", cash)
        return

    btc_bought = TRADE_SIZE_USD / current_price
    cash -= TRADE_SIZE_USD
    btc_holdings += btc_bought
    last_buy_price = current_price
    trade_count += 1

    logging.info(
        "SIM BUY | Price: $%.2f | BTC bought: %.6f | Cash left: $%.2f | BTC holdings: %.6f | Trades: %d",
        current_price, btc_bought, cash, btc_holdings, trade_count
    )


def sell(current_price, reason):
    global cash, btc_holdings, last_buy_price

    if btc_holdings <= 0:
        return

    usd_received = btc_holdings * current_price
    pnl = 0.0
    if last_buy_price is not None:
        pnl = (current_price - last_buy_price) * btc_holdings

    cash += usd_received

    logging.info(
        "SIM SELL | Reason: %s | Price: $%.2f | USD received: $%.2f | PnL: $%.2f | Cash: $%.2f",
        reason, current_price, usd_received, pnl, cash
    )

    btc_holdings = 0.0
    last_buy_price = None


def main():
    logging.info("NEW CLEAN VERSION RUNNING")
    logging.info("Bot started")
    logging.info("Tracking %s", PRODUCT_ID)
    logging.info("Simulation mode ON")
    logging.info("Starting cash: $%.2f", cash)

    first_buy_done = False

    while True:
        try:
            price = get_price()

            if not first_buy_done and btc_holdings == 0.0:
                logging.info("FORCING FIRST BUY")
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
                "BTC Price: $%.2f | Cash: $%.2f | BTC: %.6f | Portfolio: $%.2f",
                price, cash, btc_holdings, portfolio_value(price)
            )

        except Exception as e:
            logging.exception("Loop error: %s", e)

        time.sleep(10)


if __name__ == "__main__":
    main()