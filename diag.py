import sqlite3,time,sys
c=sqlite3.connect(sys.argv[1]);t=time.time()-4*3600;f=lambda x:time.strftime('%H:%M',time.localtime(x))
print('PAPER BETS last 4h:')
for r in c.execute("select placed_at,strategy,coin,side,price from paper_orders where placed_at>? order by placed_at",(t,)):print(' ',f(r[0]),*r[1:])
print('BIG GAPS (model vs price, last 2 min, last 4h):')
for r in c.execute("select ts,coin,t_left,p_up,up_ask,down_ask from closer_signals where ts>? and t_left<120 and (p_up-up_ask>0.03 or (1-p_up)-down_ask>0.03) order by ts desc limit 15",(t,)):print(' ',f(r[0]),r[1],int(r[2]),'model up %.2f'%r[3],'ask up',r[4],'down',r[5])
print('LIVE:',dict(c.execute("select k,v from live_state where k in ('armed','bots')").fetchall()))
