import time
import concurrent.futures
from alpha_bot_execution import fetch_symphony_stats, ACCOUNT_UUIDS
import os

os.environ["ACCOUNT_UUIDS"] = "act1,act2,act3,act4,act5"
test_accounts = os.environ["ACCOUNT_UUIDS"].split(",")

def concurrent_fetch():
    start_time = time.time()
    symphony_data_cache = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(test_accounts)) as executor:
        future_to_account = {executor.submit(fetch_symphony_stats, account): account for account in test_accounts}
        for future in concurrent.futures.as_completed(future_to_account):
            account = future_to_account[future]
            try:
                symphony_data_cache[account] = future.result()
            except Exception as exc:
                print(f'{account} generated an exception: {exc}')
                symphony_data_cache[account] = []
    end_time = time.time()
    return end_time - start_time

print(f"Concurrent took: {concurrent_fetch()} seconds")
