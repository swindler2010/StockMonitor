"""Display range is independent from candle interval; never fabricate missing bars."""
import pandas as pd

DAYS={'день':1,'неделя':7,'Последние':7}
MONTHS={'месяц':1,'год':12,'1 месяц':1,'6 месяцев':6,'1 год':12,'2 года':24,'5 лет':60}

def visible_frame(frame,period):
    if frame.empty:return frame
    if period=='Max':return frame
    if period in DAYS:return frame.loc[frame.index>=frame.index[-1]-pd.Timedelta(days=DAYS[period])]
    if period not in MONTHS:return frame.tail(100)
    start=frame.index[-1]-pd.DateOffset(months=MONTHS[period])
    return frame.loc[frame.index>=start]

