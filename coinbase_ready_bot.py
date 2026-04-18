import time
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

PRODUCT_ID = "BTC-USD"

def get_price():
    url = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    return float(data["price"])

def main():
    logging.info("Bot started")
    logging.info("Tracking %s", PRODUCT_ID)

    while True:
        try:
            price = get_price()
            logging.info("BTC Price: $%.2f", price)
        except Exception as e:
            logging.exception("Loop error: %s", e)

        time.sleep(10)

if __name__ == "__main__":
    main()