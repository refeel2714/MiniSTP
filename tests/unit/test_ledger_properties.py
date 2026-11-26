"""원장 불변식 무작위 시험 (상태 기계).

입금, 매수, 일부·전량 매도, 날짜 넘김(결제), 일 마감(평가 → 기준가)을 무작위 순서로
섞어 수천 가지 거래 흐름을 만들고, 매 단계가 끝날 때마다 원장이 지켜야 할 규칙을 확인해요.
규칙은 원장 코드를 다시 부르지 않고 시험이 따로 세는 장부(모형)와 맞춰 봐요.
실패하면 hypothesis가 가장 짧은 재현 순서로 줄여서 보여 줘요.
"""
import datetime as dt
import os
from collections import defaultdict
from decimal import Decimal

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, settings, strategies as st  # noqa: E402
from hypothesis.stateful import (RuleBasedStateMachine, initialize, invariant, precondition,  # noqa: E402
                                 rule)

from ministp.accounting import Account  # noqa: E402
from ministp.calendar import BusinessCalendar  # noqa: E402
from ministp.fees import FeeSchedule  # noqa: E402
from ministp.instruments import AssetType, Instrument, Market  # noqa: E402
from ministp.journal_templates import template_of  # noqa: E402
from ministp.ledger import Ledger, Side  # noqa: E402

# 연말 휴장일을 넣어 결제일이 휴장일을 건너뛰는 경우도 섞이게 해요.
HOLIDAYS = {dt.date(2026, 12, 25), dt.date(2026, 12, 31), dt.date(2027, 1, 1)}
CAL = BusinessCalendar(loaded_years={2026, 2027}, holidays=HOLIDAYS)
INSTRUMENTS = {
    "KP": Instrument("KP", "코스피 주식", Market.KOSPI, AssetType.STOCK),
    "KQ": Instrument("KQ", "코스닥 주식", Market.KOSDAQ, AssetType.STOCK),
    "ET": Instrument("ET", "ETF", Market.KOSPI, AssetType.ETF),
}
SYMBOLS = sorted(INSTRUMENTS)
START = dt.date(2026, 12, 16)

symbols = st.sampled_from(SYMBOLS)
prices = st.integers(min_value=1, max_value=300_000)
amounts = st.integers(min_value=1, max_value=50_000_000)


class LedgerMachine(RuleBasedStateMachine):
    """원장 하나와, 같은 거래를 따로 세는 모형."""

    @initialize(fees=st.sampled_from([FeeSchedule.spec_v1(), FeeSchedule.kis_paper()]),
                opening=st.lists(st.tuples(symbols, st.integers(1, 1_000), prices),
                                 max_size=3, unique_by=lambda t: t[0]),
                cash=st.integers(0, 100_000_000))
    def start(self, fees, opening, cash):
        self.day = START
        self.led = Ledger(CAL, fees, INSTRUMENTS)
        self.fees = fees
        # 모형
        self.deposits = 0
        self.contributed = 0
        self.cost_in = defaultdict(int)      # 종목별 들어간 원가 (기초 + 매수 체결금액)
        self.cost_out = defaultdict(int)     # 종목별 나간 원가 (매도원가)
        self.qty = defaultdict(int)
        self.unsettled: dict[str, tuple[Side, int, dt.date]] = {}
        self.settled_net = 0                 # 결제로 예수금에 더해진 금액 (매도 +, 매수 -)
        self.realized = self.commission = self.taxes = 0
        self.n = 0
        self.closed_today = False
        for sym, qty, price in opening:
            self.led.open_position(self.day, sym, qty, qty * price)
            self.cost_in[sym] += qty * price
            self.qty[sym] += qty
            self.contributed += qty * price
        if cash:
            self.led.deposit(self.day, cash)
            self.deposits += cash
            self.contributed += cash

    # ----- 규칙 -----

    @rule(amount=amounts)
    def deposit(self, amount):
        self.led.deposit(self.day, amount)
        self.deposits += amount
        self.contributed += amount

    @precondition(lambda self: not self.closed_today)
    @rule(data=st.data(), sym=symbols, price=prices)
    def buy(self, data, sym, price):
        affordable = self.led.projected_cash() // (price + price // 1000 + 1)   # 주문가능 안에서만
        if affordable < 1:
            return
        qty = data.draw(st.integers(1, min(2_000, affordable)), label="qty")
        gross = qty * price
        r = self._execute(sym, Side.BUY, qty, price)
        assert r.net == gross + r.commission
        assert r.tx_tax == r.rural_tax == 0
        self.cost_in[sym] += gross
        self.qty[sym] += qty

    @precondition(lambda self: not self.closed_today and any(self.qty.values()))
    @rule(data=st.data(), price=prices)
    def sell(self, data, price):
        sym = data.draw(st.sampled_from([s for s in SYMBOLS if self.qty[s]]))
        held = self.qty[sym]
        qty = data.draw(st.integers(1, held))
        before = self.led.position(sym)
        r = self._execute(sym, Side.SELL, qty, price)
        # R-32·R-33: 이동평균 원가, 전량 매도는 남은 원가 전부
        if qty == held:
            assert r.cost_of_sale == before.book_cost
        else:
            exact = Decimal(before.book_cost) * qty / held
            assert r.cost_of_sale <= exact < r.cost_of_sale + 1
        assert r.realized_pnl == r.gross - r.cost_of_sale
        assert r.net == r.gross - r.commission - r.tx_tax - r.rural_tax
        self.cost_out[sym] += r.cost_of_sale
        self.qty[sym] -= qty
        self.realized += r.realized_pnl

    @rule(days=st.integers(1, 4))
    def next_day(self, days):
        for _ in range(days):
            self.day = CAL.add_business_days(self.day, 1)
        settled = self.led.settle_due(self.day)
        assert self.led.settle_due(self.day) == []               # 같은 날 두 번 결제하지 않아요
        for eid in settled:
            side, net, settle_date = self.unsettled.pop(eid)
            assert settle_date <= self.day
            self.settled_net += net if side is Side.SELL else -net
        assert all(sd > self.day for _, _, sd in self.unsettled.values())
        self.closed_today = False

    @precondition(lambda self: not self.closed_today)
    @rule(data=st.data())
    def close_day(self, data):
        """일 마감: 보유 종목 종가로 평가(R-55)하고 기준가(R-60~63)를 내요."""
        held = self.led.positions()
        closes = {s: data.draw(prices, label=f"close {s}") for s in sorted(held)}
        self.led.revalue(self.day, closes)
        row = self.led.nav(self.day, closes)
        self.closed_today = True
        # 평가조정 = 시가 − 장부가 (R-55)
        mv = sum(p.qty * closes[s] for s, p in held.items())
        book = sum(p.book_cost for p in held.values())
        assert self.led.account_balance(Account.FVPL_VALUATION) == mv - book
        assert row.market_value == mv
        # 순자산(R-60) = 자본 계정 합계: 자산 − 부채 = 자본
        assert row.net_assets == self._equity()
        # 1,000좌당 기준가는 순자산 ÷ 좌수, 둘째 자리 반올림 (R-62)
        if row.units:
            assert abs(row.nav_per_1000 * row.units / 1000 - row.net_assets) <= Decimal(row.units) / 1000 * Decimal("0.005")

    # ----- 불변식 (매 단계) -----

    @invariant()
    def trial_balance_is_zero(self):
        dr = sum(ln.debit for e in self.led.journal for ln in e.lines)
        cr = sum(ln.credit for e in self.led.journal for ln in e.lines)
        assert dr == cr

    @invariant()
    def positions_match_the_model(self):
        for sym in SYMBOLS:
            p = self.led.position(sym)
            assert p.qty == self.qty[sym] >= 0
            # 원가 보존: 들어간 원가 = 나간 원가 + 남은 원가
            assert self.cost_in[sym] == self.cost_out[sym] + p.book_cost
            assert (p.book_cost > 0) == (p.qty > 0)
        assert self.led.account_balance(Account.FVPL_STOCK) == sum(
            self.led.position(s).book_cost for s in SYMBOLS)

    @invariant()
    def cash_and_settlement_accounts_match_the_model(self):
        assert self.led.cash_balance() == self.deposits + self.settled_net
        assert self.led.account_balance(Account.BUY_PAYABLE) == sum(
            net for side, net, _ in self.unsettled.values() if side is Side.BUY)
        assert self.led.account_balance(Account.SELL_RECEIVABLE) == sum(
            net for side, net, _ in self.unsettled.values() if side is Side.SELL)
        assert self.led.projected_cash() == self.led.cash_balance() + sum(
            net if side is Side.SELL else -net for side, net, _ in self.unsettled.values())
        assert {s.exec_id for s in self.led.pending_settlements()} == set(self.unsettled)

    @invariant()
    def profit_and_cost_accounts_match_the_model(self):
        acct = self.led.account_balance
        assert acct(Account.TRADING_GAIN) - acct(Account.TRADING_LOSS) == self.realized
        assert acct(Account.COMMISSION) == self.commission
        assert acct(Account.TAXES) == self.taxes
        assert acct(Account.CONTRIBUTED_CAPITAL) == self.contributed

    @invariant()
    def every_entry_fits_a_journal_template(self):
        # 분개 모형 표(STP-4500)가 원장이 실제로 만드는 전표를 빠짐없이 설명하는지
        assert all(template_of(e) is not None for e in self.led.journal)

    @invariant()
    def replay_rebuilds_the_same_ledger(self):
        again = Ledger.replay(self.led.events, CAL, self.fees, INSTRUMENTS)
        assert again.journal == self.led.journal
        assert again.positions() == self.led.positions()
        assert again.units == self.led.units
        assert again.pending_settlements() == self.led.pending_settlements()
        assert again.nav_history() == self.led.nav_history()

    # ----- 도우미 -----

    def _execute(self, sym, side, qty, price):
        self.n += 1
        r = self.led.execute(f"E{self.n}", self.day, sym, side, qty, price)
        # R-11: 결제일은 체결일 다음 둘째 영업일
        assert r.settle_date == CAL.add_business_days(self.day, 2)
        assert CAL.is_business_day(r.settle_date)
        c = self.fees.costs(INSTRUMENTS[sym], side is Side.SELL, qty * price)
        assert (r.commission, r.tx_tax, r.rural_tax) == (c.commission, c.tx_tax, c.rural_tax)
        assert r.commission % self.fees.commission_unit == 0
        self.unsettled[r.exec_id] = (side, r.net, r.settle_date)
        self.commission += r.commission
        self.taxes += r.tx_tax + r.rural_tax
        return r

    def _equity(self) -> int:
        a = self.led.account_balance
        return (a(Account.CONTRIBUTED_CAPITAL) + a(Account.TRADING_GAIN) + a(Account.VALUATION_GAIN)
                + a(Account.DIVIDEND_INCOME) - a(Account.TRADING_LOSS) - a(Account.VALUATION_LOSS)
                - a(Account.COMMISSION) - a(Account.TAXES) - a(Account.WITHHOLDING_TAX))


# 평소에는 150가지, 깊게 돌릴 때는 MINISTP_PROPERTY_EXAMPLES=5000처럼 늘려요.
EXAMPLES = int(os.environ.get("MINISTP_PROPERTY_EXAMPLES", "150"))
LedgerMachine.TestCase.settings = settings(
    max_examples=EXAMPLES, stateful_step_count=40, deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
TestLedgerInvariants = LedgerMachine.TestCase
