from polygon import RESTClient
from local_settings import polygon as settings

#from datetime import date
from datetime import date, datetime
from typing import Any, Optional
import pandas as pd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json

markets = ['crypto', 'stocks', 'fx']


class MyRESTClient(RESTClient):
    def __init__(self, auth_key: str=settings['api_key'], timeout:int=5):
        super().__init__(auth_key)
        retry_strategy = Retry(total=10,
                               backoff_factor=10,
                               status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry_strategy)
        #self._session.mount('https://', adapter)

    def get_tickers(self, market:str=None) -> pd.DataFrame:
        print('Right Here!')
        if not market in markets:
            raise Exception(f'Market must be one of {markets}.')

        resp = self.reference_tickers_v3(market=market)
        if hasattr(resp, 'results'):
            df = pd.DataFrame(resp.results)

            while hasattr(resp, 'next_url'):
                resp = self.reference_tickers_v3(next_url=resp.next_url)
                df = df.append(pd.DataFrame(resp.results))

            if market == 'crypto':
                # Only use USD pairings.</em>
                df = df[df['currency_symbol'] == 'USD']
                df['name'] = df['base_currency_name']
                df = df[['ticker', 'name', 'market', 'active']]

            df = df.drop_duplicates(subset='ticker')
            return df
        return None
    
    

    def get_bars(self, market:str=None, ticker:str=None, multiplier:int=1,
                     timespan:str='minute', from_:date=None, to:date=None) -> pd.DataFrame:
    
            if not market in markets:
                raise Exception(f'Market must be one of {markets}.')
    
            if ticker is None:
                raise Exception('Ticker must not be None.')
    
            from_ = from_ if from_ else date(2000,1,1)
            to = to if to else date.today()
            
            
            
            resp = self.get_aggs(ticker, multiplier, timespan,
                                              from_.strftime('%Y-%m-%d'), to.strftime('%Y-%m-%d'),
                                              limit=50000, raw = True)

            # if resp:
            #     print(f"Daily aggregate data for {ticker} from {from_} to {to}:")
            #     print(resp)
            #     for agg in resp.data:
            #         print(agg)
            # else:
            #     print("No data found for the specified date range.")
            # print("After get_aggs")

            json_response = json.loads(resp.data)
            print(pd.DataFrame(json_response['results']))

            
            if market == 'stocks':
               
                
                last_minute = 0
                while json_response['results'][-1]['t'] > last_minute:
                    last_minute = json_response['results'][-1]['t'] # Last minute in response
                    last_minute_date = datetime.fromtimestamp(last_minute/1000).strftime('%Y-%m-%d')
                    resp = self.get_aggs(ticker, multiplier, timespan,
                                              last_minute_date, to.strftime('%Y-%m-%d'),
                                              limit=50000, raw=True)
                    new_bars = pd.DataFrame(json.loads(resp.data))
                    print(new_bars)
                    # print(new_bars[new_bars['results']['t']])
                    # df = df.append(new_bars[new_bars['results']['t'] > last_minute])
                print("Did we make it here?")
                df['date'] = pd.to_datetime(df['t'], unit='ms')
                print("How about here?")
                df = df.rename(columns={'o':'open',
                                        'h':'high',
                                        'l':'low',
                                        'c':'close',
                                        'v':'volume',
                                        'vw':'vwap',
                                        'n':'transactions'})
                df = df[['date','open','high','low','close','volume']]
    
                return df
            return None
        




def main():
    start = datetime(2022,7,1)
    pd.set_option('display.max_columns', None)
    client = MyRESTClient()
    #df = client.get_bars(market='crypto', ticker='X:BTCUSD', from_=start)
    df = client.get_bars(market='stocks', ticker='SPY',multiplier=30,timespan='minute', from_=start)
    print("Here2")
    print(df)
    print("Here3")


if __name__ == "__main__":
    main()