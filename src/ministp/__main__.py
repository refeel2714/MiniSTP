"""MiniSTP command line, run on the owner's PC.

    python -m ministp db-init                     # 처음 한 번: DB와 테이블 만들기
    python -m ministp instrument 069500 "KODEX 200" --etf
    python -m ministp open                        # 처음 한 번: 증권사 잔고를 원장 시작값으로
    python -m ministp run                         # 장중: 실시간 체결 반영 + 주문 콘솔 (IBOR)
    python -m ministp order 069500 buy 1          # 장중: 시장가 주문 1건 (--limit 가격)
    python -m ministp daily                       # 매 영업일 장 마감 후(15:40~): 결제, NAV, 대사
    python -m ministp status                      # 보유, 예수금, NAV, 무오류 연속일
    python -m ministp resolve 12 FEE_DIFF "메모"   # 불일치 판단 기록 (새 행으로 추가)
    python -m ministp sample-db                   # 데모용 샘플 DB(ministp_sample) 만들기, 운영 DB와 별개
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

from .broker import KisClient, KisConfig, KisError, trading_env
from .calendar import CalendarNotLoadedError
from .cli import ask_missing, krx_calendar
from .fees import FeeSchedule
from .instruments import AssetType, Instrument, Market
from .ledger import Side
from .oms import OrderStatus
from .ops import (GATE_DAYS, IntradayRunner, OpsError, acquire_writer, add_instrument, cancel_order, clean_streak, daily_close, load_books, open_books,
                  place_order, resolve_break)
from .recon import Cause, Status
from .session import failure_text
from .store import PostgresLedgerStore

DB_NAME = "ministp"
MARKET_OPEN, MARKET_CLOSE, DAILY_FROM = dt.time(9, 0), dt.time(15, 20), dt.time(15, 40)


def database_url(environ) -> str:
    url = environ.get("MINISTP_DATABASE_URL")
    if url:
        return url
    pw = getpass.getpass("PostgreSQL postgres 계정 비밀번호 (화면에 표시되지 않아요): ").strip()
    return f"postgresql://postgres:{urllib.parse.quote(pw, safe='')}@localhost:5432/{DB_NAME}"


def _broker(environ, client_factory):
    cfg = KisConfig.from_env(ask_missing(environ))
    if cfg.env != "paper":
        raise OpsError("Phase 4 전까지는 모의투자 서버에서만 실행해요.")
    return client_factory(cfg)


def _print_recon(report) -> None:
    for r in report.results:
        mark = "OK   " if r.status is Status.MATCH else "BREAK"
        print(f"  {mark} {r.item:15} {r.key:10} 내부 {r.internal:>15,}  증권사 {r.broker:>15,}"
              + (f"  차이 {r.diff:,} ({r.cause})" if r.status is Status.BREAK else ""))
    for a in report.anomalies:
        print(f"  이상: {a.kind} {a.ref} {a.detail}")


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


def run(argv, *, environ=os.environ, client_factory=KisClient, connect=psycopg.connect,
        now=dt.datetime.now, sleep=time.sleep, feed_factory=None, console=None, master_download=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ministp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("db-init")
    p = sub.add_parser("instrument")
    p.add_argument("symbol"), p.add_argument("name")
    kind = p.add_mutually_exclusive_group(required=True)
    kind.add_argument("--etf", action="store_true"), kind.add_argument("--stock", action="store_true")
    p.add_argument("--kosdaq", action="store_true")
    sub.add_parser("open")
    sub.add_parser("run")
    p = sub.add_parser("order")
    p.add_argument("symbol"), p.add_argument("side", choices=["buy", "sell"])
    p.add_argument("qty", type=int), p.add_argument("--limit", type=int)
    p = sub.add_parser("cancel", help="오늘 낸 주문의 미체결 잔량 취소 (run이 떠 있으면 run 창에서 cancel)")
    p.add_argument("order", help="주문 번호: 3, S20261124-0003, 또는 증권사 주문번호")
    p = sub.add_parser("daily")
    p.add_argument("--date", type=dt.date.fromisoformat, help="지난 영업일을 다시 대사할 때")
    sub.add_parser("status")
    p = sub.add_parser("resolve")
    p.add_argument("result_id", type=int), p.add_argument("cause", choices=[c.value for c in Cause])
    p.add_argument("note")
    p = sub.add_parser("sample-db")
    p.add_argument("--days", type=int, default=40, help="시뮬레이션할 영업일 수")
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
            return _sample_db(url, environ, connect, calendar, args.days)

        if args.cmd == "ui":
            return _ui(url, environ, calendar, fees, args.sample, args.port, write=args.write)

        if args.cmd == "db-migrate":
            target = url
            if args.sample:
                target = (environ.get("MINISTP_SAMPLE_DATABASE_URL")
                          or psycopg.conninfo.make_conninfo(url, dbname=SAMPLE_DB))
            return _db_migrate(target, connect, dry_run=args.dry_run, apply=args.apply)

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

        with connect(url, autocommit=True) as conn:
            if args.cmd == "instrument":
                market = Market.KOSDAQ if args.kosdaq else Market.KOSPI
                asset = AssetType.ETF if args.etf else AssetType.STOCK
                add_instrument(conn, Instrument(args.symbol, args.name, market, asset))
                print(f"등록: {args.symbol} {args.name} ({market.value} {asset.value})")
                return 0

            if args.cmd in ("open", "order", "cancel", "daily", "run", "resolve"):
                acquire_writer(conn)

            if args.cmd == "resolve":
                rid = resolve_break(conn, args.result_id, Cause(args.cause), args.note)
                print(f"판단 기록 완료 (행 {rid}).")
                return 0

            books = load_books(conn, calendar, fees)

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
                print(f"무오류 연속 영업일: {clean_streak(conn, calendar, last)} / {GATE_DAYS}")
                return 0

            broker = _broker(environ, client_factory)

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
                return _run_intraday(books, broker, today, environ, feed_factory, console, now)

            if args.cmd == "order":
                if not (MARKET_OPEN <= started.time() <= MARKET_CLOSE) or not calendar.is_business_day(today):
                    print("장중(영업일 09:00~15:20)에만 주문해요.")
                    return 2
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
                    print(f"{day}는 영업일이 아니에요.")
                    return 2
                if args.date is None and started.time() < DAILY_FROM:
                    print("장 마감 후(15:40 이후)에 실행해 주세요.")
                    return 2
                report = daily_close(books, broker, day)
                print(f"[{day}] 결제 {len(report.settled)}건, 신규 체결 {len(report.booked)}건")
                if report.nav:
                    print(f"NAV: 순자산 {report.nav.net_assets:,}, 기준가 {report.nav.nav_per_1000}")
                for n in report.notes:
                    print(n)
                print("대사 결과:")
                _print_recon(report)
                streak = clean_streak(conn, calendar, day)
                verdict = "무오류" if report.clean else "불일치 있음"
                print(f"{verdict}. 무오류 연속 영업일 {streak} / {GATE_DAYS}")
                return 0 if report.clean else 3
    except (OpsError, KisError, CalendarNotLoadedError) as e:
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


def _run_intraday(books, broker, today, environ, feed_factory, console, now) -> int:
    """Fill notices and console commands share one queue; the runner is the only writer."""
    events = queue.Queue()
    hts_id = environ.get("MINISTP_KIS_HTS_ID") or input("HTS ID (체결통보 구독용, 저장하지 않아요): ").strip()
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


def _sample_db(url: str, environ, connect, calendar, days: int) -> int:
    from .sample import SampleError, build_sample
    sample_url = environ.get("MINISTP_SAMPLE_DATABASE_URL") or psycopg.conninfo.make_conninfo(url, dbname=SAMPLE_DB)
    _create_database(sample_url, connect)
    with connect(sample_url, autocommit=True) as conn:
        try:
            s = build_sample(conn, calendar, FeeSchedule.kis_paper(), days=days)
        except SampleError as e:
            print(f"오류: {e}")
            return 1
        print(f"샘플 DB '{conn.info.dbname}' 생성: {s.first_day} ~ {s.last_day} 영업일 {s.days}일, "
              f"주문 {s.orders}건(취소 {s.canceled}), 체결 {s.fills}건, 불일치 판단 {s.breaks_resolved}건, "
              f"무오류 연속 {s.streak}일")
        for n in s.notes:
            print(f"  {n}")
    print("운영 DB(ministp)는 건드리지 않았어요. pgAdmin에서 ministp_sample을 열어 보세요.")
    return 0


def _verify(url: str, connect, calendar, fees) -> int:
    """Print the integrity report. Takes no writer lock and writes nothing, so it can run beside `run`."""
    from .ops import load_instruments
    from .store.integrity import integrity_report
    with connect(url, autocommit=True) as conn:
        try:
            checks = integrity_report(conn, calendar, fees, load_instruments(conn))
        except psycopg.errors.UndefinedTable:
            print("오류: 원장 테이블이 없어요. 먼저 db-init을 실행해 주세요.")
            return 1
        print(f"DB '{conn.info.dbname}' 원장 무결성 점검 (읽기만 해요)")
        for c in checks:
            print(f"  {'OK  ' if c.ok else '문제'}  {c.title}: {c.detail}")
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


def _db_migrate(url: str, connect, *, dry_run: bool, apply: bool) -> int:
    """Status and plan by default; `--dry-run` rehearses and rolls back; `--apply` changes the database."""
    from .store import erd, migrate as mg
    try:
        migrations = mg.discover()
        with connect(url, autocommit=True) as conn:
            todo = mg.pending(conn, migrations)
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
            for r in mg.migrate(conn, migrations, dry_run=dry_run):
                print(f"{'적용' if r.applied else '리허설(되돌림)'}: {r.migration.label}")
                for t in r.tables:
                    if t.rows_before is None:
                        print(f"  새 테이블 {t.table}")
                    elif t.unchanged:
                        print(f"  {t.table}: 기존 {t.rows_before:,}행 그대로")
                    else:
                        print(f"  {t.table}: {t.rows_before:,}행 → {t.rows_after if t.rows_after is not None else '없음'} "
                              "(파일에 적힌 의도한 변경)")
            print("리허설 통과: DB는 바뀌지 않았어요." if dry_run else "마이그레이션 완료.")
            return 0
    except mg.MigrationError as e:
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
