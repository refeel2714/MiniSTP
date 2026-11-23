"""MiniSTP command line, run on the owner's PC.

    python -m ministp db-init                     # 처음 한 번: DB와 테이블 만들기
    python -m ministp instrument 069500 "KODEX 200" --etf
    python -m ministp open                        # 처음 한 번: 증권사 잔고를 원장 시작값으로
    python -m ministp run                         # 장중: 실시간 체결 반영 + 주문 콘솔 (IBOR)
    python -m ministp order 069500 buy 1          # 장중: 시장가 주문 1건 (--limit 가격)
    python -m ministp daily                       # 매 영업일 장 마감 후(15:40~): 결제, NAV, 대사, 일 마감
    python -m ministp status                      # 보유, 예수금, NAV, 무오류 연속일
    python -m ministp resolve 12 FEE_DIFF "메모"   # 불일치 판단 기록 (새 행으로 추가)
    python -m ministp close                       # 불일치를 판단한 날의 장부 마감 (깨끗한 날은 daily가 마감)
    python -m ministp sample-db                   # 데모용 샘플 DB(ministp_sample) 만들기, 운영 DB와 별개
    python -m ministp account list                # 계좌 목록. open/run/order/daily/status는 --account 계좌ID
    python -m ministp code list                   # 공통코드 그룹. code list 그룹ID는 그 그룹의 코드와 코드명
    python -m ministp dividend list               # 현금배당 권리와 계좌별 인식·입금 현황 (등록은 dividend add)
    python -m ministp cash list                   # 입출금·대체 내역과 출금가능금액 (기록은 cash deposit | withdraw | transfer)
    python -m ministp adjust list                 # 수동 조정 내역 (예탁금 이용료, 수수료·세금·배당 차이. 기록은 adjust add)
    python -m ministp audit                       # 감사 로그: 누가 언제 무엇을 했고 어떻게 끝났는지 (거부·실패 포함)

Paper (모의투자) only until Phase 4. Keys are asked for and never saved.
"""
import argparse
import datetime as dt
import getpass
import os
import queue
import sys
import threading
import time
import urllib.parse

import psycopg

from . import adjustments, audit, cash
from .broker import KisClient, KisConfig, KisError, trading_env
from .calendar import CalendarNotLoadedError
from .cli import ask_missing, krx_calendar
from .fees import FeeSchedule
from .instruments import AssetType, Instrument, Market
from .ledger import Side
from .oms import OrderStatus
from .accounts import STATUSES, add_account, add_customer, list_accounts, set_status
from .ops import (GATE_DAYS, IntradayRunner, OpsError, acquire_writer, add_instrument, clean_streak, close_books, daily_close, load_books, open_books,
                  place_order, require_current_schema, resolve_break)
from .recon import Cause, Status
from . import rights
from .session import failure_text
from .store import DEFAULT_ACCOUNT, PostgresLedgerStore, SchemaError
from .store import migrate as mg

DB_NAME = "ministp"
MARKET_OPEN, MARKET_CLOSE, DAILY_FROM = dt.time(9, 0), dt.time(15, 20), dt.time(15, 40)


def database_url(environ) -> str:
    url = environ.get("MINISTP_DATABASE_URL")
    if url:
        return url
    pw = getpass.getpass("PostgreSQL postgres 계정 비밀번호 (화면에 표시되지 않아요): ").strip()
    return f"postgresql://postgres:{urllib.parse.quote(pw, safe='')}@localhost:5432/{DB_NAME}"


def _broker(environ, client_factory, acct, others=()):
    """The broker client of one account, from that account's own environment variables.

    Each account reads only the variables that start with its `credential_ref`, so an
    order for one account cannot go out with another account's key by a slip of the hand."""
    prefix = acct.env_prefix
    who = "" if acct.account_id == DEFAULT_ACCOUNT else f"[{acct.account_id} {acct.alias}] "
    env = ask_missing(environ, prefix, who)
    cfg = KisConfig.from_env(env, prefix)
    if cfg.env != "paper" or acct.env != "PAPER":
        raise OpsError("Phase 4 전까지는 모의투자 서버와 모의투자(PAPER) 계좌에서만 실행해요.")
    number = env[f"{prefix}_ACCOUNT"].replace("-", "")[:8]
    for o in others:   # two ledger accounts must not point at one brokerage account
        if o.account_id != acct.account_id and (environ.get(f"{o.env_prefix}_ACCOUNT") or "").replace("-", "")[:8] == number:
            raise OpsError(f"{prefix}_ACCOUNT와 {o.env_prefix}_ACCOUNT가 같은 증권사 계좌예요. "
                           f"계좌 {acct.account_id}와 {o.account_id}의 원장이 섞이지 않게 환경변수를 확인해 주세요.")
    return client_factory(cfg)


def _print_recon(report) -> None:
    for r in report.results:
        mark = "OK   " if r.status is Status.MATCH else "BREAK"
        print(f"  {mark} {r.item:15} {r.key:10} 내부 {r.internal:>15,}  증권사 {r.broker:>15,}"
              + (f"  차이 {r.diff:,} ({r.cause})" if r.status is Status.BREAK else ""))
    for a in report.anomalies:
        print(f"  이상: {a.kind} {a.ref} {a.detail}")


CASH_ACTIONS = {"deposit": "CASH_DEPOSIT", "withdraw": "CASH_WITHDRAW", "transfer": "CASH_TRANSFER"}
CODE_ACTIONS = {"rename": "CODE_RENAME", "group-add": "CODE_GROUP_ADD", "add": "CODE_ADD", "use": "CODE_USE"}


def audited(args, today: dt.date):
    """The request a command makes, as the audit log names it: (action, account, target, detail).
    None for a command that changes nothing. Used for the row of a request that was refused or failed;
    a request that goes through is recorded by the function that does it."""
    cmd, what = args.cmd, getattr(args, "what", None)
    if cmd == "instrument":
        return "INSTRUMENT_ADD", None, args.symbol, {"name": args.name}
    if cmd == "open":
        return "OPEN", args.account, today.isoformat(), None
    if cmd == "order":
        return "ORDER", args.account, None, {"symbol": args.symbol, "side": args.side.upper(), "qty": args.qty,
                                             "limit_price": args.limit, "date": today}
    if cmd in ("daily", "close"):
        return cmd.upper(), args.account, (args.date or today).isoformat(), None
    if cmd == "resolve":
        return "RESOLVE", None, str(args.result_id), {"cause": args.cause, "note": args.note}
    if cmd == "customer":
        return "CUSTOMER_ADD", None, args.customer_id, {"alias": args.alias}
    if cmd == "account" and what == "add":
        return "ACCOUNT_ADD", args.account_id, args.account_id, {"customer_id": args.customer_id, "alias": args.alias,
                                                                "credential_ref": args.credential}
    if cmd == "account" and what == "status":
        return "ACCOUNT_STATUS", args.account_id, args.account_id, {"to": args.status, "reason": args.reason,
                                                                   "effective_from": args.effective_from}
    if cmd == "dividend" and what == "add":
        return "CORPORATE_ACTION_ADD", None, None, {"symbol": args.symbol, "record_date": args.record,
                                                    "pay_date": args.pay, "amount_per_share": args.per_share}
    if cmd == "dividend" and what == "cancel":
        return "CORPORATE_ACTION_CANCEL", None, f"CA-{args.action_id}", {"reason": args.reason}
    if cmd == "dividend" and what == "pay":
        return "DIVIDEND_PAY", args.account, f"CA-{args.action_id}", {"gross": args.gross, "tax": args.tax,
                                                                      "date": args.date or today}
    if cmd == "cash" and what in CASH_ACTIONS:
        return CASH_ACTIONS[what], args.account, None, {"date": args.date or today, "amount": args.amount,
                                                       "to": getattr(args, "to", None), "memo": args.memo or None}
    if cmd == "adjust" and what == "add":
        return "ADJUSTMENT_ADD", args.account, None, {"date": args.date or today, "reason": args.reason,
                                                      "amount": args.amount, "tax": args.tax or None,
                                                      "recon_result_id": args.result_id, "memo": args.memo,
                                                      "confirmed": args.confirm or None}
    if cmd == "adjust" and what == "cancel":
        return "ADJUSTMENT_CANCEL", args.account, f"ADJ-{args.adjustment_id}", {"date": args.date or today,
                                                                                "why": args.reason}
    if cmd == "code" and what in CODE_ACTIONS:
        target = args.group_id + (f".{args.code}" if what != "group-add" else "")
        return CODE_ACTIONS[what], None, target, {"name": getattr(args, "name", None),
                                                  "state": getattr(args, "state", None)}
    return None


def _not_now(conn, args, today: dt.date, message: str) -> int:
    """A command asked for at the wrong time: said, and kept as a refused request."""
    print(message)
    action, account_id, target, detail = audited(args, today)
    audit.record_refusal(conn, action, OpsError(message), account_id=account_id, target=target, detail=detail)
    return 2


def run(argv, **kw) -> int:
    """Every audit row this command writes: from the console, by whoever is logged in to this PC."""
    with audit.acting_as("CLI"):
        return _run(argv, **kw)


def _run(argv, *, environ=os.environ, client_factory=KisClient, connect=psycopg.connect,
         now=dt.datetime.now, sleep=time.sleep, feed_factory=None, console=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ministp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("db-init")
    p = sub.add_parser("instrument")
    p.add_argument("symbol"), p.add_argument("name")
    kind = p.add_mutually_exclusive_group(required=True)
    kind.add_argument("--etf", action="store_true"), kind.add_argument("--stock", action="store_true")
    p.add_argument("--kosdaq", action="store_true")
    by_account = argparse.ArgumentParser(add_help=False)
    by_account.add_argument("--account", default=DEFAULT_ACCOUNT, metavar="계좌ID",
                            help=f"다룰 계좌 (기본 {DEFAULT_ACCOUNT}). account list로 확인")
    sub.add_parser("open", parents=[by_account])
    sub.add_parser("run", parents=[by_account])
    p = sub.add_parser("order", parents=[by_account])
    p.add_argument("symbol"), p.add_argument("side", choices=["buy", "sell"])
    p.add_argument("qty", type=int), p.add_argument("--limit", type=int)
    p = sub.add_parser("daily", parents=[by_account])
    p.add_argument("--date", type=dt.date.fromisoformat, help="지난 영업일을 다시 대사할 때")
    p = sub.add_parser("close", parents=[by_account],
                       help="장부 마감: 불일치가 있던 날을 원인 판단(resolve) 뒤에 직접 마감 (깨끗한 날은 daily가 마감해요)")
    p.add_argument("--date", type=dt.date.fromisoformat, help="마감할 영업일 (기본: 오늘)")
    sub.add_parser("status", parents=[by_account])
    p = sub.add_parser("customer", help="고객 등록 (별칭만, 개인정보는 넣지 않아요)")
    what = p.add_subparsers(dest="what", required=True)
    q = what.add_parser("add")
    q.add_argument("customer_id", metavar="고객ID"), q.add_argument("alias", metavar="별칭")
    p = sub.add_parser("account", help="위탁계좌 목록·등록·상태 변경 (계좌번호와 키는 DB에 넣지 않아요)")
    what = p.add_subparsers(dest="what", required=True)
    what.add_parser("list")
    q = what.add_parser("add")
    q.add_argument("account_id", metavar="계좌ID"), q.add_argument("customer_id", metavar="고객ID")
    q.add_argument("alias", metavar="별칭")
    q.add_argument("--credential", required=True, metavar="이름",
                   help="이 계좌의 환경변수 이름 머리말. KIS_2면 MINISTP_KIS_2_APP_KEY 등을 읽어요")
    q = what.add_parser("status")
    q.add_argument("account_id", metavar="계좌ID"), q.add_argument("status", choices=list(STATUSES))
    q.add_argument("reason", metavar="사유")
    q.add_argument("--from", dest="effective_from", type=dt.date.fromisoformat, help="적용 시작일 (기본 오늘)")
    p = sub.add_parser("code", help="공통코드: 코드와 코드명 보기, 코드명 바꾸기, 스키마·프로그램과 대조")
    what = p.add_subparsers(dest="what", required=True)
    q = what.add_parser("list", help="코드 그룹 목록. 그룹ID를 주면 그 그룹의 코드")
    q.add_argument("group_id", nargs="?", metavar="그룹ID")
    q = what.add_parser("rename", help="코드명 바꾸기 (코드 자체는 바뀌지 않아요)")
    q.add_argument("group_id", metavar="그룹ID"), q.add_argument("code", metavar="코드")
    q.add_argument("name", metavar="코드명"), q.add_argument("--desc", metavar="설명")
    q = what.add_parser("group-add", help="운영자가 관리하는 코드 그룹 만들기")
    q.add_argument("group_id", metavar="그룹ID"), q.add_argument("name", metavar="그룹명")
    q.add_argument("--desc", default="", metavar="설명")
    q = what.add_parser("add", help="운영자가 관리하는 그룹에 코드 추가")
    q.add_argument("group_id", metavar="그룹ID"), q.add_argument("code", metavar="코드")
    q.add_argument("name", metavar="코드명"), q.add_argument("--desc", default="", metavar="설명")
    q = what.add_parser("use", help="코드 끄기·켜기 (지우지는 않아요)")
    q.add_argument("group_id", metavar="그룹ID"), q.add_argument("code", metavar="코드")
    q.add_argument("state", choices=["on", "off"])
    what.add_parser("check", help="공통코드가 스키마 제약, 프로그램, 저장된 값과 어긋나지 않는지 점검")
    p = sub.add_parser("dividend", help="현금배당(주식 배당금, ETF 분배금): 권리 등록·취소, 계좌별 인식과 입금 현황")
    what = p.add_subparsers(dest="what", required=True)
    what.add_parser("list")
    q = what.add_parser("add", help="권리 등록. 인식과 입금은 그 뒤 각 계좌의 daily가 날짜에 맞춰 반영해요")
    q.add_argument("symbol", metavar="종목코드")
    q.add_argument("--record", required=True, type=dt.date.fromisoformat, metavar="배당기준일")
    q.add_argument("--pay", required=True, type=dt.date.fromisoformat, metavar="지급일")
    q.add_argument("--per-share", required=True, metavar="주당배당금", help="세전, 원")
    q.add_argument("--note", default="", metavar="메모")
    q = what.add_parser("cancel", help="잘못 등록했거나 취소된 권리를 무효로 (지우지 않고 취소 행을 더해요)")
    q.add_argument("action_id", type=int, metavar="권리번호"), q.add_argument("reason", metavar="사유")
    q = what.add_parser("pay", parents=[by_account],
                        help="증권사가 실제로 준 금액으로 입금 기록 (예상과 다르거나, 원장 시작 전 보유분의 배당일 때)")
    q.add_argument("action_id", type=int, metavar="권리번호")
    q.add_argument("--gross", type=int, metavar="세전금액"), q.add_argument("--tax", type=int, metavar="세금")
    q.add_argument("--date", type=dt.date.fromisoformat, help="기록할 날짜 (기본: 오늘)")
    p = sub.add_parser("cash", help="입출금·대체: 증권사에서 넣거나 뺀 돈, 내 계좌끼리 옮긴 돈을 원장에 기록")
    what = p.add_subparsers(dest="what", required=True)
    what.add_parser("list", parents=[by_account], help="입출금·대체 내역과 출금가능금액")
    for name, text in (("deposit", "입금 기록"), ("withdraw", "출금 기록 (출금가능금액 안에서)"),
                       ("transfer", "같은 고객의 다른 계좌로 대체 (--account가 보내는 계좌)")):
        q = what.add_parser(name, parents=[by_account], help=text)
        q.add_argument("amount", type=int, metavar="금액")
        if name == "transfer":
            q.add_argument("--to", required=True, metavar="받는계좌")
        q.add_argument("--date", type=dt.date.fromisoformat, help="돈이 실제로 움직인 날짜 (기본: 오늘)")
        q.add_argument("--memo", default="", metavar="메모")
    p = sub.add_parser("adjust", help="수동 조정: 예탁금 이용료, 증권사와의 수수료·세금·배당 차이를 사유와 함께 기록")
    what = p.add_subparsers(dest="what", required=True)
    what.add_parser("list", parents=[by_account], help="이 계좌의 조정 내역")
    q = what.add_parser("add", parents=[by_account], help="조정 기록. 사유가 상대 계정을 정해요")
    q.add_argument("reason", metavar="사유", type=str.upper, choices=adjustments.REASONS)
    q.add_argument("amount", type=int, metavar="금액", help="세전. 예수금이 늘면 +, 줄면 -")
    q.add_argument("--tax", type=int, default=0, metavar="세금", help="INTEREST에서 뗀 세금")
    q.add_argument("--memo", required=True, metavar="메모", help="왜 조정하는지")
    q.add_argument("--break", dest="result_id", type=int, metavar="불일치행", help="이 조정으로 정리하는 대사 불일치 행")
    q.add_argument("--confirm", action="store_true",
                   help=f"차이 사유로 {adjustments.CONFIRM_ABOVE:,}원을 넘는 금액일 때: 증권사 거래내역과 같은 금액임을 확인했다는 뜻")
    q.add_argument("--date", type=dt.date.fromisoformat, help="기록할 날짜 (기본: 오늘)")
    q = what.add_parser("cancel", parents=[by_account], help="조정을 반대 전표로 되돌림 (지우지 않아요)")
    q.add_argument("adjustment_id", type=int, metavar="조정번호"), q.add_argument("reason", metavar="사유")
    q.add_argument("--date", type=dt.date.fromisoformat, help="기록할 날짜 (기본: 오늘)")
    p = sub.add_parser("resolve")
    p.add_argument("result_id", type=int), p.add_argument("cause", choices=[c.value for c in Cause])
    p.add_argument("note")
    p = sub.add_parser("sample-db")
    p.add_argument("--days", type=int, default=40, help="시뮬레이션할 영업일 수")
    p.add_argument("--accounts", type=int, default=3, choices=[1, 2, 3], help="계좌 수 (기본 3: 고객 2명, 계좌 3개)")
    p = sub.add_parser("audit", help="감사 로그 보기: 처리·거부·실패한 요청을 최근 것부터 (읽기 전용)")
    p.add_argument("--limit", type=int, default=30, metavar="N", help="보여줄 줄 수 (기본 30)")
    p.add_argument("--account", metavar="계좌ID"), p.add_argument("--action", choices=sorted(audit.ACTIONS))
    p.add_argument("--outcome", choices=sorted(audit.OUTCOMES))
    p = sub.add_parser("db-migrate", help="기존 DB를 다음 ERD 버전으로 (기본은 상태와 계획만 보여줘요)")
    how = p.add_mutually_exclusive_group()
    how.add_argument("--dry-run", action="store_true", help="적용해 보고 전부 되돌려요 (리허설)")
    how.add_argument("--apply", action="store_true", help="실제로 적용해요. 먼저 pg_dump로 백업하세요")
    p.add_argument("--sample", action="store_true", help="샘플 DB(ministp_sample)에 실행")
    p = sub.add_parser("verify", help="원장 무결성 점검: 저장된 행이 이벤트를 다시 계산한 결과와 같은지 (읽기 전용)")
    p.add_argument("--sample", action="store_true", help="샘플 DB(ministp_sample)를 점검")
    p = sub.add_parser("ui", help="업무화면(STP-UI)을 이 PC에서만 열기 (기본은 조회 전용)")
    p.add_argument("--sample", action="store_true", help="샘플 DB(ministp_sample)로 열기. 연습용이라 처리 버튼도 켜져요")
    p.add_argument("--write", action="store_true", help="운영 DB에서 화면의 처리 버튼을 켜기 (없으면 조회 전용)")
    p.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    started = now()
    today = started.date()
    calendar = krx_calendar()
    fees = FeeSchedule.kis_paper()
    url = database_url(environ)

    try:
        if args.cmd == "db-init":
            _create_database(url, connect)
            with connect(url, autocommit=True) as conn:
                PostgresLedgerStore(conn).init_schema()
            print("DB 준비 완료.")
            return 0

        if args.cmd == "sample-db":
            return _sample_db(url, environ, connect, calendar, args.days, args.accounts)

        if args.cmd == "ui":
            return _ui(url, environ, calendar, fees, args.sample, args.port, write=args.write)

        if args.cmd == "db-migrate":
            target = url
            if args.sample:
                target = (environ.get("MINISTP_SAMPLE_DATABASE_URL")
                          or psycopg.conninfo.make_conninfo(url, dbname=SAMPLE_DB))
            return _db_migrate(target, connect, calendar, fees, dry_run=args.dry_run, apply=args.apply)

        if args.cmd == "verify":
            target = url
            if args.sample:
                target = (environ.get("MINISTP_SAMPLE_DATABASE_URL")
                          or psycopg.conninfo.make_conninfo(url, dbname=SAMPLE_DB))
            return _verify(target, connect, calendar, fees)

        # R-86: a request that a rule refuses, or that fails, changes nothing. Its audit row is written here.
        with connect(url, autocommit=True) as conn, audit.refusals(
                conn, lambda: audited(args, today), refused=(OpsError,),
                failed=(KisError, CalendarNotLoadedError, SchemaError, psycopg.Error)):
            if args.cmd == "audit":
                require_current_schema(conn)
                return _audit(conn, args)

            if args.cmd == "instrument":
                market = Market.KOSDAQ if args.kosdaq else Market.KOSPI
                asset = AssetType.ETF if args.etf else AssetType.STOCK
                add_instrument(conn, Instrument(args.symbol, args.name, market, asset))
                print(f"등록: {args.symbol} {args.name} ({market.value} {asset.value})")
                return 0

            if args.cmd in ("open", "order", "daily", "close", "run", "resolve", "customer") or (
                    args.cmd in ("dividend", "cash", "adjust") and args.what != "list") or (
                    args.cmd == "account" and args.what != "list") or (
                    args.cmd == "code" and args.what not in ("list", "check")):
                acquire_writer(conn)

            if args.cmd == "code":
                require_current_schema(conn)
                return _codes(conn, args)

            if args.cmd in ("customer", "account"):
                require_current_schema(conn)
                return _accounts(conn, args, today)

            if args.cmd == "dividend" and args.what != "pay":
                require_current_schema(conn)
                return _dividends(conn, args, calendar)

            if args.cmd == "resolve":
                rid = resolve_break(conn, args.result_id, Cause(args.cause), args.note)
                print(f"판단 기록 완료 (행 {rid}).")
                return 0

            books = load_books(conn, calendar, fees, args.account)
            accounts = list_accounts(conn)
            acct = next(a for a in accounts if a.account_id == args.account)
            if len(accounts) > 1:   # say whose books these are whenever there is a choice
                print(f"계좌 {acct.account_id} {acct.alias} (고객 {acct.customer_id}, {acct.status})")

            if args.cmd == "status":
                led = books.ledger
                print(f"예수금 D+0 {led.cash_balance():,}  결제 후 {led.projected_cash():,}  "
                      f"출금가능 {led.withdrawable():,}")
                for sym, pos in led.positions().items():
                    print(f"  {sym} {books.instruments[sym].name}: {pos.qty}주, 매입금액 {pos.book_cost:,}")
                navs = led.nav_history()
                if navs:
                    row = navs[-1]
                    print(f"최근 NAV {row.date}: 순자산 {row.net_assets:,}, 기준가 {row.nav_per_1000}")
                last = navs[-1].date if navs else today
                print(f"무오류 연속 영업일: {clean_streak(conn, calendar, last, args.account)} / {GATE_DAYS}")
                if led.closed_through:
                    print(f"장부 마감: {led.closed_through}까지 ({len(led.closed_days())}일), "
                          f"그 뒤 이벤트 {len(led.unclosed_events())}건")
                else:
                    print("장부 마감: 아직 마감한 날이 없어요")
                return 0

            if args.cmd == "cash":
                return _cash(conn, args, books, calendar, fees, today)

            if args.cmd == "adjust":
                return _adjust(conn, args, books, today)

            if args.cmd == "dividend":   # pay: one account's books
                day = args.date or today
                paid = rights.record_payment(books, args.action_id, day, args.gross, args.tax)
                print(f"[{day}] 배당 입금 CA-{paid.action_id} {paid.symbol}: 세전 {paid.paid_gross:,}원, 세금 {paid.tax:,}원, "
                      f"예수금 +{paid.paid_gross - paid.tax:,}원" + (
                          f" (인식했던 {paid.accrued:,}원과의 차이는 배당수익에 반영)" if paid.accrued != paid.paid_gross else ""))
                return 0

            if args.cmd == "close":
                day = args.date or today
                nav = close_books(books, day)
                print(f"[{day}] 장부 마감: 순자산 {nav.net_assets:,}, 기준가 {nav.nav_per_1000}. "
                      "이 날짜까지는 더 기록되지 않아요.")
                return 0

            broker = _broker(environ, client_factory, acct, accounts)

            if args.cmd == "open":
                open_books(books, broker, today)
                led = books.ledger
                print(f"원장 시작: 예수금 {led.cash_balance():,}, 보유 {len(led.positions())}종목")
                for sym, pos in led.positions().items():
                    print(f"  {sym}: {pos.qty}주, 매입금액 {pos.book_cost:,}")
                return 0

            if args.cmd == "run":
                if not (MARKET_OPEN <= started.time() <= MARKET_CLOSE) or not calendar.is_business_day(today):
                    print("장중(영업일 09:00~15:20)에 실행해 주세요.")
                    return 2
                return _run_intraday(books, broker, today, environ, feed_factory, console, now, acct.env_prefix)

            if args.cmd == "order":
                if not (MARKET_OPEN <= started.time() <= MARKET_CLOSE) or not calendar.is_business_day(today):
                    return _not_now(conn, args, today, "장중(영업일 09:00~15:20)에만 주문해요.")
                polls = iter(range(15))  # up to ~30 seconds

                def wait() -> bool:
                    if next(polls, None) is None:
                        return False
                    sleep(2)
                    return True

                session, cid = place_order(books, broker, today, args.symbol,
                                           Side(args.side.upper()), args.qty, args.limit, wait=wait)
                if cid is None:
                    print(f"주문 실패: {failure_text(session.anomalies[-1].detail)}")
                    return 1
                o = session.oms.orders[cid]
                print(f"주문 {cid}: {o.status}, 체결 {o.filled_qty}/{o.qty}주")
                for eid in session.executions_of(cid):
                    r = books.ledger.execution_result(eid)
                    print(f"  체결 {eid}: {r.gross:,}원, 수수료 {r.commission:,}, 세금 "
                          f"{r.tx_tax + r.rural_tax:,}, 결제일 {r.settle_date}")
                if o.status is not OrderStatus.FILLED:
                    print("아직 다 체결되지 않았어요. 남은 체결은 장 마감 후 daily가 반영해요.")
                return 0

            if args.cmd == "daily":
                day = args.date or today
                if not calendar.is_business_day(day):
                    return _not_now(conn, args, today, f"{day}는 영업일이 아니에요.")
                if args.date is None and started.time() < DAILY_FROM:
                    return _not_now(conn, args, today, "장 마감 후(15:40 이후)에 실행해 주세요.")
                report = daily_close(books, broker, day)
                print(f"[{day}] 결제 {len(report.settled)}건, 신규 체결 {len(report.booked)}건")
                if report.nav:
                    print(f"NAV: 순자산 {report.nav.net_assets:,}, 기준가 {report.nav.nav_per_1000}")
                for n in report.notes:
                    print(n)
                print("대사 결과:")
                _print_recon(report)
                streak = clean_streak(conn, calendar, day, args.account)
                verdict = "무오류" if report.clean else "불일치 있음"
                print(f"{verdict}. 무오류 연속 영업일 {streak} / {GATE_DAYS}")
                if report.closed:
                    print(f"장부 마감: {day}까지. 이 날짜로는 더 기록되지 않아요.")
                return 0 if report.clean else 3
    except (OpsError, KisError, CalendarNotLoadedError, SchemaError, mg.MigrationError) as e:
        print(f"오류: {e}")
        return 1
    except psycopg.OperationalError as e:
        print(db_error_hint(str(e)))
        return 1
    return 0


def db_error_hint(message: str) -> str:
    """Windows PostgreSQL sends Korean messages that the console shows garbled; explain plainly."""
    if "password" in message or "28P01" in message:
        return ("DB 접속 실패: postgres 계정 비밀번호가 맞지 않아요. PostgreSQL 설치 중 정한 비밀번호를 "
                "넣어 주세요 (pgAdmin 마스터 비밀번호와 다를 수 있어요). 한/영 전환 상태도 확인해 주세요.")
    if "refused" in message.lower() or "10061" in message:
        return ("DB 접속 실패: PostgreSQL 서버가 실행 중이 아니에요. Windows 서비스(services.msc)에서 "
                "postgresql 서비스를 시작해 주세요.")
    return f"DB 접속 실패: {message}"


RUN_UNTIL = dt.time(15, 30)


def _run_intraday(books, broker, today, environ, feed_factory, console, now, prefix="MINISTP_KIS") -> int:
    """Fill notices and console commands share one queue; the runner is the only writer."""
    events = queue.Queue()
    hts_id = environ.get(f"{prefix}_HTS_ID") or input("HTS ID (체결통보 구독용, 저장하지 않아요): ").strip()
    if feed_factory is None:
        from .broker.kis_ws import NoticeFeed

        def feed_factory(q):
            return NoticeFeed(broker.cfg.env, hts_id, broker.approval_key, q)
    feed = feed_factory(events)
    feed.start()
    if console is None:
        def console(q):
            def read():
                while True:
                    try:
                        line = input()
                    except EOFError:
                        q.put(("stop",))
                        return
                    q.put(("command", line, dt.datetime.now()))
            threading.Thread(target=read, daemon=True, name="console").start()
    console(events)
    runner = IntradayRunner(books, broker, today, events, now=now)
    try:
        runner.run(until=RUN_UNTIL)
    finally:
        feed.stop()
    return 0


SAMPLE_DB = "ministp_sample"


def _sample_db(url: str, environ, connect, calendar, days: int, accounts: int = 3) -> int:
    from .sample import SampleError, build_sample
    sample_url = environ.get("MINISTP_SAMPLE_DATABASE_URL") or psycopg.conninfo.make_conninfo(url, dbname=SAMPLE_DB)
    _create_database(sample_url, connect)
    with connect(sample_url, autocommit=True) as conn:
        try:
            s = build_sample(conn, calendar, FeeSchedule.kis_paper(), days=days, accounts=accounts)
        except SampleError as e:
            print(f"오류: {e}")
            return 1
        print(f"샘플 DB '{conn.info.dbname}' 생성: {s.first_day} ~ {s.last_day} 영업일 {s.days}일, "
              f"주문 {s.orders}건(취소 {s.canceled}), 체결 {s.fills}건, 불일치 판단 {s.breaks_resolved}건, "
              f"무오류 연속 {s.streak}일")
        for n in s.notes:
            print(f"  {n}")
        for a in s.others:
            print(f"  계좌 {a.account_id} {a.alias}: {a.opened_on} 개설, 주문 {a.orders}건, 체결 {a.fills}건, "
                  f"거부 {a.refused}건, 불일치 판단 {a.breaks_resolved}건, 무오류 연속 {a.streak}일, 상태 {a.status}")
        if s.others:
            print("화면(ui --sample)의 위쪽 막대에서 계좌를 골라 볼 수 있어요.")
    print("운영 DB(ministp)는 건드리지 않았어요. pgAdmin에서 ministp_sample을 열어 보세요.")
    return 0


def _accounts(conn, args, today: dt.date) -> int:
    """`customer add`, `account list | add | status`."""
    if args.cmd == "customer":
        add_customer(conn, args.customer_id, args.alias)
        print(f"고객 등록: {args.customer_id} {args.alias}")
        return 0
    if args.what == "add":
        a = add_account(conn, args.account_id, args.customer_id, args.alias,
                        credential_ref=args.credential, opened_on=today)
        print(f"계좌 등록: {a.account_id} {a.alias} (고객 {a.customer_id}, 모의투자, 개설일 {a.opened_on})")
        print(f"이 계좌의 키는 환경변수 {a.env_prefix}_APP_KEY, {a.env_prefix}_APP_SECRET, {a.env_prefix}_ACCOUNT에서 읽어요. "
              "없으면 실행할 때 물어보고 저장하지 않아요.")
        print(f"다음: 종목이 등록돼 있으면 open --account {a.account_id} 로 증권사 잔고를 원장 시작값으로 기록해요.")
        return 0
    if args.what == "status":
        a = set_status(conn, args.account_id, args.status, args.effective_from or today, args.reason)
        print(f"계좌 {a.account_id} 상태: {a.status} ({a.status_from}부터). 이전 상태는 이력에 남아 있어요.")
        return 0
    for a in list_accounts(conn):
        n, cash = conn.execute(
            "select (select count(*) from ledger_events where account_id = %(a)s),"
            " (select balance from cash_balance_v where account_id = %(a)s)", {"a": a.account_id}).fetchone()
        print(f"{a.account_id} {a.alias}: 고객 {a.customer_id}, {a.broker} {a.env}, {a.status} ({a.status_from}부터), "
              f"이벤트 {n:,}건, 예수금 {int(cash):,}원, 환경변수 {a.env_prefix}_*")
    return 0


def _dividends(conn, args, calendar) -> int:
    """`dividend list | add | cancel`."""
    if args.what == "add":
        try:
            a = rights.add_dividend(conn, args.symbol, args.record, args.pay, args.per_share, args.note)
        except ArithmeticError:
            raise OpsError(f"주당 배당금은 숫자예요. 받은 값: {args.per_share}") from None
        print(f"권리 등록 {a.ref}: {a.symbol} 현금배당 주당 {rights._won(a.amount_per_share)}원, "
              f"기준일 {a.record_date}, 지급일 {a.pay_date}")
        print(f"각 계좌의 daily가 {a.accrual_date(calendar)}부터 기준일 보유분을 미수배당금으로 인식하고, "
              "지급일에 세금을 떼고 입금으로 기록해요.")
        return 0
    if args.what == "cancel":
        a = rights.cancel_action(conn, args.action_id, args.reason)
        print(f"권리 취소 {a.ref}: {a.symbol} 기준일 {a.record_date}. 등록 행은 그대로 두고 취소 행을 더했어요.")
        print("이미 인식한 계좌는 다음 daily가 인식을 되돌려요.")
        return 0
    actions, booked = rights.list_actions(conn), rights.account_dividends(conn)
    names = dict(conn.execute("select symbol, name from instruments").fetchall())
    for a in actions:
        print(f"{a.ref}  {a.symbol} {names.get(a.symbol, '')}: 주당 {rights._won(a.amount_per_share)}원, "
              f"기준일 {a.record_date}, 지급일 {a.pay_date}" + (f"  [취소: {a.cancel_reason}]" if a.canceled else "")
              + (f"  ({a.note})" if a.note else ""))
        for acct, _, _, qty, accrued, accrued_on, paid_gross, tax, paid_on, canceled_on, status in (
                r for r in booked if r[1] == a.action_id):
            line = f"    {acct}: " + (f"{qty:,}주, 인식 {accrued:,}원 ({accrued_on})" if accrued_on else "인식 없음")
            if status == "PAID":
                line += f", 입금 {paid_gross - tax:,}원 (세전 {paid_gross:,}, 세금 {tax:,}, {paid_on})"
            elif status == "CANCELED":
                line += f", 인식 취소 ({canceled_on})"
            print(line)
    if not actions:
        print("등록된 권리가 없어요. dividend add 종목코드 --record 기준일 --pay 지급일 --per-share 주당배당금")
    return 0


CASH_KIND_NAMES = {"DEPOSIT": "입금", "WITHDRAWAL": "출금", "TRANSFER_IN": "대체 입금", "TRANSFER_OUT": "대체 출금"}


def _cash(conn, args, books, calendar, fees, today: dt.date) -> int:
    """`cash list | deposit | withdraw | transfer` on the books of `--account`."""
    led, acct = books.ledger, books.account_id

    def units(m) -> str:
        return f"좌수 {m.units:+,}좌 (기준가 {m.nav_per_1000} 적용)"

    if args.what == "list":
        print(f"예수금 D+0 {led.cash_balance():,}  결제 후 {led.projected_cash():,}  "
              f"출금가능 {led.withdrawable():,}  좌수 {led.units:,}")
        for m in led.cash_movements():
            other = ""
            if m.transfer_id is not None:
                other = f" TR-{m.transfer_id} " + ("← " if m.amount > 0 else "→ ") + m.counter_account
            print(f"  {m.date}  {CASH_KIND_NAMES[m.kind]}{other}  {m.amount:+,}원  {units(m)}")
        if not led.cash_movements():
            print("입출금 기록이 아직 없어요.")
        return 0
    day = args.date or today
    if args.what == "transfer":
        to = load_books(conn, calendar, fees, args.to)
        t, sent, got = cash.transfer(books, to, day, args.amount, args.memo, today)
        print(f"[{day}] 대체 {t.ref}: {t.from_account} → {t.to_account} {t.amount:,}원")
        print(f"  {t.from_account}: 예수금 {led.cash_balance():,}원, {units(sent)}")
        print(f"  {t.to_account}: 예수금 {to.ledger.cash_balance():,}원, {units(got)}")
    elif args.what == "deposit":
        m = cash.deposit(books, day, args.amount, args.memo, today)
        print(f"[{day}] 입금 {args.amount:,}원 ({acct}): 예수금 {led.cash_balance():,}원, {units(m)}")
    else:
        m = cash.withdraw(books, day, args.amount, args.memo, today)
        print(f"[{day}] 출금 {args.amount:,}원 ({acct}): 예수금 {led.cash_balance():,}원, "
              f"출금가능 {led.withdrawable():,}원, {units(m)}")
    print("증권사 잔고와는 다음 daily의 대사에서 맞춰 봐요. 오늘 기준가를 이미 산출했다면 daily가 다시 산출해요.")
    return 0


def _adjust(conn, args, books, today: dt.date) -> int:
    """`adjust list | add | cancel` on the books of `--account`."""
    from . import codes
    names = {code: name for code, name, *_ in codes.list_codes(conn, "ADJUSTMENT_REASON")}

    def said(a) -> str:
        tax = f"세전 {a.amount:+,}원, 세금 {a.tax:,}원, " if a.tax else ""
        return f"{a.ref} {names.get(a.reason, a.reason)}: {tax}예수금 {a.cash:+,}원"

    if args.what == "list":
        booked = books.ledger.adjustments().values()
        for a in booked:
            print(f"  {a.date}  {said(a)}  ({a.memo})"
                  + (f"  불일치 행 {a.recon_result_id}" if a.recon_result_id is not None else "")
                  + (f"  [취소 {a.canceled_on}: {a.cancel_memo}]" if a.canceled_on else ""))
        if not booked:
            print("수동 조정이 아직 없어요. adjust add 사유 금액 --memo 메모")
        return 0
    day = args.date or today
    if args.what == "add":
        a = adjustments.add(books, day, args.reason, args.amount, args.tax, args.memo, args.result_id, today,
                            confirmed=args.confirm)
        print(f"[{day}] 조정 {said(a)} ({books.account_id})")
    else:
        a = adjustments.cancel(books, args.adjustment_id, day, args.reason, today)
        print(f"[{day}] 조정 취소 {a.ref} ({books.account_id}): 예수금 {-a.cash:+,}원. 조정 전표는 그대로 두고 반대 전표를 더했어요.")
    print(f"예수금 {books.ledger.cash_balance():,}원. 증권사 잔고와는 다음 daily의 대사에서 맞춰 봐요.")
    return 0


def _codes(conn, args) -> int:
    """`code list | rename | group-add | add | use | check`."""
    from . import codes
    if args.what == "rename":
        old = codes.rename(conn, args.group_id, args.code, args.name, args.desc)
        print(f"코드명 변경: {args.group_id} {args.code} '{old}' → '{args.name}'. 코드와 이미 저장된 값은 그대로예요.")
        return 0
    if args.what == "group-add":
        codes.add_group(conn, args.group_id, args.name, args.desc)
        print(f"코드 그룹 등록: {args.group_id} {args.name}. code add {args.group_id} 코드 코드명 으로 코드를 넣어요.")
        return 0
    if args.what == "add":
        codes.add_code(conn, args.group_id, args.code, args.name, args.desc)
        print(f"코드 등록: {args.group_id} {args.code} {args.name}")
        return 0
    if args.what == "use":
        codes.set_in_use(conn, args.group_id, args.code, args.state == "on")
        print(f"{args.group_id} {args.code}: {'사용' if args.state == 'on' else '사용 안 함'}. 코드는 지우지 않아요.")
        return 0
    if args.what == "check":
        problems = codes.check(conn)
        for line in problems:
            print(f"  문제  {line}")
        groups = codes.list_groups(conn)
        if not problems:
            print(f"공통코드 {len(groups)}개 그룹, 코드 {sum(g[5] + g[6] for g in groups)}개가 "
                  "스키마 제약, 프로그램, 저장된 값과 어긋나지 않아요.")
        return 1 if problems else 0
    if args.group_id:
        for code, name, desc, in_use in codes.list_codes(conn, args.group_id):
            print(f"{code}  {name}" + (f"  ({desc})" if desc else "") + ("" if in_use else "  [사용 안 함]"))
        return 0
    for gid, name, _, used_by, fixed, on, off in codes.list_groups(conn):
        print(f"{gid}  {name}: 코드 {on}개" + (f" (끈 코드 {off}개)" if off else "")
              + (f", 쓰는 곳 {used_by}" if used_by else "") + ("" if fixed else ", 운영자 관리"))
    print("계정과목은 원장이 직접 참조하는 코드라서 전용 테이블(accounts_chart)에 있어요.")
    return 0


AUDIT_KEYS = ("tx", "symbol", "side", "qty", "limit_price", "order_no", "date", "reason", "from", "to")


def _audit(conn, args) -> int:
    """`audit`: the latest rows, newest first, then where the chain ends."""
    from . import codes
    found = audit.rows(conn, limit=args.limit, account_id=args.account, action=args.action, outcome=args.outcome)
    action_names = codes.names(conn, "AUDIT_ACTION")
    for r in found:
        about = " ".join(x for x in (r.account_id, r.target) if x)
        said = "  ".join(f"{k}={v}" for k, v in sorted(   # jsonb keeps no key order: the order a person reads in
            r.detail.items(), key=lambda kv: (AUDIT_KEYS.index(kv[0]) if kv[0] in AUDIT_KEYS else len(AUDIT_KEYS), kv[0])))
        print(f"{r.id:>6}  {r.at.astimezone():%Y-%m-%d %H:%M:%S}  {audit.CHANNELS[r.channel]} {r.actor}  "
              f"{audit.OUTCOMES[r.outcome]}  {action_names.get(r.action, r.action)}" + (f"  {about}" if about else ""))
        if said:
            print(f"        {said}")
        if r.message:
            print(f"        → {r.message}")
    n, last, bad = audit.chain_problems(conn)
    if not n:
        print("감사 로그가 아직 비어 있어요.")
        return 0
    if not found:
        print("조건에 맞는 줄이 없어요.")
    if bad:
        print(f"주의: 해시 사슬이 이어지지 않는 줄이 있어요 ({', '.join(map(str, bad))}번). verify로 확인해 주세요.")
        return 1
    print(f"전체 {n:,}줄, 마지막 줄 {n:,}번의 해시 {last[:16]}. 이 값을 따로 적어 두면 나중에 끝이 잘렸는지도 알 수 있어요.")
    return 0


def _print_checks(checks) -> None:
    many = len({c.account for c in checks if c.account}) > 1
    for c in checks:
        who = f"[{c.account}] " if many and c.account else ""
        print(f"  {'OK  ' if c.ok else '문제'}  {who}{c.title}: {c.detail}")


def _verify(url: str, connect, calendar, fees) -> int:
    """Print the integrity report. Takes no writer lock and writes nothing, so it can run beside `run`."""
    from .ops import load_instruments
    from .store.integrity import integrity_report
    with connect(url, autocommit=True) as conn:
        if not mg._has_table(conn, "ledger_events"):
            print("오류: 원장 테이블이 없어요. 먼저 db-init을 실행해 주세요.")
            return 1
        todo = mg.pending(conn, mg.discover())
        if todo:
            print(f"오류: DB 스키마가 이 버전의 코드보다 오래돼서 점검할 수 없어요 (남은 마이그레이션 {len(todo)}개). "
                  "db-migrate --dry-run이 적용한 상태에서 같은 점검을 하고 되돌려요.")
            return 1
        checks = integrity_report(conn, calendar, fees, load_instruments(conn))
        print(f"DB '{conn.info.dbname}' 원장 무결성 점검 (읽기만 해요)")
        _print_checks(checks)
        bad = [c for c in checks if not c.ok]
        if bad:
            print(f"문제 {len(bad)}건. 원장을 쓰는 명령(run, order, daily)을 멈추고 원인을 먼저 확인해 주세요.")
            return 1
        print("이상 없음: 저장된 원장이 이벤트를 처음부터 다시 계산한 결과와 같아요.")
        return 0


def _db_migrate(url: str, connect, calendar, fees, *, dry_run: bool, apply: bool) -> int:
    """Status and plan by default; `--dry-run` rehearses and rolls back; `--apply` rehearses, then applies.

    A rehearsal also runs the ledger integrity checks on the migrated state, so `--apply`
    never changes a database whose ledger would not verify afterwards."""
    from .ops import load_instruments
    from .store import erd
    from .store.integrity import integrity_report

    def ledger_still_verifies(conn) -> str:
        checks = integrity_report(conn, calendar, fees, load_instruments(conn))
        bad = [c for c in checks if not c.ok]
        if bad:
            _print_checks(bad)
            raise mg.MigrationError(f"적용한 상태에서 원장 무결성 점검 {len(bad)}건이 어긋나서 되돌렸어요.")
        accounts = len({c.account for c in checks if c.account})
        return f"원장 무결성 점검 {len(checks)}개 통과 (계좌 {accounts}개)"

    try:
        migrations = mg.discover()
        asked = []   # what --apply was about to apply, for the audit row of a failure
        with connect(url, autocommit=True) as conn, audit.refusals(
                conn, lambda: ("DB_MIGRATE", None, None, {"pending": asked}) if apply and asked else None,
                refused=(OpsError,), failed=(mg.MigrationError, psycopg.Error)):
            todo = mg.pending(conn, migrations)
            asked += [m.label for m in todo]
            done = mg.applied(conn)
            print(f"DB '{conn.info.dbname}': 적용된 마이그레이션 {len(done)}개, 남은 것 {len(todo)}개 "
                  f"(코드의 ERD v{erd.schema_version(erd.schema_sql())})")
            for m in todo:
                print(f"  대기: {m.label}" + (f" → ERD v{m.erd_version}" if m.erd_version else ""))
            if not todo:
                print("적용할 마이그레이션이 없어요.")
                return 0
            if not (dry_run or apply):
                print("리허설은 --dry-run (적용해 보고 되돌려요), 실제 적용은 --apply 예요. 적용 전에 pg_dump로 백업해 주세요.")
                return 0
            acquire_writer(conn)
            verified = []
            for r in mg.migrate(conn, migrations, dry_run=True, after=lambda c: verified.append(ledger_still_verifies(c))):
                print(f"리허설(되돌림): {r.migration.label}")
                for t in r.tables:
                    if t.rows_before is None:
                        print(f"  새 테이블 {t.table}")
                    elif t.unchanged:
                        print(f"  {t.table}: 기존 {t.rows_before:,}행 그대로")
                    else:
                        print(f"  {t.table}: {t.rows_before:,}행 → {t.rows_after if t.rows_after is not None else '없음'} "
                              "(파일에 적힌 의도한 변경)")
            print(f"  적용한 상태에서 {verified[0]}")
            print("리허설 통과: DB는 바뀌지 않았어요.")
            if dry_run:
                return 0
            for r in mg.migrate(conn, migrations):
                print(f"적용: {r.migration.label}")
            print(f"  적용된 DB에서 {ledger_still_verifies(conn)}")
            if audit.has_log(conn):   # from ERD v2.3 on; the migration that creates the log is its first row
                audit.record(conn, "DB_MIGRATE", target=f"ERD v{erd.schema_version(erd.schema_sql())}",
                             detail={"applied": asked})
            print("마이그레이션 완료.")
            return 0
    except (mg.MigrationError, OpsError) as e:
        print(f"오류: {e}")
        return 1


def ui_database_url(url: str, environ, sample: bool) -> str:
    if sample:
        return environ.get("MINISTP_SAMPLE_DATABASE_URL") or psycopg.conninfo.make_conninfo(url, dbname=SAMPLE_DB)
    return environ.get("MINISTP_UI_DATABASE_URL") or url


def _ui(url: str, environ, calendar, fees, sample: bool, port: int, serve=None, write: bool = False) -> int:
    """Screens read on `dsn` (read-only session). 처리 buttons write only on the sample DB or with --write,
    and then through the normal DB URL, the one the CLI writes with."""
    from .ui.app import create_app, sample_today
    from .ui.definitions import DefinitionError
    dsn = ui_database_url(url, environ, sample)
    write_dsn = dsn if sample else url if write else None
    try:
        # 실전 테두리: the sample DB is never a broker's; otherwise the screens follow the same
        # environment variable the commands read, so a shell set up for 실전 opens 실전 screens.
        env = "sample" if sample else trading_env(environ)
        app = create_app(dsn, calendar=calendar, fees=fees, today=sample_today(dsn) if sample else None,
                         label="샘플" if sample else "", write_dsn=write_dsn, env=env)
    except KisError:
        print("오류: MINISTP_KIS_ENV는 paper(모의)나 live(실전)여야 해요.")
        return 1
    except DefinitionError as e:
        print(f"오류: {e}")
        return 1
    mode = "처리 버튼 켜짐" if write_dsn else "조회 전용, 처리 버튼은 ui --write"
    if env == "live":
        print("주의: 실전 환경(MINISTP_KIS_ENV=live)으로 열어요. 모든 화면에 실전 테두리가 보여요.")
    print(f"STP-UI: http://127.0.0.1:{port}  ({mode}. 이 PC에서만 열려요. 끝낼 때 Ctrl+C)")
    if serve is None:
        import uvicorn
        serve = lambda a: uvicorn.run(a, host="127.0.0.1", port=port, log_level="warning",  # noqa: E731
                                     timeout_graceful_shutdown=1)  # live screens keep a stream open
    serve(app)
    return 0


def _create_database(url: str, connect) -> None:
    info = psycopg.conninfo.conninfo_to_dict(url)
    name = info.get("dbname", DB_NAME)
    admin = psycopg.conninfo.make_conninfo(url, dbname="postgres")
    with connect(admin, autocommit=True) as conn:
        if not conn.execute("select 1 from pg_database where datname = %s", (name,)).fetchone():
            conn.execute(f'create database "{name}"')


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
