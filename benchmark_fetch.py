import time
from alpha_bot_execution import fetch_symphony_stats, ACCOUNT_UUIDS
import os

os.environ["ACCOUNT_UUIDS"] = "act1,act2,act3,act4,act5"
test_accounts = os.environ["ACCOUNT_UUIDS"].split(",")

def sequential_fetch():
    start_time = time.time()
    for account in test_accounts:
        _ = fetch_symphony_stats(account)
    end_time = time.time()
    return end_time - start_time

print(f"Sequential took: {sequential_fetch()} seconds")
