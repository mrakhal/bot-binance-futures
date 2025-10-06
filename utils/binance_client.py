from binance.client import Client
from dotenv import load_dotenv
import os
load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TESTNET = os.getenv("TESTNET", "true").lower() in ("1","true","yes")

FUTURES_TESTNET_BASE = "https://testnet.binancefuture.com"

def create_client():
    client = Client(API_KEY, API_SECRET)
    if TESTNET:
        client.FUTURES_URL = FUTURES_TESTNET_BASE
    return client
