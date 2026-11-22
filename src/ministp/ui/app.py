"""STP-UI server: screens, the transaction API and generated screen specs.

Read only, end to end: every DB session is opened with
`default_transaction_read_only=on`, the transactions only read, and writes stay
with the CLI (single writer). The server answers only on 127.0.0.1/localhost
(Host header check against DNS rebinding) and never shows keys or account numbers.
At start it checks every screen definition against the live schema and refuses
to start if one does not match, so a screen can never drift from the DB.
"""
import asyncio
import datetime as dt
import html
import json
import time
from decimal import Decimal
from importlib.resources import files

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..calendar import BusinessCalendar
from ..fees import FeeSchedule
from ..ops import OpsError
from ..store import erd
from .definitions import DefinitionError, load_screens, resolve, validate
from .spec import screen_markdown
from .transactions import TxContext, TxError, run_tx

READ_ONLY = "-c default_transaction_read_only=on"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]


def connect_read_only(dsn: str, connect=psycopg.connect) -> psycopg.Connection:
    return connect(dsn, autocommit=True, options=READ_ONLY)


def to_json(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (dict, list)):
        return v
    return v


# Cheap markers that move whenever the single writer records anything a screen shows.
HEAD_SQL = """select (select coalesce(max(seq), 0) from ledger_events),
                     (select count(*) from oms_orders),
                     (select count(*) from oms_order_acks),
                     (select coalesce(max(snapshot_id), 0) from broker_snapshots),
                     (select coalesce(max(id), 0) from recon_results)"""
HEAD_NAMES = ("ledger_seq", "orders", "acks", "snapshot", "recon")


def read_head(conn: psycopg.Connection) -> dict:
    return dict(zip(HEAD_NAMES, conn.execute(HEAD_SQL).fetchone()))


async def live_events(read, *, interval: float = 1.0, keepalive: float = 15.0, disconnected=None,
                      sleep=asyncio.sleep, limit: int | None = None):
    """Server-sent events: one `change` when the DB head moves (and one at start), comments as keep-alive.

    No trigger or schema change is needed (the gate period forbids one): the
    stream polls a few max()/count() markers. `read` is a blocking call, run off
    the event loop.
    """
    last, quiet, sent = None, 0.0, 0
    while limit is None or sent < limit:
        if disconnected and await disconnected():
            return
        head = await asyncio.to_thread(read)
        if head != last:
            last, quiet = head, 0.0
            sent += 1
            yield f"event: change\ndata: {json.dumps(head)}\n\n"
        elif quiet >= keepalive:
            quiet = 0.0
            yield ": keep-alive\n\n"
        await sleep(interval)
        quiet += interval


def _page(title: str, body: str, head: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><link rel="stylesheet" href="/static/stpui.css">{head}</head>
<body>{body}</body></html>""")


def create_app(dsn: str, *, calendar: BusinessCalendar, fees: FeeSchedule, today=None,
               connect=psycopg.connect, allowed_hosts=LOCAL_HOSTS, label: str = "") -> FastAPI:
    """`today`: a date, or a callable giving one (the sample DB passes its last business day)."""
    screens = load_screens()
    with connect_read_only(dsn, connect) as conn:
        catalog = erd.read_catalog(conn, "public")
        db_name = conn.info.dbname
    problems = validate(screens, catalog)
    if problems:
        raise DefinitionError("화면 정의가 DB 스키마와 맞지 않아요:\n  " + "\n  ".join(problems))
    resolved = {sid: resolve(s, catalog) for sid, s in screens.items()}
    menu = [{"id": s.id, "title": s.title} for s in screens.values()]
    business_day = today if callable(today) else (lambda: today or dt.date.today())

    app = FastAPI(title="STP-UI", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:"
        return resp

    app.mount("/static", StaticFiles(directory=str(files("ministp.ui").joinpath("static"))), name="static")

    def shell_data(screen_id: str | None) -> str:
        data = {"menu": menu, "today": business_day().isoformat(), "db": db_name, "label": label,
                "screen": resolved.get(screen_id) if screen_id else None}
        # Embedded as JSON in a non-executed script tag; '<' escaped so no value can close the tag.
        return json.dumps(data, ensure_ascii=False, default=str).replace("<", "\\u003c")

    def shell(title: str, screen_id: str | None) -> HTMLResponse:
        return _page(title, f'<div id="app"></div><script id="stpui-data" type="application/json">'
                            f'{shell_data(screen_id)}</script><script src="/static/stpui.js"></script>')

    @app.get("/", response_class=HTMLResponse)
    def home():
        return shell("STP-UI", None)

    @app.get("/screen/{screen_id}", response_class=HTMLResponse)
    def screen(screen_id: str):
        if screen_id not in resolved:
            raise HTTPException(404, f"없는 화면: {screen_id}")
        return shell(f"{screen_id} {screens[screen_id].title}", screen_id)

    @app.get("/api/screen/{screen_id}")
    def screen_def(screen_id: str):
        if screen_id not in resolved:
            raise HTTPException(404, f"없는 화면: {screen_id}")
        return resolved[screen_id]

    @app.get("/spec/{screen_id}", response_class=PlainTextResponse)
    def spec(screen_id: str):
        if screen_id not in screens:
            raise HTTPException(404, f"없는 화면: {screen_id}")
        return PlainTextResponse(screen_markdown(screens[screen_id], catalog),
                                 media_type="text/markdown; charset=utf-8")

    @app.post("/api/tx/{tx_id}")
    async def tx(tx_id: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        params = body.get("params", {}) if isinstance(body, dict) else {}
        started = time.perf_counter()
        try:
            with connect_read_only(dsn, connect) as conn:
                result = run_tx(tx_id, TxContext(conn, calendar, fees, business_day()), params)
        except (TxError, OpsError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except psycopg.Error as e:
            return JSONResponse({"error": f"DB 오류: {e.diag.message_primary or type(e).__name__}"},
                                status_code=500)
        return {"tx": tx_id, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "meta": {k: to_json(v) for k, v in result.meta.items()},
                "datasets": {name: {"columns": list(ds.columns),
                                    "rows": [[to_json(v) for v in r] for r in ds.rows]}
                             for name, ds in result.datasets.items()}}

    @app.get("/api/live")
    async def live(request: Request):
        conn = connect_read_only(dsn, connect)

        async def stream():
            try:
                async for chunk in live_events(lambda: read_head(conn), disconnected=request.is_disconnected):
                    yield chunk
            finally:
                conn.close()
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    return app


def sample_today(dsn: str, connect=psycopg.connect):
    """The sample DB lives in the past: 'today' is its last booked business day."""
    def day() -> dt.date:
        with connect_read_only(dsn, connect) as conn:
            row = conn.execute("select max((payload->>'date')::date) from ledger_events"
                               " where event_type = 'NavEvent'").fetchone()
        return row[0] or dt.date.today()
    return day
