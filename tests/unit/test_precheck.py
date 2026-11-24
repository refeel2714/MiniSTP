"""묶음 검증 (R-98): 한 줄씩은 통과여도 같이 보내면 걸리는 경우를 잡아요."""
from dataclasses import replace
from decimal import Decimal

from ministp.basket import Portfolio, PriceBasis, Target, simulate
from ministp.instruments import AssetType, Instrument, Market
from ministp.ledger import Side
from ministp.precheck import AccountState, Proposed, check_set, issue_text
from ministp.risk import RiskLimits

D = Decimal
BUY, SELL = Side.BUY, Side.SELL
INST = {
    "069500": Instrument("069500", "KODEX 200", Market.KOSPI, AssetType.ETF),
    "005930": Instrument("005930", "삼성전자", Market.KOSPI, AssetType.STOCK),
    "000660": Instrument("000660", "SK하이닉스", Market.KOSPI, AssetType.STOCK),
}
PRICES = {"069500": 82_950, "005930": 58_100, "000660": 187_300}
# 총자산 20,000,000 = 예수금 20,000,000 (보유 없음)
CASH_ONLY = AccountState(INST, PRICES, {}, projected_cash=20_000_000)


def buy(symbol, qty, **kw):
    return Proposed(symbol, BUY, qty, PRICES[symbol], PRICES[symbol], **kw)


def sell(symbol, qty, **kw):
    return Proposed(symbol, SELL, qty, PRICES[symbol], PRICES[symbol], **kw)


def test_each_line_passes_alone_but_the_set_runs_out_of_cash():
    st = replace(CASH_ONLY, projected_cash=4_000_000)
    r = check_set(st, [buy("069500", 30), buy("005930", 30), buy("000660", 3)], weight_limit=D(100))
    assert [ln.alone for ln in r.lines] == [(), (), ()]
    assert [ln.together for ln in r.lines] == [(), ("INSUFFICIENT_CASH",), ("INSUFFICIENT_CASH",)]
    assert r.first_blocked == 1 and r.lines[1].breaks_set and not r.lines[0].breaks_set
    assert r.required_cash == int(2_488_500 * D("1.001")) + int(1_743_000 * D("1.001")) + int(561_900 * D("1.001"))
    assert r.issues == ("INSUFFICIENT_CASH",) and not r.ok


def test_open_buys_and_earlier_lines_count_toward_the_weight_limit():
    # 미체결 매수 5백만 원이 있으면 116만 원어치만 더 사도 30%를 넘어요 (6,162,000 / 20,000,000)
    st = replace(CASH_ONLY, reserved_buy=5_000_000, open_buy_value={"005930": 5_000_000})
    r = check_set(st, [buy("005930", 20)])
    assert r.lines[0].together == ("WEIGHT_LIMIT",) and r.lines[0].post_weight == D("30.81")
    assert check_set(CASH_ONLY, [buy("005930", 20)]).lines[0].post_weight == D("5.81")
    # 같은 종목을 두 줄에서 사면 둘째 줄에서 넘어요
    r = check_set(CASH_ONLY, [buy("005930", 60), buy("005930", 60)])
    assert [ln.post_weight for ln in r.lines] == [D("17.43"), D("34.86")]
    assert r.lines[1].alone == () and r.lines[1].together == ("WEIGHT_LIMIT",) and r.lines[1].breaks_set


def test_a_sell_that_reduces_an_over_limit_position_is_not_blocked():
    st = AccountState(INST, PRICES, {"069500": 243}, projected_cash=10_000_000)   # 비중 66.84%
    r = check_set(st, [sell("069500", 100)])
    assert r.ok and r.lines[0].post_weight == D("39.33")
    assert check_set(st, [buy("069500", 1)]).lines[0].together == ("WEIGHT_LIMIT",)


def test_sells_add_up_and_sell_proceeds_do_not_fund_buys():
    # 보유 96, 미체결 매도 46 → 매도가능 50
    st = AccountState(INST, PRICES, {"005930": 96}, projected_cash=1_000_000, open_sell_qty={"005930": 46})
    r = check_set(st, [sell("005930", 30), buy("005930", 10), sell("005930", 30)], weight_limit=D(100))
    assert [ln.together for ln in r.lines] == [(), (), ("SHORT_SELL",)]
    assert r.lines[2].alone == () and r.first_blocked == 2
    r = check_set(st, [sell("005930", 50), buy("069500", 20)], weight_limit=D(100))
    assert r.lines[1].together == ("INSUFFICIENT_CASH",)


def test_requests_already_waiting_for_approval_use_up_cash_first():
    st = replace(CASH_ONLY, projected_cash=1_000_000)
    r = check_set(st, [buy("069500", 10)], committed=[buy("069500", 10)], weight_limit=D(1000))
    assert r.available_cash == 1_000_000 - 829_500
    assert r.lines[0].alone == r.lines[0].together == ("INSUFFICIENT_CASH",) and not r.lines[0].breaks_set
    assert check_set(st, [buy("069500", 10)], weight_limit=D(1000)).ok


def test_kill_switch_and_account_status_block_the_whole_set():
    r = check_set(replace(CASH_ONLY, halted=True), [buy("005930", 1)])
    assert r.issues == ("KILL_SWITCH",) and r.lines[0].together == ("KILL_SWITCH",) and not r.ok
    r = check_set(replace(CASH_ONLY, active=False), [buy("005930", 1)])
    assert r.issues == ("ACCOUNT_NOT_OPEN",) and r.lines[0].together == () and not r.ok
    assert "정상 상태의 계좌" in issue_text(r.issues)


def test_one_blocked_line_blocks_its_whole_group():
    r = check_set(CASH_ONLY, [buy("069500", 1, group="BK-1"), buy("005930", 0, group="BK-1"),
                              buy("000660", 1, group="BK-2")])
    assert [ln.together for ln in r.lines] == [("GROUP_BLOCKED",), ("BAD_QTY",), ()]
    assert r.first_blocked == 0


def test_form_errors_come_from_the_same_order_check():
    r = check_set(CASH_ONLY, [Proposed("005930", BUY, 1, 58_150, 58_150), Proposed("999999", BUY, 1, 1_000),
                              Proposed("000660", BUY, 1, None)],
                  limits=RiskLimits(max_order_value=1_000_000))
    assert [ln.together for ln in r.lines] == [("BAD_TICK",), ("NOT_REGISTERED",), ("NO_PRICE",)]
    r = check_set(replace(CASH_ONLY, business_day=False), [buy("005930", 1)])
    assert r.lines[0].together == ("NOT_BUSINESS_DAY",)


# ---------- 바스켓 시뮬레이션(STP-1500)도 같은 검사를 써요 ----------

PF = Portfolio(instruments=INST, prices=PRICES, held_qty={"069500": 243, "005930": 96},
               sellable_qty={"069500": 243, "005930": 96}, available_cash=66_160_000, projected_cash=66_160_000)


def test_basket_counts_open_buys_toward_the_weight_limit():
    # 069500 비중 21.01%, 미체결 매수 5백만 원을 더하면 26.22%, 5백만 원어치를 더 사면 31.4%
    r = simulate(PF, [Target("069500", D(100))], 5_000_000)
    assert "WEIGHT_LIMIT" not in r.lines[0].issues
    pf = replace(PF, available_cash=61_160_000, open_buy_value={"069500": 5_000_000})
    r = simulate(pf, [Target("069500", D(100))], 5_000_000)
    assert r.lines[0].issues == ("WEIGHT_LIMIT",) and r.lines[0].post_weight > 30


def test_basket_sell_that_reduces_an_over_limit_position_passes():
    r = simulate(PF, [Target("069500", D(100))], 1_000_000, side=SELL, weight_limit=D(15))
    assert r.lines[0].issues == () and r.lines[0].post_weight > 15 and r.ok
    # 비중 0%로 둔 보유 종목(주문 없음)도 바스켓을 막지 않아요
    r = simulate(PF, [Target("069500", D(0)), Target("005930", D(100))], 1_000_000, weight_limit=D(15))
    assert r.lines[0].issues == () and r.ok


def test_basket_reads_the_same_numbers_as_the_set_check():
    r = simulate(PF, [Target("069500", D(50)), Target("005930", D(50))], 2_000_000, basis=PriceBasis.TICK1)
    st = PF.state()
    again = check_set(st, [Proposed(ln.symbol, BUY, ln.qty, ln.order_price, ln.limit_price) for ln in r.lines])
    assert [ln.together for ln in again.lines] == [ln.issues for ln in r.lines]
    assert again.required_cash == r.required_cash
