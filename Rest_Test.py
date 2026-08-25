from polygon import RESTClient
from typing import cast
from urllib3 import HTTPResponse
from local_settings import polygon as settings
import json


from datetime import date, datetime
from typing import Any, Optional
import pandas as pd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

client_original = RESTClient(settings['api_key'])


class MyRESTClient(RESTClient):
    def __init__(self, auth_key: str=settings['api_key'], timeout:int=5):
        super().__init__(auth_key)
        retry_strategy = Retry(total=10, backoff_factor=10,status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry_strategy)
        #self._session.mount('https://', adapter)



# raw attribute is very important as if not included data is returned in list format








print("Here 2")
client = MyRESTClient()




resp = client.get_aggs(
        "SPY",
        5,
        "minute",
        "2025-09-15",
        "2025-09-16",
        raw=True)

print("Here 3")
print(resp.data)


json_response = json.loads(resp.data)
print(json_response['results'])
print("Here 4")
df = pd.DataFrame(json_response['results'])
df['date'] = pd.to_datetime(df['t'], unit='ms').dt.tz_localize('UTC')
df['date'] = df['date'].dt.tz_convert('America/Los_Angeles')
df = df.rename(columns={'o':'open',
                        'h':'high',
                        'l':'low',
                        'c':'close',
                        'v':'volume',
                        'vw':'vwap',
                        'n':'transactions'})
df = df[['date','open','high','low','close','volume','vwap']]
print(df)
df.to_csv('SPX30MTEST.csv', sep='\t', encoding='utf-8')