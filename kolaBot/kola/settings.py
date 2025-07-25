# -*- coding: utf-8 -*-
API_REST_INTERVAL = 5
API_ERROR_INTERVAL = 10
HTTP_SIMPLE_RATE_LIMITE = 1.5
HTTP_BULK_RATE_LIMITE = 0.30
TIMEOUT = 12
SYMBOL = "XBTUSD"
ORDERID_PREFIX = "mlk_"

LIVE = False
POST_ONLY = False

URL = "https://www.bitmex.com/api/v1/"
LIVE_URL = "https://www.bitmex.com/api/v1/"
TEST_URL = "https://testnet.bitmex.com/api/v1/"

# Binance endpoints
BINANCE_URL = "https://api.binance.com/api"
BINANCE_TEST_URL = "https://testnet.binance.vision/api"

# Binance API credentials read from environment
import os

BINANCE_API_KEY = os.getenv("BINANCE_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_SECRET", "")
BINANCE_TEST_API_KEY = os.getenv("BINANCE_TEST_KEY", "")
BINANCE_TEST_API_SECRET = os.getenv("BINANCE_TEST_SECRET", "")

# constante
XBTSATOSHI = 10 ** -8

# for portfolio calculation
CONTRACTS = ["XBTUSD"]

# definie if the run parse commande line or takes arguments from setting file
PARSE_COMMANDE_LINE = True

# LOGS
LOGLEVELS = {
    "CRITICAL": 50,
    "ERROR": 40,
    "WARNING": 30,
    "INFO": 20,
    "DEBUG": 10,
    "NOTSET": 0,
    None: 0,
}
MAINLOGLEVEL = "INFO"

# fmt = '%(asctime)s-%(levelname)s-%(filename)s@%(lineno)s(%(threadName)s): %(message)s'
# LOGFMT = '%(threadName)s~%(levelno)s /%(filename)s@%(lineno)s@%(funcName)s/ %(message)s'
# LOGFMT = '%(asctime)s (%(threadName)s~%(name)s~%(levelno)s) /%(filename)s@%(lineno)s@%(funcName)s/ %(message)s'
LOGFMT = "%(asctime)s %(threadName)s~%(levelno)s /%(filename)s@%(lineno)s@%(funcName)s/ %(message)s"

LOGNAME = "kola"

ordStatusTrans = {
    "N": "New",
    "C": "Canceled",
    "F": "Filled",
    "P": "PartiallyFilled",
    "T": "Triggered",
}
