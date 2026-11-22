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


def test_config_of_another_account_reads_only_its_own_variables():
    """다계좌: each account has its own prefix, and its own token cache file (a token belongs to one app key)."""
    second = {"MINISTP_KIS_2_APP_KEY": "key2", "MINISTP_KIS_2_APP_SECRET": "sec2",
              "MINISTP_KIS_2_ACCOUNT": "60001234-01", "MINISTP_KIS_2_HTS_ID": "hts2"}
    c = KisConfig.from_env({**ENV, **second}, "MINISTP_KIS_2")
    assert (c.app_key, c.app_secret, c.account_no, c.hts_id) == ("key2", "sec2", "60001234", "hts2")
    assert c.token_cache.name == "kis_token_kis_2.json"
    assert KisConfig.from_env({**ENV, **second}).token_cache.name == "kis_token.json"
    with pytest.raises(KisError, match="MINISTP_KIS_2_APP_KEY, MINISTP_KIS_2_APP_SECRET, MINISTP_KIS_2_ACCOUNT"):
        KisConfig.from_env(ENV, "MINISTP_KIS_2")          # never falls back to the default account's key


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
    KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None).token()
    KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None).token()
    assert sum(1 for c in fake.calls if c[1].endswith("tokenP")) == 1
    _, _, _, body = fake.calls[0]
    assert body == {"grant_type": "client_credentials", "appkey": cfg.app_key,
                    "appsecret": cfg.app_secret}


def test_expiring_token_is_renewed(cfg):
    fake = FakeKis([])
    KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None).token()
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
    fake = FakeKis([({}, {"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "없는 서비스 코드 입니다"})])
    with pytest.raises(KisError, match="OPSQ0002"):
        KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None).get_balance()


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


class FakeKisRoutes:
    """Route by path; record every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/oauth2/tokenP"):
            return 200, {}, {"access_token": "tok", "access_token_token_expired": "2026-11-18 09:00:00"}
        path = urllib.parse.urlparse(url).path
        return self.routes[path].pop(0)


def ok(output=None, output1=None, more=False):
    body = {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "주문 전송 완료 되었습니다."}
    if output is not None:
        body["output"] = output
    if output1 is not None:
        body["output1"] = output1
    return 200, {"tr_cont": "M" if more else "D"}, body


def test_place_market_and_limit_orders(cfg):
    fake = FakeKisRoutes({"/uapi/domestic-stock/v1/trading/order-cash": [
        ok({"KRX_FWDG_ORD_ORGNO": "00950", "ODNO": "0000117057", "ORD_TMD": "091500"}),
        ok({"KRX_FWDG_ORD_ORGNO": "00950", "ODNO": "0000117058", "ORD_TMD": "091501"})]})
    c = KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None)
    ack = c.place_order("069500", "BUY", 1, None)
    c.place_order("069500", "SELL", 2, 40_000)
    assert (ack.order_no, ack.org_no) == ("0000117057", "00950")
    posts = [x for x in fake.calls if x[0] == "POST" and "order-cash" in x[1]]
    assert [p[2]["tr_id"] for p in posts] == ["VTTC0012U", "VTTC0011U"]
    assert posts[0][3]["ORD_DVSN"] == "01" and posts[0][3]["ORD_UNPR"] == "0"
    assert posts[1][3]["ORD_DVSN"] == "00" and posts[1][3]["ORD_UNPR"] == "40000"
    assert posts[1][3]["ORD_QTY"] == "2" and posts[1][3]["SLL_TYPE"] == "01"


def test_cancel_uses_org_and_order_no(cfg):
    from ministp.broker import OrderAck
    fake = FakeKisRoutes({"/uapi/domestic-stock/v1/trading/order-rvsecncl": [ok({})]})
    KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None).cancel_order(OrderAck("0000117057", "00950"))
    _, _, headers, body = fake.calls[-1]
    assert headers["tr_id"] == "VTTC0013U"
    assert (body["KRX_FWDG_ORD_ORGNO"], body["ORGN_ODNO"], body["RVSE_CNCL_DVSN_CD"],
            body["QTY_ALL_ORD_YN"]) == ("00950", "0000117057", "02", "Y")


def test_daily_orders_parsed_across_pages(cfg):
    r1 = {"odno": "0000117057", "pdno": "069500", "sll_buy_dvsn_cd": "02", "ord_qty": "10",
          "tot_ccld_qty": "4", "tot_ccld_amt": "160000", "cnc_cfrm_qty": "0", "rjct_qty": "0"}
    r2 = {**r1, "odno": "0000117058", "sll_buy_dvsn_cd": "01", "tot_ccld_qty": "0",
          "tot_ccld_amt": "0", "cnc_cfrm_qty": "10"}
    fake = FakeKisRoutes({"/uapi/domestic-stock/v1/trading/inquire-daily-ccld": [
        ok(output1=[r1], more=True), ok(output1=[r2, {"odno": ""}])]})
    orders = KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None).daily_orders(d("11-17"))
    assert [(o.order_no, o.side, o.filled_qty, o.filled_amount, o.canceled_qty) for o in orders] == [
        ("0000117057", "BUY", 4, 160_000, 0), ("0000117058", "SELL", 0, 0, 10)]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(fake.calls[1][1]).query)
    assert q["INQR_STRT_DT"] == q["INQR_END_DT"] == ["20261117"]
    assert fake.calls[1][2]["tr_id"] == "VTTC0081R"


def test_get_price(cfg):
    fake = FakeKisRoutes({"/uapi/domestic-stock/v1/quotations/inquire-price": [
        ok({"stck_prpr": "40125"})]})
    assert KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None).get_price("069500") == 40_125


def test_rate_limited_query_is_paced_and_retried(cfg):
    limited = (500, {}, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."})
    fake = FakeKisRoutes({"/uapi/domestic-stock/v1/quotations/inquire-price": [
        limited, ok({"stck_prpr": "40125"})]})
    slept, clock = [], [100.0]
    c = KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: slept.append(s) or clock.__setitem__(0, clock[0] + s),
                  monotonic=lambda: clock[0])
    assert c.get_price("069500") == 40_125
    assert 1.0 in slept                       # backoff after EGW00201
    assert any(0 < s <= 0.6 for s in slept)   # pacing between consecutive calls


def test_rate_limited_order_is_not_retried(cfg):
    limited = (500, {}, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."})
    fake = FakeKisRoutes({"/uapi/domestic-stock/v1/trading/order-cash": [limited]})
    c = KisClient(cfg, fake, now=lambda: NOW, sleep=lambda s: None)
    with pytest.raises(KisError, match="EGW00201"):
        c.place_order("069500", "BUY", 1, None)
    assert sum(1 for x in fake.calls if "order-cash" in x[1]) == 1
