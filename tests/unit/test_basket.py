"""바스켓 주문 시뮬레이션 (R-97): 화면 그림(STP-1500)의 숫자를 그대로 재현해요."""
from decimal import Decimal

import pytest

from ministp.basket import (BasketError, Portfolio, PriceBasis, Target, current_weights, default_targets,
                            read_targets, simulate, write_csv)
from ministp.fees import FeeSchedule
from ministp.instruments import AssetType, Instrument, Market
from ministp.ledger import Side

D = Decimal
INST = {
    "069500": Instrument("069500", "KODEX 200", Market.KOSPI, AssetType.ETF),
    "360750": Instrument("360750", "TIGER 미국S&P500", Market.KOSPI, AssetType.ETF),
    "005930": Instrument("005930", "삼성전자", Market.KOSPI, AssetType.STOCK),
    "000660": Instrument("000660", "SK하이닉스", Market.KOSPI, AssetType.STOCK),
}
PF = Portfolio(instruments=INST,
               prices={"069500": 82_950, "360750": 22_840, "005930": 58_100, "000660": 187_300},
               held_qty={"069500": 243, "360750": 355, "005930": 96},
               sellable_qty={"069500": 243, "360750": 355, "005930": 50},
               available_cash=66_160_000, projected_cash=66_160_000)
FEES = FeeSchedule.kis_paper()


def test_current_weights_sum_to_exactly_100():
    w = current_weights(PF)
    assert w == {"069500": D("59.56"), "360750": D("23.96"), "005930": D("16.48")}
    assert sum(w.values()) == D("100.00")
    assert [t.symbol for t in default_targets(PF)] == ["069500", "360750", "005930"]


def test_picture_example_buy():
    targets = [Target("069500", D("49.56")), Target("360750", D("23.96")),
               Target("005930", D("16.48")), Target("000660", D("10"))]
    r = simulate(PF, targets, 10_000_000, fees=FEES)
    got = [(ln.symbol, ln.target_amount, ln.qty, ln.amount, ln.commission) for ln in r.lines]
    assert got == [("069500", 4_956_000, 59, 4_894_050, 730), ("360750", 2_396_000, 104, 2_375_360, 350),
                   ("005930", 1_648_000, 28, 1_626_800, 240), ("000660", 1_000_000, 5, 936_500, 140)]
    assert (r.allocated, r.leftover, r.commission) == (9_832_710, 167_290, 1_460)
    assert r.lines[0].actual_weight == D("48.94") and r.lines[0].weight_gap == D("-0.62")
    assert r.lines[3].current_weight == 0 and r.lines[3].post_weight == D("0.94")
    assert r.lines[0].post_weight == D("25.05")
    assert r.lines[0].limit_price == 82_950 and r.lines[0].tick == 5
    assert r.required_cash == sum(int(ln.amount * D("1.001")) for ln in r.lines)
    assert r.ok


def test_weights_must_sum_to_100():
    with pytest.raises(BasketError, match="100%"):
        simulate(PF, [Target("069500", D("50"))], 1_000_000)
    with pytest.raises(BasketError, match="두 번"):
        simulate(PF, [Target("069500", D("50")), Target("069500", D("50"))], 1_000_000)
    with pytest.raises(BasketError, match="1원"):
        simulate(PF, [Target("069500", D("100"))], 0)


def test_price_basis_tick_and_market():
    r = simulate(PF, [Target("005930", D("100"))], 1_000_000, basis=PriceBasis.TICK1)
    assert r.lines[0].limit_price == 58_200
    r = simulate(PF, [Target("005930", D("100"))], 1_000_000, side=Side.SELL, basis=PriceBasis.TICK1)
    assert r.lines[0].limit_price == 58_000
    r = simulate(PF, [Target("005930", D("100"))], 1_000_000, basis=PriceBasis.MARKET)
    assert r.lines[0].limit_price is None and r.lines[0].order_price == 58_100


def test_checks_block_or_warn():
    # 한도: 지금 25%인 KODEX 200을 1천만 원 더 사면 30%를 넘어요
    r = simulate(PF, [Target("069500", D("100"))], 10_000_000)
    assert "WEIGHT_LIMIT" in r.lines[0].issues and not r.ok
    # 1주도 못 사면 경고만
    r = simulate(PF, [Target("069500", D("99.9")), Target("000660", D("0.1"))], 1_000_000)
    assert r.lines[1].issues == ("ZERO_QTY",) and r.ok
    # 주문가능금액
    r = simulate(PF, [Target("360750", D("100"))], 70_000_000, weight_limit=D(100))
    assert r.issues == ("INSUFFICIENT_CASH",) and not r.ok
    # 매도가능수량
    r = simulate(PF, [Target("005930", D("100"))], 5_000_000, side=Side.SELL)
    assert "SHORT_SELL" in r.lines[0].issues
    # 가격 없는 종목, 등록 안 된 종목
    pf = Portfolio(INST, {}, {}, {}, 1_000_000, 1_000_000)
    r = simulate(pf, [Target("000660", D("50")), Target("999999", D("50"))], 1_000_000)
    assert [ln.issues for ln in r.lines] == [("NO_PRICE",), ("NOT_REGISTERED",)]
    r = simulate(pf, [Target("000660", D("100"), price=187_300)], 1_000_000)
    assert r.lines[0].qty == 5


def test_read_targets_and_write_csv(tmp_path):
    t = read_targets("﻿종목코드,편입비,가격\n69500,40%,\n000660,60,\"187,300\"\n")
    assert t == [Target("069500", D("40")), Target("000660", D("60"), 187_300)]
    with pytest.raises(BasketError):
        read_targets("code,w\n1,2\n")
    r = simulate(PF, t, 1_000_000)
    p = write_csv(r, tmp_path / "b.csv")
    text = p.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("종목코드,종목명,현재 편입비") and "합계" in text
    with pytest.raises(BasketError, match="이미"):
        write_csv(r, p)


def test_each_line_runs_the_order_check_as_if_the_earlier_lines_were_sent():
    from dataclasses import replace

    from ministp.risk import RiskLimits
    # 휴장일에는 어느 줄도 못 보내요 (R-38)
    r = simulate(replace(PF, business_day=False), [Target("005930", D("100"))], 1_000_000)
    assert r.lines[0].issues == ("NOT_BUSINESS_DAY",) and not r.ok
    # 1회 주문 금액 한도
    r = simulate(PF, [Target("005930", D("100"))], 1_000_000, limits=RiskLimits(max_order_value=500_000))
    assert "MAX_ORDER_VALUE" in r.lines[0].issues
    # 미체결 매도가 있으면 그만큼 매도가능수량이 줄어요 (보유 96, 매도가능 50)
    r = simulate(PF, [Target("005930", D("100"))], 3_000_000, side=Side.SELL)
    assert r.lines[0].qty == 51 and "SHORT_SELL" in r.lines[0].issues
    r = simulate(PF, [Target("005930", D("100"))], 2_905_000, side=Side.SELL)
    assert r.lines[0].qty == 50 and "SHORT_SELL" not in r.lines[0].issues
    # 앞 줄 매수가 주문가능금액을 쓰면 뒷줄이 주문 검증에서 걸려요 (보낼 때와 같은 결과)
    pf = replace(PF, available_cash=5_000_000, projected_cash=5_000_000)
    r = simulate(pf, [Target("069500", D("60")), Target("360750", D("40"))], 8_000_000, weight_limit=D(100))
    assert r.lines[0].issues == () and r.lines[1].issues == ("INSUFFICIENT_CASH",)
