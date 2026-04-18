import time
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logging.info("NEW VERSION update bot logic")
PRODUCT_ID = "BTC-USD"

STARTING_CASH = 1000.0
TRADE_SIZE_USD = 100.0

BUY_DROP_PCT = 0.0003
SELL_GAIN_PCT = 0.0005
STOP_LOSS_PCT = 0.0005

cash = STARTING_CASH
btc_holdings = 0.0
last_buy_price = None
reference_price = None


def get_price():
    url = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    return float(data["price"])


def portfolio_value(current_price):
    return cash + (btc_holdings * current_price)


def buy(current_price):
    global cash, btc_holdings, last_buy_price

    if cash < TRADE_SIZE_USD:
        logging.info("Not enough cash to buy. Cash: $%.2f", cash)
        return

    btc_bought = TRADE_SIZE_USD / current_price
    cash -= TRADE_SIZE_USD
    btc_holdings += btc_bought
    last_buy_price = current_price

    logging.info(
        "SIM BUY | Price: $%.2f | Bought: %.6f BTC | Cash left: $%.2f | BTC holdings: %.6f",
        current_price, btc_bought, cash, btc_holdings
    )


def sell(current_price, reason):
    global cash, btc_holdings, last_buy_price

if btc_holdings == 0:

    # FORCE FIRST BUY (for testing)
    if last_buy_price is None:
        buy(price)
        reference_price = price
    else:
        drop_from_reference = (reference_price - price) / reference_price

        if drop_from_reference >= BUY_DROP_PCT:
            buy(price)
            reference_price = price
        else:
            if price > reference_price:
                reference_price = price
        return

    usd_received = btc_holdings * current_price
    pnl = 0.0
    if last_buy_price is not None:
        pnl = (current_price - last_buy_price) * btc_holdings

    logging.info(
        "SIM SELL | Reason: %s | Price: $%.2f | Sold: %.6f BTC | USD received: $%.2f | Trade PnL: $%.2f",
        reason, current_price, btc_holdings, usd_received, pnl
    )

    cash += usd_received
    btc_holdings = 0.0
    last_buy_price = None


def main():
    global reference_price

    logging.info("Bot started")
    logging.info("Tracking %s", PRODUCT_ID)
    logging.info("Simulation mode ON")
    logging.info("Starting cash: $%.2f", cash)

    while True:
        try:
            price = get_price()

            if reference_price is None:
                reference_price = price

            logging.info(
                "BTC Price: $%.2f | Cash: $%.2f | BTC: %.6f | Portfolio: $%.2f",
                price, cash, btc_holdings, portfolio_value(price)
            )

            if btc_holdings == 0:
                drop_from_reference = (reference_price - price) / reference_price

                if drop_from_reference >= BUY_DROP_PCT:
                    buy(price)
                    reference_price = price
                else:
                    if price > reference_price:
                        reference_price = price

            else:
                gain_from_buy = (price - last_buy_price) / last_buy_price
                loss_from_buy = (last_buy_price - price) / last_buy_price

                if gain_from_buy >= SELL_GAIN_PCT:
                    sell(price, "target hit")
                    reference_price = price

                elif loss_from_buy >= STOP_LOSS_PCT:
                    sell(price, "stop loss")
                    reference_price = price

            time.sleep(10)

        except Exception as e:
            logging.exception("Loop error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    main()