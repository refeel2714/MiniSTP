"""KIS client against a fake transport: no network, no real keys."""
import datetime as dt
import urllib.parse

import pytest

from conftest import d
from gate.trades import BUSINESS_DAYS, INITIAL_DEPOSIT, run_day
from ministp.broker import (BrokerBalance, BrokerPosition, KisClient, KisConfig, KisError,
                            LiveTradingLocked, recon_items)
from ministp.recon import Cause, Status, reconcile

NOW = dt.datetime(2026, 11, 17, 9, 0, 0)
ENV = {"MINISTP_KIS_APP_KEY": "fake-app-key-1234", "MINISTP_KIS_APP_SECRET": "fake-secret",
       "MINISTP_KIS_ACCOUNT": "50001234-01"}


class FakeKis:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/oauth2/tokenP"):
            return 200, {}, {"access_token": "tok", "token_type": "Bearer",
                             "access_token_token_expired": "2026-11-18 09:00:00"}
        resp_headers, payload = self.pages.pop(0)
        return 200, resp_headers, payload


def page(rows, summary=None, more=False, nk="NK1"):
    body = {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리",
            "output1": rows, "output2": [summary] if summary else [],
            "ctx_area_fk100": "FK1", "ctx_area_nk100": nk}
    return ({"tr_cont": "M" if more else "D"}, body)


def row(sym, qty, amt):
    return {"pdno": sym, "prdt_name": sym, "hldg_qty": str(qty), "pchs_amt": str(amt)}


SUMMARY = {"dnca_tot_amt": "14999250", "nxdy_excc_amt": "14999250",
           "prvs_rcdl_excc_amt": "15641758"}


@pytest.fixture
def cfg(tmp_path):
    c = KisConfig.from_env(ENV)
    return KisConfig(c.app_key, c.app_secret, c.account_no, c.product_code,
                     token_cache=tmp_path / "token.json")


def test_config_from_env_defaults_to_paper():
    c = KisConfig.from_env(ENV)
    assert (c.env, c.account_no, c.product_code) == ("paper", "50001234", "01")
    assert "openapivts" in c.base_url
    assert "fake" not in repr(c)  # secrets hidden from repr / logs


def test_live_needs_two_switches():
    with pytest.raises(LiveTradingLocked):
        KisConfig.from_env({**ENV, "MINISTP_KIS_ENV": "live"})
    c = KisConfig.from_env({**ENV, "MINISTP_KIS_ENV": "live", "MINISTP_ALLOW_LIVE": "1"})
    assert "openapi.koreainvestment.com:9443" in c.base_url


def test_missing_env_is_reported():
    with pytest.raises(KisError, match="MINISTP_KIS_APP_SECRET"):
        KisConfig.from_env({"MINISTP_KIS_APP_KEY": "x", "MINISTP_KIS_ACCOUNT": "12345678"})


def test_token_is_cached_across_clients(cfg):
    fake = FakeKis([])
    KisClient(cfg, fake, now=lambda: NOW).token()
    KisClient(cfg, fake, now=lambda: NOW).token()
    assert sum(1 for c in fake.calls if c[1].endswith("tokenP")) == 1
    _, _, _, body = fake.calls[0]
    assert body == {"grant_type": "client_credentials", "appkey": cfg.app_key,
                    "appsecret": cfg.app_secret}


def test_expiring_token_is_renewed(cfg):
    fake = FakeKis([])
    KisClient(cfg, fake, now=lambda: NOW).token()
    KisClient(cfg, fake, now=lambda: dt.datetime(2026, 11, 18, 8, 55)).token()
    assert sum(1 for c in fake.calls if c[1].endswith("tokenP")) == 2


def test_balance_with_continuation(cfg):
    fake = FakeKis([page([row("A", 90, 4_590_000)], more=True),
                    page([row("E", 0, 0)], SUMMARY)])
    bal = KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None).get_balance()
    assert bal.positions == {"A": BrokerPosition("A", 90, 4_590_000)}
    assert (bal.cash_d0, bal.cash_projected) == (14_999_250, 15_641_758)

    gets = [c for c in fake.calls if c[0] == "GET"]
    assert [g[2]["tr_id"] for g in gets] == ["VTTC8434R", "VTTC8434R"]
    assert [g[2]["tr_cont"] for g in gets] == ["", "N"]
    q2 = urllib.parse.parse_qs(urllib.parse.urlparse(gets[1][1]).query)
    assert q2["CTX_AREA_NK100"] == ["NK1"] and q2["CANO"] == ["50001234"]
    assert gets[0][2]["authorization"] == "Bearer tok"


def test_api_error_is_raised(cfg):
    fake = FakeKis([({}, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."})])
    with pytest.raises(KisError, match="EGW00201"):
        KisClient(cfg, fake, now=lambda: NOW).get_balance()


def test_recon_items_against_ledger_on_11_04(ledger):
    """Same numbers as spec 04 S8: only the 1-won projected-cash break remains."""
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    for day in BUSINESS_DAYS[:3]:
        run_day(ledger, day, d)
    broker = BrokerBalance({"A": BrokerPosition("A", 90, 4_590_000)}, 14_999_250, 15_641_759)
    results = reconcile(d("11-04"), recon_items(ledger, broker))
    breaks = [(r.item, r.cause, r.diff) for r in results if r.status is Status.BREAK]
    assert breaks == [("CASH_PROJECTED", Cause.ROUNDING, 1)]


def test_recon_flags_position_missing_on_one_side(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    broker = BrokerBalance({"Z": BrokerPosition("Z", 5, 50_000)}, 1_000_000, 1_000_000)
    results = reconcile(d("11-02"), recon_items(ledger, broker))
    assert [(r.item, r.key, r.internal, r.broker) for r in results if r.status is Status.BREAK] == [
        ("POSITION_QTY", "Z", 0, 5), ("BOOK_COST", "Z", 0, 50_000)]
