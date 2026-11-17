"""Spec 04 S7 주문 상태 (R-40~42)."""
import pytest

from ministp.oms import OrderManager, OrderStatus, OverfillError

pytestmark = pytest.mark.gate


def test_s7_partial_fill_cancel_race_and_overfill():
    om = OrderManager()
    o = om.create("O1", symbol="A", side="BUY", qty=100, limit_price=50_000)
    assert (o.status, o.filled_qty) == (OrderStatus.NEW, 0)

    om.on_accepted("O1")
    assert o.status == OrderStatus.SUBMITTED

    om.on_fill("O1", 30)
    assert (o.status, o.filled_qty) == (OrderStatus.PARTIALLY_FILLED, 30)

    om.request_cancel("O1")
    assert (o.status, o.filled_qty) == (OrderStatus.CANCEL_PENDING, 30)

    om.on_fill("O1", 20)  # R-42: fill after cancel request is still booked
    assert (o.status, o.filled_qty) == (OrderStatus.CANCEL_PENDING, 50)

    om.on_cancel_confirmed("O1", canceled_qty=50)
    assert (o.status, o.filled_qty) == (OrderStatus.CANCELED, 50)

    with pytest.raises(OverfillError):
        om.on_fill("O1", 60)
    assert o.filled_qty == 50
    assert om.halted  # kill switch engaged
    assert [e.status_after for e in o.events][-1] == "ERROR_OVERFILL"


def test_halted_manager_rejects_new_orders():
    om = OrderManager()
    om.halt("test")
    with pytest.raises(RuntimeError):
        om.create("O2", symbol="A", side="BUY", qty=1, limit_price=1)
