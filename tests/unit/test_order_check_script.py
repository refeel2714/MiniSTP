"""Dry run of scripts/kis_order_check.py with a fake broker (no network, no keys)."""
import datetime as dt
import importlib.util
from pathlib import Path

from ministp.broker import BrokerBalance, BrokerOrder, BrokerPosition, OrderAck

spec = importlib.util.spec_from_file_location(
    "kis_order_check", Path(__file__).parents[2] / "scripts" / "kis_order_check.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

ENV = {"MINISTP_KIS_APP_KEY": "k" * 20, "MINISTP_KIS_APP_SECRET": "s", "MINISTP_KIS_ACCOUNT": "12345678-01"}
OPEN = lambda: dt.datetime(2026, 11, 18, 10, 0)  # Wednesday


class PaperBroker:
    def __init__(self, cfg):
        self.cash, self.order = 100_000_000, None

    def get_balance(self):
        if self.order is None or not self.order.filled_qty:
            pos, proj = {}, self.cash
        else:
            pos = {"069500": BrokerPosition("069500", 1, 40_125)}
            proj = self.cash - 40_125 - 6  # 0.015% fee, truncated
        raw = ({"output2": [{"dnca_tot_amt": str(self.cash), "prvs_rcdl_excc_amt": str(proj)}]},)
        return BrokerBalance(pos, self.cash, proj, raw)

    def get_price(self, symbol):
        return 40_125

    def place_order(self, symbol, side, qty, limit_price):
        self.order = BrokerOrder("0000000001", symbol, side, qty, 0, 0, 0, 0)
        return OrderAck("0000000001", "00950")

    def daily_orders(self, day):
        if self.order and not self.order.filled_qty:
            self.order = BrokerOrder("0000000001", "069500", "BUY", 1, 1, 40_125, 0, 0)
        return [self.order] if self.order else []


def test_full_loop_reconciles(capsys):
    assert script.main([], client_factory=PaperBroker, now=OPEN, sleep=lambda s: None, environ=ENV) == 0
    out = capsys.readouterr().out
    assert "체결: 40,125원 x 1주" in out
    assert "BREAK" not in out


def test_refuses_outside_market_hours(capsys):
    late = lambda: dt.datetime(2026, 11, 18, 16, 0)
    assert script.main([], client_factory=PaperBroker, now=late, sleep=lambda s: None, environ=ENV) == 2


def test_refuses_live_server(capsys):
    env = {**ENV, "MINISTP_KIS_ENV": "live", "MINISTP_ALLOW_LIVE": "1"}
    assert script.main([], client_factory=PaperBroker, now=OPEN, sleep=lambda s: None, environ=env) == 2
