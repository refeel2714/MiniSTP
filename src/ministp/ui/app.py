"""STP-UI server: screens, the transaction API and generated screen specs.

Every screen reads on a session opened with `default_transaction_read_only=on`.
Nothing is written unless the server was started for it (`write_dsn`), and then
only by a 처리 button: one named 처리 transaction (ui/actions.py) that calls the
same command function as the CLI, on its own short connection that takes the
single-writer lock. Such a request must come from a page this server sent (a
token made at start, same origin, JSON), so no other web page can press a button.
The server answers only on 127.0.0.1/localhost (Host header check against DNS
rebinding) and never shows keys or account numbers.
At start it checks every screen definition against the live schema and refuses
to start if one does not match, so a screen can never drift from the DB.
"""
import asyncio
import datetime as dt
import html
import json
import secrets
import time
from decimal import Decimal
from importlib.resources import files
from urllib.parse import urlsplit

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..calendar import BusinessCalendar, CalendarNotLoadedError
from ..fees import FeeSchedule
from ..ops import OpsError, acquire_writer
from ..store import erd
from .actions import ActionContext, ActionError, run_action
from .definitions import DefinitionError, load_lookups, load_screens, lookup_problems, resolve, validate
from .spec import screen_markdown
from .transactions import TxContext, TxError, run_tx

READ_ONLY = "-c default_transaction_read_only=on"
# 실전 테두리: what the screens are looking at. sample: the practice DB · paper: 모의투자 · live: 실전.
# A live page says so before any script runs: the frame is drawn from an attribute the server writes.
ENVS = {"sample": "샘플", "paper": "모의", "live": "실전"}
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


def _page(title: str, body: str, head: str = "", env: str = "paper") -> HTMLResponse:
    if env == "live":
        title = f"[실전] {title}"       # the browser tab says it too
    return HTMLResponse(f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><link rel="stylesheet" href="/static/stpui.css">{head}</head>
<body data-env="{env}">{body}</body></html>""")


def same_origin(request: Request) -> bool:
    """A browser names the page a POST came from; it has to be this server's own page."""
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        return False
    return request.headers.get("sec-fetch-site", "same-origin") == "same-origin"


def create_app(dsn: str, *, calendar: BusinessCalendar, fees: FeeSchedule, today=None,
               connect=psycopg.connect, allowed_hosts=LOCAL_HOSTS, label: str = "",
               write_dsn: str | None = None, env: str = "paper") -> FastAPI:
    """`today`: a date, or a callable giving one (the sample DB passes its last business day).
    `write_dsn`: where 처리 buttons write. Without it the server is read only and the buttons are off.
    `env`: sample, paper or live (ENVS). Every page of a live server wears the 실전 frame."""
    if env not in ENVS:
        raise ValueError(f"env는 {sorted(ENVS)} 중 하나")
    screens = load_screens()
    lookups = load_lookups()
    token = secrets.token_urlsafe(24)   # only pages this process served know it
    with connect_read_only(dsn, connect) as conn:
        catalog = erd.read_catalog(conn, "public")
        db_name = conn.info.dbname
    problems = validate(screens, catalog, lookups) + lookup_problems(lookups, catalog)
    if problems:
        raise DefinitionError("화면 정의가 DB 스키마와 맞지 않아요:\n  " + "\n  ".join(problems))
    resolved = {sid: resolve(s, catalog, lookups) for sid, s in screens.items()}
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
                "writable": write_dsn is not None, "token": token if write_dsn else "",
                "screen": resolved.get(screen_id) if screen_id else None}
        data |= {"env": env, "env_name": ENVS[env]}        # 실전 테두리: the page says which environment it shows
        # Embedded as JSON in a non-executed script tag; '<' escaped so no value can close the tag.
        return json.dumps(data, ensure_ascii=False, default=str).replace("<", "\\u003c")

    def shell(title: str, screen_id: str | None) -> HTMLResponse:
        return _page(title, f'<div id="app"></div><script id="stpui-data" type="application/json">'
                            f'{shell_data(screen_id)}</script><script src="/static/stpui.js"></script>', env=env)

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
        except (TxError, OpsError, CalendarNotLoadedError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except psycopg.Error as e:
            return JSONResponse({"error": f"DB 오류: {e.diag.message_primary or type(e).__name__}"},
                                status_code=500)
        return {"tx": tx_id, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "meta": {k: to_json(v) for k, v in result.meta.items()},
                "datasets": {name: {"columns": list(ds.columns),
                                    "rows": [[to_json(v) for v in r] for r in ds.rows]}
                             for name, ds in result.datasets.items()}}

    @app.post("/api/action/{tx_id}")
    async def action(tx_id: str, request: Request):
        """A 처리 button. Refused unless the server was started to write and the request is this page's own."""
        if write_dsn is None:
            return JSONResponse({"error": "조회 전용으로 열려 있어요. 처리하려면 ui --write 로 다시 열어 주세요."},
                                status_code=403)
        sent = request.headers.get("x-stpui-token", "")
        if not same_origin(request) or not secrets.compare_digest(sent.encode(), token.encode()):
            return JSONResponse({"error": "이 화면에서 보낸 요청이 아니에요. 화면을 새로 고친 뒤 다시 해 주세요."},
                                status_code=403)
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return JSONResponse({"error": "JSON 요청만 받아요"}, status_code=415)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("params", {}), dict):
            return JSONResponse({"error": "요청 형식이 올바르지 않아요"}, status_code=400)
        started = time.perf_counter()
        try:
            with connect(write_dsn, autocommit=True) as conn:
                acquire_writer(conn)          # one writer at a time: waits its turn behind run, daily, ...
                with conn.transaction():      # all of it or none of it
                    message = run_action(tx_id, ActionContext(conn, calendar, fees, business_day()),
                                         body.get("params", {}))
        except (ActionError, TxError, OpsError, CalendarNotLoadedError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except psycopg.Error as e:
            return JSONResponse({"error": f"DB 오류: {e.diag.message_primary or type(e).__name__}"},
                                status_code=500)
        return {"tx": tx_id, "message": message, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}

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
