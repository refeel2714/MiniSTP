"""One full loop on the KIS paper account. Run on your own PC during market hours.

    python scripts/kis_order_check.py              # KODEX 200 (069500) 1주 시장가 매수
    python scripts/kis_order_check.py 005930 --stock

order -> fill sync -> ledger -> reconciliation against the broker balance.
Paper server only: it refuses to run against the live server.
"""
import argparse
import datetime as dt
import os
import sys
import time

from ministp.broker import KisClient, KisConfig, KisError
from ministp.cli import ask_missing, krx_calendar
from ministp.fees import FeeSchedule
from ministp.instruments import AssetType, Instrument, Market
from ministp.ledger import Ledger, Side
from ministp.oms import OrderStatus
from ministp.recon import Status
from ministp.session import TradingSession


def main(argv=None, client_factory=KisClient, now=dt.datetime.now, sleep=time.sleep,
         environ=os.environ) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?", default="069500")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--stock", action="store_true", help="일반 주식 (기본은 ETF)")
    ap.add_argument("--kosdaq", action="store_true")
    args = ap.parse_args(argv)

    try:
        cfg = KisConfig.from_env(ask_missing(environ))
    except KisError as e:
        print(f"설정 오류: {e}")
        return 2
    if cfg.env != "paper":
        print("이 스크립트는 모의투자 서버에서만 실행돼요.")
        return 2
    started = now()
    if not (dt.time(9, 0) <= started.time() <= dt.time(15, 20)) or started.weekday() >= 5:
        print("장중(평일 09:00~15:20)에 실행해 주세요.")
        return 2

    client = client_factory(cfg)
    today = started.date()
    try:
        bal = client.get_balance()
    except KisError as e:
        print(f"잔고 조회 실패: {e}")
        return 1
    if bal.positions or bal.cash_d0 != bal.cash_projected:
        print("보유 종목이나 미결제 거래가 없는 모의계좌에서 시작해야 원장 시작값을 맞출 수 있어요.")
        print(f"보유 {len(bal.positions)}종목, 예수금 D+0 {bal.cash_d0:,} / D+2 {bal.cash_projected:,}")
        return 2

    inst = Instrument(args.symbol, args.symbol, Market.KOSDAQ if args.kosdaq else Market.KOSPI,
                      AssetType.STOCK if args.stock else AssetType.ETF)
    ledger = Ledger(krx_calendar(), FeeSchedule.spec_v1(), {args.symbol: inst})
    ledger.deposit(today, bal.cash_d0)  # opening balance = broker cash
    session = TradingSession(ledger, client)

    print(f"{args.symbol} {args.qty}주 시장가 매수 주문...")
    cid = session.submit(today, args.symbol, Side.BUY, args.qty)
    if cid is None:
        print(f"주문 실패: {session.anomalies[-1].detail}")
        return 1
    for _ in range(30):
        sleep(2)
        session.sync(today)
        if session.oms.orders[cid].status is OrderStatus.FILLED:
            break
    else:
        print("60초 안에 체결되지 않았어요. 주문 상태:", session.oms.orders[cid].status)
        return 1

    r = ledger.execution_result(session.executions_of(cid)[0])
    print(f"체결: {r.gross // args.qty:,}원 x {args.qty}주 = {r.gross:,}원, "
          f"원장 수수료(0.015% 가정) {r.commission:,}원, 결제일 {r.settle_date}")

    sleep(1)
    results = session.reconcile(today)
    bal = client.get_balance()
    summary = bal.raw[-1]["output2"][0]
    print("증권사 예수금 필드:")
    for k in ("dnca_tot_amt", "nxdy_excc_amt", "prvs_rcdl_excc_amt"):
        print(f"  {k} = {summary.get(k)}")
    print("대사 결과:")
    for x in results:
        mark = "OK " if x.status is Status.MATCH else "BREAK"
        print(f"  {mark} {x.item:15} {x.key:10} 내부 {x.internal:>15,}  증권사 {x.broker:>15,}"
              + (f"  차이 {x.diff:,} ({x.cause})" if x.status is Status.BREAK else ""))
    for a in session.anomalies:
        print(f"  이상: {a.kind} {a.detail}")
    print("CASH_PROJECTED 차이가 수수료만큼이면 모의계좌 수수료율이 0.015%와 다르다는 뜻이에요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
