#-*-coding:utf-8 -*-
import pyupbit
import time
import pandas as pd
import numpy

import math

from cryptography.fernet import Fernet

'''

주식/코인 자동매매 FAQ
https://blog.naver.com/zacra/223203988739

그래도 안 된다면 구글링 해보시고
그래도 모르겠다면 클래스 댓글, 블로그 댓글, 단톡방( https://blog.naver.com/zacra/223111402375 )에 질문주세요! ^^

클래스 제작 후 전략의 많은 발전이 있었습니다.
백테스팅으로 검증해보고 실제로 제가 현재 돌리는 최신 전략을 완강 후 제 블로그에서 체크해 보셔요!
https://blog.naver.com/zacra

기다릴게요 ^^!

'''

#암호화 복호화 클래스
class SimpleEnDecrypt:
    def __init__(self, key=None):
        if key is None: # 키가 없다면
            key = Fernet.generate_key() # 키를 생성한다
        self.key = key
        self.f   = Fernet(self.key)
    
    def encrypt(self, data, is_out_string=True):
        if isinstance(data, bytes):
            ou = self.f.encrypt(data) # 바이트형태이면 바로 암호화
        else:
            ou = self.f.encrypt(data.encode('utf-8')) # 인코딩 후 암호화
        if is_out_string is True:
            return ou.decode('utf-8') # 출력이 문자열이면 디코딩 후 반환
        else:
            return ou
        
    def decrypt(self, data, is_out_string=True):
        if isinstance(data, bytes):
            ou = self.f.decrypt(data) # 바이트형태이면 바로 복호화
        else:
            ou = self.f.decrypt(data.encode('utf-8')) # 인코딩 후 복호화
        if is_out_string is True:
            return ou.decode('utf-8') # 출력이 문자열이면 디코딩 후 반환
        else:
            return ou


#RSI지표 수치를 구해준다. 첫번째: 분봉/일봉 정보, 두번째: 기간, 세번째: 기준 날짜
def GetRSI(ohlcv,period,st):
    delta = ohlcv["close"].diff()
    up, down = delta.copy(), delta.copy()
    up[up < 0] = 0
    down[down > 0] = 0
    _gain = up.ewm(com=(period - 1), min_periods=period).mean()
    _loss = down.abs().ewm(com=(period - 1), min_periods=period).mean()
    RS = _gain / _loss
    return float(pd.Series(100 - (100 / (1 + RS)), name="RSI").iloc[st])

#이동평균선 수치를 구해준다 첫번째: 분봉/일봉 정보, 두번째: 기간, 세번째: 기준 날짜
def GetMA(ohlcv,period,st):
    close = ohlcv["close"]
    ma = close.rolling(period).mean()
    return float(ma.iloc[st])

#볼린저 밴드를 구해준다 
#입력: 첫번째: 분봉/일봉 정보, 두번째: 기간, 세번째: 기준 날짜
#반환: 딕셔너리(ma: 중앙값, upper: 상단밴드, lower: 하단밴드)
#차트와 다소 오차가 있을 수 있습니다.
def GetBB(ohlcv,period,st):
    dic_bb = dict()

    ohlcv = ohlcv[::-1]
    ohlcv = ohlcv.shift(st + 1)
    close = ohlcv["close"].iloc[::-1]

    unit = 2.0
    bb_center=numpy.mean(close[len(close)-period:len(close)])
    band1=unit*numpy.std(close[len(close)-period:len(close)])

    dic_bb['ma'] = float(bb_center)
    dic_bb['upper'] = float(bb_center + band1)
    dic_bb['lower'] = float(bb_center - band1)

    return dic_bb


#일목 균형표의 각 데이타를 리턴한다 첫번째: 분봉/일봉 정보, 두번째: 기준 날짜
def GetIC(ohlcv,st):

    high_prices = ohlcv['high']
    close_prices = ohlcv['close']
    low_prices = ohlcv['low']


    nine_period_high =  ohlcv['high'].shift(-2-st).rolling(window=9).max()
    nine_period_low = ohlcv['low'].shift(-2-st).rolling(window=9).min()
    ohlcv['conversion'] = (nine_period_high + nine_period_low) /2
    
    period26_high = high_prices.shift(-2-st).rolling(window=26).max()
    period26_low = low_prices.shift(-2-st).rolling(window=26).min()
    ohlcv['base'] = (period26_high + period26_low) / 2
    
    ohlcv['sunhang_span_a'] = ((ohlcv['conversion'] + ohlcv['base']) / 2).shift(26)
    
    
    period52_high = high_prices.shift(-2-st).rolling(window=52).max()
    period52_low = low_prices.shift(-2-st).rolling(window=52).min()
    ohlcv['sunhang_span_b'] = ((period52_high + period52_low) / 2).shift(26)
    
    
    ohlcv['huhang_span'] = close_prices.shift(-26)


    nine_period_high_real =  ohlcv['high'].rolling(window=9).max()
    nine_period_low_real = ohlcv['low'].rolling(window=9).min()
    ohlcv['conversion'] = (nine_period_high_real + nine_period_low_real) /2
    
    period26_high_real = high_prices.rolling(window=26).max()
    period26_low_real = low_prices.rolling(window=26).min()
    ohlcv['base'] = (period26_high_real + period26_low_real) / 2
    


    
    dic_ic = dict()

    dic_ic['conversion'] = ohlcv['conversion'].iloc[st]
    dic_ic['base'] = ohlcv['base'].iloc[st]
    dic_ic['huhang_span'] = ohlcv['huhang_span'].iloc[-27]
    dic_ic['sunhang_span_a'] = ohlcv['sunhang_span_a'].iloc[-1]
    dic_ic['sunhang_span_b'] = ohlcv['sunhang_span_b'].iloc[-1]


  

    return dic_ic




#MACD의 12,26,9 각 데이타를 리턴한다 첫번째: 분봉/일봉 정보, 두번째: 기준 날짜
def GetMACD(ohlcv,st):
    macd_short, macd_long, macd_signal=12,26,9

    ohlcv["MACD_short"]=ohlcv["close"].ewm(span=macd_short).mean()
    ohlcv["MACD_long"]=ohlcv["close"].ewm(span=macd_long).mean()
    ohlcv["MACD"]=ohlcv["MACD_short"] - ohlcv["MACD_long"]
    ohlcv["MACD_signal"]=ohlcv["MACD"].ewm(span=macd_signal).mean() 

    dic_macd = dict()
    
    dic_macd['macd'] = ohlcv["MACD"].iloc[st]
    dic_macd['macd_siginal'] = ohlcv["MACD_signal"].iloc[st]
    dic_macd['ocl'] = dic_macd['macd'] - dic_macd['macd_siginal']

    return dic_macd




#스토캐스틱 %K %D 값을 구해준다 첫번째: 분봉/일봉 정보, 두번째: 기간, 세번째: 기준 날짜
def GetStoch(ohlcv,period,st):

    dic_stoch = dict()

    ndays_high = ohlcv['high'].rolling(window=period, min_periods=1).max()
    ndays_low = ohlcv['low'].rolling(window=period, min_periods=1).min()
    fast_k = (ohlcv['close'] - ndays_low)/(ndays_high - ndays_low)*100
    slow_d = fast_k.rolling(window=3, min_periods=1).mean()

    dic_stoch['fast_k'] = fast_k.iloc[st]
    dic_stoch['slow_d'] = slow_d.iloc[st]

    return dic_stoch





#거래대금이 많은 순으로 코인 리스트를 얻는다.
#입력: 첫번째: Interval기간(day,week,minute15 등), 두번째: 상위 몇 개까지 
#반환: 거래대금 상위 코인들의 티커 리스트
def GetTopCoinList(interval,top):
    print("--------------GetTopCoinList Start-------------------")
    Tickers = pyupbit.get_tickers("KRW")
    time.sleep(0.1)
    dic_coin_money = dict()

    for ticker in Tickers:
        print("--------------------------", ticker)
        try:
            time.sleep(0.1)
            df = pyupbit.get_ohlcv(ticker,interval)
            volume_money = (df['close'].iloc[-1] * df['volume'].iloc[-1]) + (df['close'].iloc[-2] * df['volume'].iloc[-2])
            #volume_money = float(df['value'].iloc[-1]) + float(df['value'].iloc[-2]) #거래대금!
            dic_coin_money[ticker] = volume_money
            print(ticker, dic_coin_money[ticker])
           # time.sleep(0.1)

        except Exception as e:
            print("---:",e)

    dic_sorted_coin_money = sorted(dic_coin_money.items(), key = lambda x : x[1], reverse= True)

    coin_list = list()
    cnt = 0
    for coin_data in dic_sorted_coin_money:
        cnt += 1
        if cnt <= top:
            coin_list.append(coin_data[0])
        else:
            break

    print("--------------GetTopCoinList End-------------------")

    return coin_list

#해당되는 리스트안에 해당 코인이 있는지 여부를 리턴하는 함수
def CheckCoinInList(CoinList,Ticker):
    InCoinOk = False
    for coinTicker in CoinList:
        if coinTicker == Ticker:
            InCoinOk = True
            break

    return InCoinOk

#티커에 해당하는 코인의 수익율을 구해서 리턴하는 함수.
def GetRevenueRate(balances,Ticker):
    revenue_rate = 0.0
    for value in balances:
        try:
            realTicker = value['unit_currency'] + "-" + value['currency']
            if Ticker == realTicker:
                time.sleep(0.05)
                nowPrice = pyupbit.get_current_price(realTicker)
                revenue_rate = (float(nowPrice)- float(value['avg_buy_price'])) * 100.0 / float(value['avg_buy_price'])
                break

        except Exception as e:
            # 에러 발생 시 조용히 처리
            pass

    return revenue_rate

#수익금과 수익률을 리턴해주는 함수 (수수료는 생각안함) myUpbit에 넣으셔서 사용하셔도 됨!
def GetRevenueMoneyAndRate(upbit,balances,Ticker):
    

    revenue_data = dict()

    revenue_data['revenue_money'] = 0
    revenue_data['revenue_rate'] = 0

    for value in balances:
        try:
            realTicker = value['unit_currency'] + "-" + value['currency']
            if Ticker == realTicker:
                
                nowPrice = pyupbit.get_current_price(realTicker)
                revenue_data['revenue_money'] = (float(nowPrice) - float(value['avg_buy_price'])) * upbit.get_balance(Ticker)
                revenue_data['revenue_rate'] = (float(nowPrice) - float(value['avg_buy_price'])) * 100.0 / float(value['avg_buy_price'])
                time.sleep(0.06)
                break

        except Exception as e:
            # 에러 발생 시 조용히 처리
            pass

    return revenue_data

#티커에 해당하는 코인의 총 매수금액을 리턴하는 함수
def GetCoinNowMoney(balances,Ticker):
    CoinMoney = 0.0
    for value in balances:
        realTicker = value['unit_currency'] + "-" + value['currency']
        if Ticker == realTicker:
            CoinMoney = float(value['avg_buy_price']) * (float(value['balance']) + float(value['locked']))
            break
    return CoinMoney

#티커에 해당하는 코인의 현재 평가 금액을 리턴하는 함수
def GetCoinNowRealMoney(balances,Ticker):
    CoinMoney = 0.0
    for value in balances:
        realTicker = value['unit_currency'] + "-" + value['currency']
        if Ticker == realTicker:
            time.sleep(0.1)
            nowPrice = pyupbit.get_current_price(realTicker)
            CoinMoney = float(nowPrice) * (float(value['balance']) + float(value['locked']))
            break
    return CoinMoney


#티커에 해당하는 코인이 매수된 상태면 참을 리턴하는함수
def IsHasCoin(balances,Ticker):
    HasCoin = False
    for value in balances:
        realTicker = value['unit_currency'] + "-" + value['currency']
        if Ticker == realTicker:
            HasCoin = True
    return HasCoin

#내가 매수한 (가지고 있는) 코인 개수를 리턴하는 함수
def GetHasCoinCnt(balances):
    CoinCnt = 0
    for value in balances:
        avg_buy_price = float(value['avg_buy_price'])
        if avg_buy_price != 0: #원화, 드랍받은 코인(평균매입단가가 0이다) 제외!
            CoinCnt += 1
    return CoinCnt



#티커에 해당하는 코인의 평균 매입단가를 리턴한다.
def GetAvgBuyPrice(balances, Ticker):
    avg_buy_price = 0
    for value in balances:
        realTicker = value['unit_currency'] + "-" + value['currency']
        if Ticker == realTicker:
            avg_buy_price = float(value['avg_buy_price'])
    return avg_buy_price
    
#총 원금을 구한다!
def GetTotalMoney(balances):
    total = 0.0
    for value in balances:
        try:
            ticker = value['currency']
            if ticker == "KRW": #원화일 때는 평균 매입 단가가 0이므로 구분해서 총 평가금액을 구한다.
                total += (float(value['balance']) + float(value['locked']))
            else:
                avg_buy_price = float(value['avg_buy_price'])
                if avg_buy_price != 0 and (float(value['balance']) != 0 or float(value['locked']) != 0):
                    total += (avg_buy_price * (float(value['balance']) + float(value['locked'])))
        except Exception as e:
            # 에러 발생 시 조용히 처리
            pass
    return total

#총 평가금액을 구한다!
def GetTotalRealMoney(balances):
    total = 0.0
    for value in balances:

        try:
            ticker = value['currency']
            if ticker == "KRW": #원화일 때는 평균 매입 단가가 0이므로 구분해서 총 평가금액을 구한다.
                total += (float(value['balance']) + float(value['locked']))
            else:
            
                avg_buy_price = float(value['avg_buy_price'])
                if avg_buy_price != 0 and (float(value['balance']) != 0 or float(value['locked']) != 0): #드랍받은 코인(평균매입단가가 0이다) 제외 하고 현재가격으로 평가금액을 구한다,.
                    realTicker = value['unit_currency'] + "-" + value['currency']
                    time.sleep(0.1)
                    nowPrice = pyupbit.get_current_price(realTicker)
                    total += (float(nowPrice) * (float(value['balance']) + float(value['locked'])))
        except Exception as e:
            # 에러 발생 시 조용히 처리
            pass


    return total



#시장가 매수한다. 2초뒤 잔고 데이타 리스트를 리턴한다.
def BuyCoinMarket(upbit,Ticker,Money):
    time.sleep(0.05)
    print(upbit.buy_market_order(Ticker,Money))

    time.sleep(2.0)
    #내가 가진 잔고 데이터를 다 가져온다.
    balances = upbit.get_balances()
    return balances

#시장가 매도한다. 2초뒤 잔고 데이타 리스트를 리턴한다.
def SellCoinMarket(upbit,Ticker,Volume):
    time.sleep(0.05)
    print(upbit.sell_market_order(Ticker,Volume))

    time.sleep(2.0)
    #내가 가진 잔고 데이터를 다 가져온다.
    balances = upbit.get_balances()
    return balances

#소량 정리 함수: 10000원 이하 보유 금액의 경우 10000원 매수 후 전량 매도하여 보유량을 0으로 만듦
def CleanupSmallAmount(upbit, Ticker):
    """
    10000원 이하의 소량 보유 코인을 정리하는 함수
    1. 10000원 시장가 매수
    2. 즉시 전량 시장가 매도
    3. 보유량이 정확히 0이 될 때까지 반복 시도
    """
    try:
        time.sleep(0.1)
        current_price = pyupbit.get_current_price(Ticker)
        if current_price is None or current_price <= 0:
            print(f"[소량정리] {Ticker}: 현재가 조회 실패")
            return False
            
        # 현재 보유량 확인
        balances = upbit.get_balances()
        coin_symbol = Ticker.replace('KRW-', '') if Ticker.startswith('KRW-') else Ticker
        
        current_qty = 0.0
        for b in balances:
            if b.get('currency') == coin_symbol:
                balance = float(b.get('balance', 0.0))
                locked = float(b.get('locked', 0.0))
                current_qty = balance + locked
                break
        
        if current_qty <= 0:
            print(f"[소량정리] {Ticker}: 보유량 없음")
            return True
            
        # 현재 보유 금액 계산
        current_value = current_qty * current_price
        print(f"[소량정리] {Ticker}: 현재 보유량 {current_qty:.8f}, 평가금액 {current_value:,.0f}원")
        
        # 10000원 이하인 경우에만 소량 정리 실행
        if current_value <= 10000:
            print(f"[소량정리] {Ticker}: 소량 정리 시작 (평가금액: {current_value:,.0f}원)")
            
            max_attempts = 3  # 최대 3회 시도
            attempt = 0
            
            while attempt < max_attempts:
                attempt += 1
                print(f"[소량정리] {Ticker}: 시도 {attempt}/{max_attempts}")
                
                # 현재 보유량 재확인
                balances = upbit.get_balances()
                current_qty = 0.0
                for b in balances:
                    if b.get('currency') == coin_symbol:
                        balance = float(b.get('balance', 0.0))
                        locked = float(b.get('locked', 0.0))
                        current_qty = balance + locked
                        break
                
                if current_qty <= 0:
                    print(f"[소량정리] {Ticker}: 보유량 0 달성!")
                    return True
                
                # 1단계: 10000원 시장가 매수
                try:
                    print(f"[소량정리] {Ticker}: 10000원 시장가 매수 시도")
                    buy_result = upbit.buy_market_order(Ticker, 10000)
                    print(f"[소량정리] {Ticker}: 매수 결과: {buy_result}")
                    time.sleep(2.0)
                except Exception as e:
                    print(f"[소량정리] {Ticker}: 10000원 매수 실패: {e}")
                    continue
                
                # 2단계: 전량 시장가 매도
                try:
                    # 매수 후 새로운 보유량 확인
                    balances = upbit.get_balances()
                    new_qty = 0.0
                    for b in balances:
                        if b.get('currency') == coin_symbol:
                            balance = float(b.get('balance', 0.0))
                            locked = float(b.get('locked', 0.0))
                            new_qty = balance + locked
                            break
                    
                    if new_qty > 0:
                        print(f"[소량정리] {Ticker}: 전량 매도 시도 (수량: {new_qty:.8f})")
                        sell_result = upbit.sell_market_order(Ticker, new_qty)
                        print(f"[소량정리] {Ticker}: 매도 결과: {sell_result}")
                        time.sleep(2.0)
                        
                        # 매도 후 최종 확인
                        balances = upbit.get_balances()
                        final_qty = 0.0
                        for b in balances:
                            if b.get('currency') == coin_symbol:
                                balance = float(b.get('balance', 0.0))
                                locked = float(b.get('locked', 0.0))
                                final_qty = balance + locked
                                break
                        
                        print(f"[소량정리] {Ticker}: 시도 {attempt} 완료 - 보유량: {final_qty:.8f}")
                        
                        # 보유량이 0이면 성공
                        if final_qty <= 0:
                            print(f"[소량정리] {Ticker}: 보유량 0 달성! 정리 완료")
                            return True
                        else:
                            print(f"[소량정리] {Ticker}: 아직 보유량 남음 ({final_qty:.8f}), 다음 시도")
                            
                    else:
                        print(f"[소량정리] {Ticker}: 매수 후 보유량 없음")
                        return True
                        
                except Exception as e:
                    print(f"[소량정리] {Ticker}: 전량 매도 실패: {e}")
                    continue
            
            # 최대 시도 횟수 초과
            print(f"[소량정리] {Ticker}: 최대 시도 횟수 초과, 정리 실패")
            return False
            
        else:
            print(f"[소량정리] {Ticker}: 소량 정리 불필요 (평가금액: {current_value:,.0f}원 > 10000원)")
            return True
            
    except Exception as e:
        print(f"[소량정리] {Ticker}: 소량 정리 오류: {e}")
        return False

#넘겨받은 가격과 수량으로 지정가 매수한다.
def BuyCoinLimit(upbit,Ticker,Price,Volume):
    time.sleep(0.05)
    print(upbit.buy_limit_order(Ticker,get_tick_size(Price),Volume))

#넘겨받은 가격과 수량으로 지정가 매도한다.
def SellCoinLimit(upbit,Ticker,Price,Volume):
    time.sleep(0.05)
    print(upbit.sell_limit_order(Ticker,get_tick_size(Price),Volume))

#해당 코인에 걸어진 매수매도주문 모두를 취소한다.
def CancelCoinOrder(upbit,Ticker):
    orders_data = upbit.get_order(Ticker)
    if len(orders_data) > 0:
        for order in orders_data:
            time.sleep(0.1)
            print(upbit.cancel_order(order['uuid']))

#거래대금 폭발 여부 첫번째: 캔들 정보, 두번째: 이전 5개의 평균 거래량보다 몇 배 이상 큰지
#이전 캔들이 그 이전 캔들 5개의 평균 거래금액보다 몇 배이상 크면 거래량 폭발로 인지하고 True를 리턴해줍니다
#현재 캔들[-1]은 막 시작했으므로 이전 캔들[-2]을 보는게 맞다!
def IsVolumePung(ohlcv,st):

    Result = False
    try:
        avg_volume = (float(ohlcv['volume'].iloc[-3]) + float(ohlcv['volume'].iloc[-4]) + float(ohlcv['volume'].iloc[-5]) + float(ohlcv['volume'].iloc[-6]) + float(ohlcv['volume'].iloc[-7])) / 5.0
        if avg_volume * st < float(ohlcv['volume'].iloc[-2]):
            Result = True
    except Exception as e:
        # 에러 발생 시 조용히 처리
        pass

    
    return Result



#틱사이즈 보정!! 업비트 새로운 호가 단위 규칙 적용 (2025년 변경)
def get_tick_size(price, method="floor"):

    if method == "floor":
        func = math.floor
    elif method == "round":
        func = round 
    else:
        func = math.ceil 

    if price >= 2000000:
        tick_size = func(price / 1000) * 1000
    elif price >= 1000000:
        tick_size = func(price / 1000) * 1000
    elif price >= 500000:
        tick_size = func(price / 500) * 500
    elif price >= 100000:
        tick_size = func(price / 100) * 100
    elif price >= 50000:
        tick_size = func(price / 50) * 50
    elif price >= 10000:
        tick_size = func(price / 10) * 10
    elif price >= 5000:
        tick_size = func(price / 5) * 5
    elif price >= 1000:
        tick_size = func(price / 1) * 1
    elif price >= 100:
        tick_size = func(price / 1) * 1
    elif price >= 10:
        tick_size = func(price / 0.1) / 10
    elif price >= 1:
        tick_size = func(price / 0.01) / 100
    elif price >= 0.1:
        tick_size = func(price / 0.001) / 1000
    elif price >= 0.01:
        tick_size = func(price / 0.0001) / 10000
    elif price >= 0.001:
        tick_size = func(price / 0.00001) / 100000
    elif price >= 0.0001:
        tick_size = func(price / 0.000001) / 1000000
    elif price >= 0.00001:
        tick_size = func(price / 0.0000001) / 10000000
    else:
        tick_size = func(price / 0.00000001) / 100000000


    return tick_size

def adjust_price_to_tick_size(price, currency):
    special_ticker_list = ['ADA', 'ALGO', 'BLUR', 'CELO', 'ELF', 'EOS', 'GRS', 'GRT', 'ICX', 'MANA', 'MINA', 'POL', 'SAND', 'SEI', 'STG', 'TRX']
    if currency in special_ticker_list:
        if price >= 2000000:
            return (price // 1000) * 1000  # 1000 단위로 내림
        elif price >= 1000000:
            return (price // 500) * 500  # 500 단위로 내림
        elif price >= 500000:
            return (price // 100) * 100  # 100 단위로 내림
        elif price >= 100000:
            return (price // 50) * 50  # 50 단위로 내림
        elif price >= 10000:
            return (price // 10) * 10  # 10 단위로 내림
        elif price >= 1000:
            return (price // 1) * 1  # 1 단위로 내림
        elif price >= 100:
            return (price // 1) * 1  # 100원 이상 1000원 미만인 경우 1원 단위
        elif price >= 10:
            return round(price, 2)  # 0.01 단위로 반올림
        elif price >= 1:
            return round(price, 3)  # 0.001 단위로 반올림
        elif price >= 0.1:
            return round(price, 4)  # 0.0001 단위로 반올림
        elif price >= 0.01:
            return round(price, 5)  # 0.00001 단위로 반올림
        elif price >= 0.001:
            return round(price, 6)  # 0.000001 단위로 반올림
        elif price >= 0.0001:
            return round(price, 7)  # 0.0000001 단위로 반올림
        else:
            return round(price, 8)  # 0.00000001 단위로 반올림
    else:
        if price >= 2000000:
            return (price // 1000) * 1000  # 1000 단위로 내림
        elif price >= 1000000:
            return (price // 500) * 500  # 500 단위로 내림
        elif price >= 500000:
            return (price // 100) * 100  # 100 단위로 내림
        elif price >= 100000:
            return (price // 50) * 50  # 50 단위로 내림
        elif price >= 10000:
            return (price // 10) * 10  # 10 단위로 내림
        elif price >= 1000:
            return (price // 1) * 1  # 1 단위로 내림
        elif price >= 100:
            return round(price, 1)  # 0.1 단위로 반올림
        elif price >= 10:
            return round(price, 2)  # 0.01 단위로 반올림
        elif price >= 1:
            return round(price, 3)  # 0.001 단위로 반올림
        elif price >= 0.1:
            return round(price, 4)  # 0.0001 단위로 반올림
        elif price >= 0.01:
            return round(price, 5)  # 0.00001 단위로 반올림
        elif price >= 0.001:
            return round(price, 6)  # 0.000001 단위로 반올림
        elif price >= 0.0001:
            return round(price, 7)  # 0.0000001 단위로 반올림
        else:
            return round(price, 8)  # 0.00000001 단위로 반올림  # 0.00000001 단위로 반올림  # 0.00000001 단위로 반올림  # 0.00000001 단위로 반올림  # 0.00000001 단위 반올림