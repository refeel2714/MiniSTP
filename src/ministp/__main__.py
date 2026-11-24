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
    python -m ministp audit                       # 감사 로그: 누가 언제 무엇을 했고 어떻게 끝났는지 (거부·실패 포함)
    python -m ministp master fetch                # 시장 전체 종목정보(KIS 마스터 파일) 받아 두기, 키 필요 없음
    python -m ministp master check                # 받은 종목정보 점검: 전일 대비, 등록 종목, 기준가 대조 (읽기 전용)
    python -m ministp erp export --from 2026-11-18  # 분개를 ERP 일반전표 업로드 CSV로 (읽기 전용)

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

from . import audit
from .broker import KisClient, KisConfig, KisError, trading_env
from .calendar import CalendarNotLoadedError
from .cli import ask_missing, krx_calendar
from .fees import FeeSchedule
from .instruments import AssetType, Instrument, Market
from .ledger import Side
from .oms import OrderStatus
from .accounts import STATUSES, add_account, add_customer, list_accounts, set_status
from .ops import (GATE_DAYS, IntradayRunner, OpsError, acquire_writer, add_instrument, cancel_order, clean_streak, close_books, daily_close, load_books, open_books,
                  place_order, require_current_schema, resolve_break)
from .recon import Cause, Status
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


def _erp_export(url: str, connect, args, today: dt.date) -> int:
    from . import erp
    date_from = args.date_from or today
    date_to = args.date_to or date_from
    try:
        mapping = erp.load_mapping(args.mapping) if args.mapping else None
        with connect(url) as conn:
            result = erp.export(conn, mapping, date_from, date_to, args.by)
            conn.rollback()
        if not result.lines:
            print(f"{date_from}~{date_to}에 내보낼 분개가 없어요.")
            return 0
        path = erp.write_csv(result, args.out or erp.default_out(date_from, date_to, args.by))
    except erp.ErpError as e:
        print(f"내보내지 않았어요: {e}")
        return 1
    kind = "건별" if args.by == "entry" else "일별 합계"
    print(f"{path} ({kind}): 전표 {result.documents}건, 줄 {len(result.lines)}개, "
          f"차변 {result.debit:,} = 대변 {result.credit:,}")
    print("계정별 합계 (ERP 총계정원장과 맞춰 볼 값):")
    for code, name, dr, cr in result.by_account():
        print(f"  {code:10} {name:24} 차변 {dr:>15,}  대변 {cr:>15,}")
    return 0


def run(argv, **kw) -> int:
    """Every audit row this command writes: from the console, by whoever is logged in to this PC."""
    with audit.acting_as("CLI"):
        return _run(argv, **kw)


def _run(argv, *, environ=os.environ, client_factory=KisClient, connect=psycopg.connect,
         now=dt.datetime.now, sleep=time.sleep, feed_factory=None, console=None, master_download=None) -> int:
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
    p = sub.add_parser("cancel", parents=[by_account],
                       help="오늘 낸 주문의 미체결 잔량 취소 (run이 떠 있으면 run 창에서 cancel)")
    p.add_argument("order", help="주문 번호: 3, S20261124-0003, 또는 증권사 주문번호")
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
    p = sub.add_parser("master", help="시장 전체 종목정보: KIS 마스터 파일 받기(fetch)와 점검(check). DB에 쓰지 않아요")
    p.add_argument("action", choices=["fetch", "check"])
    p.add_argument("--date", type=dt.date.fromisoformat, help="check: 점검할 날 (기본은 가장 최근에 받은 날)")
    p.add_argument("--csv", action="store_true", help="엑셀로 열 수 있게 받은 폴더에 master.csv도 만들기")
    p.add_argument("--no-db", action="store_true", help="check: 등록 종목·기준가 대조 없이 파일만 점검")
    p = sub.add_parser("erp", help="분개를 ERP 일반전표 업로드 파일(CSV)로 내보내기. DB에 쓰지 않아요 (R-96)")
    p.add_argument("action", choices=["export"])
    p.add_argument("--from", dest="date_from", type=dt.date.fromisoformat, help="시작일 (기본은 오늘)")
    p.add_argument("--to", dest="date_to", type=dt.date.fromisoformat, help="종료일 (기본은 시작일)")
    p.add_argument("--by", choices=["entry", "day"], default="entry", help="entry: 분개 1건 = 전표 1건, day: 하루 합계 (일 마감한 날만)")
    p.add_argument("--mapping", help="회사 계정코드 매핑 YAML (없으면 MiniSTP 계정 그대로). 예시: docs/erp/mapping.example.yaml")
    p.add_argument("--out", help="저장할 CSV 경로 (기본 erp-export/전표_날짜_건별.csv)")
    p.add_argument("--sample", action="store_true", help="샘플 DB(ministp_sample)에서 내보내기")
    p = sub.add_parser("ui", help="업무화면(STP-UI)을 이 PC에서만 열기 (기본은 조회 전용)")
    p.add_argument("--sample", action="store_true", help="샘플 DB(ministp_sample)로 열기. 연습용이라 처리 버튼도 켜져요")
    p.add_argument("--write", action="store_true", help="운영 DB에서 화면의 처리 버튼을 켜기 (없으면 조회 전용)")
    p.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    started = now()
    today = started.date()
    calendar = krx_calendar()
    fees = FeeSchedule.kis_paper()
    if args.cmd == "master" and (args.action == "fetch" or args.no_db):
        return _master(args, environ, None, connect, calendar, fees, started, master_download)
    url = database_url(environ)

    try:
        if args.cmd == "master":
            return _master(args, environ, url, connect, calendar, fees, started, master_download)

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

        if args.cmd == "erp":
            target = url
            if args.sample:
                target = (environ.get("MINISTP_SAMPLE_DATABASE_URL")
                          or psycopg.conninfo.make_conninfo(url, dbname=SAMPLE_DB))
            return _erp_export(target, connect, args, today)

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

            if args.cmd in ("open", "order", "cancel", "daily", "close", "run", "resolve", "customer") or (
                    args.cmd == "account" and args.what != "list") or (
                    args.cmd == "code" and args.what not in ("list", "check")):
                acquire_writer(conn)

            if args.cmd == "code":
                require_current_schema(conn)
                return _codes(conn, args)

            if args.cmd in ("customer", "account"):
                require_current_schema(conn)
                return _accounts(conn, args, today)

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
                print(f"예수금 D+0 {led.cash_balance():,}  결제 후 {led.projected_cash():,}")
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

            if args.cmd == "cancel":
                if not (MARKET_OPEN <= started.time() <= MARKET_CLOSE) or not calendar.is_business_day(today):
                    print("장중(영업일 09:00~15:20)에만 취소해요.")
                    return 2
                polls = iter(range(10))  # up to ~20 seconds

                def wait() -> bool:
                    if next(polls, None) is None:
                        return False
                    sleep(2)
                    return True

                session, cid = cancel_order(books, broker, today, args.order, wait=wait)
                o = session.oms.orders[cid]
                if o.status is OrderStatus.CANCELED:
                    print(f"주문 {cid} 취소 확인: 체결 {o.filled_qty}주, 취소 {o.qty - o.filled_qty}주")
                elif o.status is OrderStatus.FILLED:
                    print(f"주문 {cid}는 취소 전에 모두 체결됐어요 ({o.filled_qty}주). 체결을 원장에 반영했어요.")
                else:
                    print(f"주문 {cid} 취소 요청을 증권사가 받았어요. 확인은 장중 run이나 장 마감 후 daily가 반영해요.")
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


def _master(args, environ, url, connect, calendar, fees, started, download=None) -> int:
    """R-90~92: market master files. Reads the DB only for `check` and never writes to it."""
    import urllib.error

    from . import master
    from .pricecheck import closes_by_day
    root = master.default_dir(environ)
    try:
        if args.action == "fetch":
            got = master.fetch(root, started.date(), started, download or master.urllib_download)
            rows = ", ".join(f"{m} {n:,}종목" for m, n in got.rows.items())
            print(f"{'받음' if got.changed else '오늘 받은 파일과 같아요'}: {got.folder} ({rows})")
            if args.csv:
                master.write_csv(master.load_day(root, started.date()), got.folder / "master.csv")
                print(f"엑셀용: {got.folder / 'master.csv'}")
            print("점검: python -m ministp master check")
            return 0

        days = master.kept_days(root)
        day = args.date or (days[-1] if days else None)
        if day is None or day not in days:
            print(f"오류: {root}에 {'받은 파일이 없어요' if day is None else f'{day} 파일이 없어요'}. "
                  "먼저 python -m ministp master fetch")
            return 1
        rows = master.load_day(root, day)
        prev_day = next((d for d in reversed(days) if d < day), None)
        prev = master.load_day(root, prev_day) if prev_day else None
        instruments, held, closes = {}, set(), {}
        if url is not None:
            with connect(url, autocommit=True) as conn:
                books = load_books(conn, calendar, fees)
            instruments, held = books.instruments, set(books.ledger.positions())
            closes = {d: v[0] or v[1] for d, v in closes_by_day(books.ledger).items()}
        rep = master.check(day, rows, prev_day, prev, instruments, held, closes)
        if args.csv:
            master.write_csv(rows, root / day.isoformat() / "master.csv")
    except (master.MasterError, OSError, urllib.error.URLError) as e:
        print(f"오류: {e}")
        return 1

    print(f"시장 종목정보 {day} (KIS 마스터 파일, 읽기만 해요)")
    for (mkt, group), n in sorted(rep.counts.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print(f"  {mkt:6} {master.ljust_wide(group, 16)} {n:>6,}")
    print("  상태: " + ", ".join(f"{k} {v:,}" for k, v in rep.flags.items()))
    if prev is None:
        print("전일 대비: 비교할 이전 파일이 없어요 (내일부터 비교해요)")
    else:
        print(f"전일 대비 ({prev_day}): 신규 {len(rep.new)}, 빠짐 {len(rep.gone)}, 이름 변경 {len(rep.renamed)}")
        for r in rep.new[:20]:
            print(f"  + {r.symbol} {r.name} ({r.market.value} {r.group_name})")
        for r in rep.gone[:20]:
            print(f"  - {r.symbol} {r.name} ({r.market.value} {r.group_name})")
        for a, b in rep.renamed[:20]:
            print(f"  ~ {a.symbol} {a.name} → {b.name}")
    if url is None:
        print("등록 종목·기준가 대조: 건너뜀 (--no-db)")
    for f in rep.findings:
        print(f"  [{f.level}] {f.topic}: {f.text}")
    print(f"결과: {rep.level}")
    return 1 if rep.level == master.BAD else 0


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
