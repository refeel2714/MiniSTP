"""운영 경로 무작위 시험: 주문 → 체결 → 원장 → DB → 일 마감·대사를 무작위로 섞어요.

모의 증권사(`SimBroker`)와 실제 운영 함수(`load_books`, `session_for`, `daily_close`)를 그대로 써서,
매수·매도·지정가·시장가 주문, 일부 체결, 시스템 취소, 증권사 쪽 취소(HTS), 체결 동기화,
프로그램 재시작, 시세 변동, 일 마감과 날짜 넘김을 무작위 순서로 섞어요. 확인하는 규칙:

- 동기화 뒤 장중 대사(수량, 매입금액, 예수금 D+0·결제 후)는 항상 일치한다.
- 같은 체결은 몇 번을 다시 동기화하거나 재시작해도 한 번만 원장에 들어간다.
- 주문 상태(OMS)가 증권사 누적 상태와 맞는다.
- 일 마감 대사는 무오류이고, 원장 내부 대사(`verify`)의 모든 항목이 맞는다.
- DB에서 다시 읽은 원장이 메모리 원장과 같다.
"""
import datetime as dt
import os
import queue

import pytest

hypothesis = pytest.importorskip("hypothesis")
psycopg = pytest.importorskip("psycopg")
from hypothesis import HealthCheck, event, settings, strategies as st  # noqa: E402
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule  # noqa: E402

from ministp.broker.sim import SimBroker  # noqa: E402
from ministp.calendar import BusinessCalendar  # noqa: E402
from ministp.fees import FeeSchedule  # noqa: E402
from ministp.instruments import AssetType, Instrument, Market  # noqa: E402
from ministp.ledger import ExecutionEvent, Side  # noqa: E402
from ministp.oms import OrderStatus  # noqa: E402
from ministp.ops import IntradayRunner, add_instrument, daily_close, load_books, open_books  # noqa: E402
from ministp.recon import Status  # noqa: E402
from ministp.store import PostgresLedgerStore  # noqa: E402
from ministp.store.integrity import integrity_report  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

CAL = BusinessCalendar(loaded_years={2026, 2027},
                       holidays={dt.date(2026, 12, 25), dt.date(2026, 12, 31), dt.date(2027, 1, 1)})
FEES = FeeSchedule.kis_paper()
INSTRUMENTS = {
    "KP": Instrument("KP", "코스피 주식", Market.KOSPI, AssetType.STOCK),
    "KQ": Instrument("KQ", "코스닥 주식", Market.KOSDAQ, AssetType.STOCK),
    "ET": Instrument("ET", "ETF", Market.KOSPI, AssetType.ETF),
}
SYMBOLS = sorted(INSTRUMENTS)
START = dt.date(2026, 12, 21)

# 500원 배수는 주식·ETF 어느 가격대에서도 호가단위에 맞아요 (20만 원 미만).
prices = st.integers(1, 399).map(lambda n: n * 500)


class OperationsMachine(RuleBasedStateMachine):

    @initialize(cash=st.integers(1_000_000, 300_000_000),
                start_prices=st.fixed_dictionaries({s: prices for s in SYMBOLS}))
    def start(self, cash, start_prices):
        self.conn = psycopg.connect(URL, autocommit=True)
        self.conn.execute("drop schema if exists public cascade; create schema public")
        PostgresLedgerStore(self.conn).init_schema()
        for inst in INSTRUMENTS.values():
            add_instrument(self.conn, inst)
        self.day = START
        self.broker = SimBroker(CAL, FEES, INSTRUMENTS, cash, start_prices, self.day)
        open_books(load_books(self.conn, CAL, FEES), self.broker, self.day)
        self.unsynced: set[str] = set()              # 마지막 조회 뒤 체결이 난 종목
        self.restart()

    def teardown(self):
        if getattr(self, "conn", None) is not None:
            self.conn.close()

    # ----- 도우미 -----

    def restart(self):
        """장중 실행(`run`)을 다시 띄운 것처럼: 원장을 DB에서 읽고 오늘 주문을 되살린 뒤 시작 조회를 해요."""
        self.books = load_books(self.conn, CAL, FEES)
        self.runner = IntradayRunner(self.books, self.broker, self.day, queue.Queue(), monotonic=lambda: 0.0,
                                     out=lambda line: None, settle_wait=lambda: None,
                                     now=lambda: dt.datetime.combine(self.day, dt.time(10)))
        self.session = self.runner.session
        self.runner.poll("시작 시 확인")
        self.unsynced.clear()

    def broker_orders(self):
        return {o.order_no: o for o in self.broker.daily_orders(self.day)}

    def live_orders(self):
        """증권사에서 아직 체결·취소할 잔량이 남은 오늘 주문."""
        return [o for o in self.broker_orders().values()
                if o.filled_qty + o.canceled_qty + o.rejected_qty < o.qty]

    def tracked(self, order_no):
        return self.session._by_broker_no.get(order_no)

    # ----- 규칙 -----

    @rule(sym=st.sampled_from(SYMBOLS), side=st.sampled_from([Side.BUY, Side.SELL]),
          qty=st.integers(1, 300), limit=st.booleans())
    def submit(self, sym, side, qty, limit):
        if side is Side.SELL:
            held = self.books.ledger.position(sym).qty
            if not held:
                return
            qty = min(qty, held)
        price = self.broker.prices[sym] if limit else None
        before = len(self.broker_orders())
        cid = self.session.submit(self.day, sym, side, qty, price)
        event(f"주문 {'거절' if cid is None else '접수'}")
        if cid is None:                                  # 주문 검증에서 거절: 증권사에 가지 않아요
            assert len(self.broker_orders()) == before
            assert self.session.anomalies[-1].kind == "SUBMIT_FAILED"
        else:
            assert self.session.oms.orders[cid].status is OrderStatus.SUBMITTED

    @precondition(lambda self: self.live_orders())
    @rule(data=st.data())
    def fill(self, data):
        o = data.draw(st.sampled_from(sorted(self.live_orders(), key=lambda o: o.order_no)), label="order")
        # 원장은 체결을 증권사의 주문별 누적 조회로 받아요(R-76). 그래서 한 조회 사이에 같은 종목의
        # 체결이 두 건 이상 쌓이면 체결별 순서와 가격을 잃어요: 같은 주문이면 한 건으로 합쳐져 10원
        # 절사 수수료가 달라지고(가격이 다르면 ODD_FILL_PRICE로 멈춤), 다른 주문이면 주문번호 순으로
        # 들어가 이동평균 매도원가가 달라져요. 아직 정하지 않은 설계 문제라(체결통보의 체결별 값으로
        # 반영할지), 여기서는 같은 종목에 반영 안 된 체결이 있으면 먼저 조회해요. 체결통보가 살아
        # 있는 평소 운영과 같은 조건이에요.
        if o.symbol in self.unsynced:
            self.sync()
            o = self.broker_orders()[o.order_no]
            if o.filled_qty + o.canceled_qty >= o.qty:
                return
        qty = data.draw(st.integers(1, o.qty - o.filled_qty), label="qty")
        if o.side == "SELL":
            # 증권사 잔고를 넘겨 팔 수는 없어요
            held = self.broker.positions.get(o.symbol, [0, 0])[0]
            if held < qty:
                return
        self.broker.fill(o.order_no, qty, data.draw(prices, label="price"))
        self.unsynced.add(o.symbol)

    @rule()
    def sync(self):
        before = {e.exec_id for e in self.books.ledger.events if isinstance(e, ExecutionEvent)}
        booked = self.runner.poll()
        assert not set(booked) & before
        if booked:
            event("체결 반영")
        assert self.session.sync(self.day) == []          # 다시 동기화해도 새로 들어가는 체결은 없어요
        self.unsynced.clear()
        results = self.session.reconcile(self.day)
        assert all(r.status is Status.MATCH for r in results), [r for r in results if r.status is not Status.MATCH]
        for b in self.broker_orders().values():
            t = self.tracked(b.order_no)
            assert t is not None, f"MiniSTP가 모르는 주문 {b.order_no}"
            o = self.session.oms.orders[t.client_id]
            assert o.filled_qty == b.filled_qty
            if b.filled_qty == b.qty:
                assert o.status is OrderStatus.FILLED
            elif b.canceled_qty and b.filled_qty + b.canceled_qty == b.qty:
                assert o.status is OrderStatus.CANCELED

    @precondition(lambda self: self.live_orders())
    @rule(data=st.data(), from_hts=st.booleans())
    def cancel(self, data, from_hts):
        o = data.draw(st.sampled_from(sorted(self.live_orders(), key=lambda o: o.order_no)), label="order")
        t = self.tracked(o.order_no)
        if from_hts:
            self.broker.cancel_order(t.ack)               # 증권사 화면(HTS)에서 취소
        else:
            self.runner.cancel(t.client_id)          # R-45: 체결 먼저 반영, 증권사 요청, 바로 확인 조회
            status = self.session.oms.orders[t.client_id].status
            assert status in (OrderStatus.CANCELED, OrderStatus.FILLED), status
            self.unsynced.clear()

    @rule()
    def restart_process(self):
        self.restart()

    @rule(sym=st.sampled_from(SYMBOLS), price=prices)
    def move_price(self, sym, price):
        self.broker.prices[sym] = price

    @rule(days=st.integers(1, 3))
    def close_and_next_day(self, days):
        report = daily_close(self.books, self.broker, self.day)
        event(f"일 마감 (결제 {'있음' if report.settled else '없음'}, 마감 때 반영 {'있음' if report.booked else '없음'})")
        assert report.clean, [r for r in report.results if r.status is not Status.MATCH]
        assert not [a for a in report.anomalies if a.kind != "SUBMIT_FAILED"], report.anomalies
        checks = integrity_report(self.conn, CAL, FEES, INSTRUMENTS)
        assert all(c.ok for c in checks), [c for c in checks if not c.ok]
        for _ in range(days):
            self.day = CAL.add_business_days(self.day, 1)
        self.broker.set_day(self.day)
        self.restart()                                    # 결제일이면 시작할 때 결제를 반영해요

    # ----- 불변식 -----

    @invariant()
    def stored_ledger_equals_memory(self):
        if not hasattr(self, "books"):
            return
        again = PostgresLedgerStore(self.conn).load(CAL, FEES, INSTRUMENTS)
        assert again.events == self.books.ledger.events
        assert again.journal == self.books.ledger.journal
        assert again.positions() == self.books.ledger.positions()

    @invariant()
    def ledger_never_ahead_of_broker(self):
        """원장은 증권사가 알려 준 체결만 담아요: 주문마다 반영 수량 ≤ 증권사 누적 체결."""
        if not hasattr(self, "books"):
            return
        for b in self.broker_orders().values():
            t = self.tracked(b.order_no)
            if t is not None:
                assert t.booked_qty <= b.filled_qty


EXAMPLES = int(os.environ.get("MINISTP_PROPERTY_EXAMPLES", "25"))
OperationsMachine.TestCase.settings = settings(
    max_examples=EXAMPLES, stateful_step_count=30, deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
TestOperationsInvariants = OperationsMachine.TestCase
