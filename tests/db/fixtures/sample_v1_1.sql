--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: ministp_check_entry_balance(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ministp_check_entry_balance() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
declare
    eid bigint := new.entry_id;
    dr bigint; cr bigint; n int;
begin
    select coalesce(sum(debit), 0), coalesce(sum(credit), 0), count(*)
      into dr, cr, n from journal_lines where entry_id = eid;
    if n = 0 or dr <> cr then
        raise exception 'journal entry % unbalanced: % lines, debit %, credit %', eid, n, dr, cr
            using errcode = 'check_violation';
    end if;
    return null;
end $$;


--
-- Name: ministp_forbid_change(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ministp_forbid_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
begin
    raise exception 'append-only table %: % is not allowed', tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: accounts_chart; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts_chart (
    code text NOT NULL,
    name text NOT NULL,
    normal_side character(1) NOT NULL,
    CONSTRAINT accounts_chart_normal_side_check CHECK ((normal_side = ANY (ARRAY['D'::bpchar, 'C'::bpchar])))
);


--
-- Name: TABLE accounts_chart; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.accounts_chart IS '계정과목';


--
-- Name: COLUMN accounts_chart.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.accounts_chart.code IS '계정코드';


--
-- Name: COLUMN accounts_chart.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.accounts_chart.name IS '계정명';


--
-- Name: COLUMN accounts_chart.normal_side; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.accounts_chart.normal_side IS '정상잔액 방향 (D 차변 / C 대변)';


--
-- Name: journal_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal_lines (
    line_id bigint NOT NULL,
    entry_id bigint NOT NULL,
    account_code text NOT NULL,
    debit bigint DEFAULT 0 NOT NULL,
    credit bigint DEFAULT 0 NOT NULL,
    CONSTRAINT journal_lines_check CHECK (((debit = 0) <> (credit = 0))),
    CONSTRAINT journal_lines_credit_check CHECK ((credit >= 0)),
    CONSTRAINT journal_lines_debit_check CHECK ((debit >= 0))
);


--
-- Name: TABLE journal_lines; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.journal_lines IS '분개 명세 (차변·대변 라인)';


--
-- Name: COLUMN journal_lines.line_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_lines.line_id IS '라인 번호';


--
-- Name: COLUMN journal_lines.entry_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_lines.entry_id IS '전표 번호';


--
-- Name: COLUMN journal_lines.account_code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_lines.account_code IS '계정코드';


--
-- Name: COLUMN journal_lines.debit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_lines.debit IS '차변 금액';


--
-- Name: COLUMN journal_lines.credit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_lines.credit IS '대변 금액';


--
-- Name: account_balances_v; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.account_balances_v AS
 SELECT a.code,
    a.name,
    a.normal_side,
        CASE
            WHEN (a.normal_side = 'D'::bpchar) THEN COALESCE(sum((l.debit - l.credit)), (0)::numeric)
            ELSE COALESCE(sum((l.credit - l.debit)), (0)::numeric)
        END AS balance
   FROM (public.accounts_chart a
     LEFT JOIN public.journal_lines l ON ((l.account_code = a.code)))
  GROUP BY a.code, a.name, a.normal_side;


--
-- Name: VIEW account_balances_v; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.account_balances_v IS '계정별 잔액 (시산표)';


--
-- Name: COLUMN account_balances_v.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.account_balances_v.code IS '계정코드';


--
-- Name: COLUMN account_balances_v.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.account_balances_v.name IS '계정명';


--
-- Name: COLUMN account_balances_v.normal_side; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.account_balances_v.normal_side IS '정상잔액 방향';


--
-- Name: COLUMN account_balances_v.balance; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.account_balances_v.balance IS '잔액';


--
-- Name: broker_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.broker_snapshots (
    snapshot_id bigint NOT NULL,
    date date NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    taken_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE broker_snapshots; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.broker_snapshots IS '증권사 응답 원본 (대사 실행, 체결통보, 원장 시작)';


--
-- Name: COLUMN broker_snapshots.snapshot_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broker_snapshots.snapshot_id IS '스냅샷 번호';


--
-- Name: COLUMN broker_snapshots.date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broker_snapshots.date IS '기준일';


--
-- Name: COLUMN broker_snapshots.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broker_snapshots.kind IS '유형 (OPENING_BALANCE / DAILY_RECON / INTRADAY_RECON / FILL_NOTICE)';


--
-- Name: COLUMN broker_snapshots.payload; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broker_snapshots.payload IS '응답 원본';


--
-- Name: COLUMN broker_snapshots.taken_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broker_snapshots.taken_at IS '수신 시각';


--
-- Name: broker_snapshots_snapshot_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.broker_snapshots_snapshot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: broker_snapshots_snapshot_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.broker_snapshots_snapshot_id_seq OWNED BY public.broker_snapshots.snapshot_id;


--
-- Name: cash_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cash_ledger (
    id bigint NOT NULL,
    event_seq bigint NOT NULL,
    date date NOT NULL,
    reason text NOT NULL,
    amount bigint NOT NULL,
    ref_id text NOT NULL
);


--
-- Name: TABLE cash_ledger; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.cash_ledger IS '예수금 원장 (결제일 기준)';


--
-- Name: COLUMN cash_ledger.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cash_ledger.id IS '행 번호';


--
-- Name: COLUMN cash_ledger.event_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cash_ledger.event_seq IS '원장 이벤트 순번';


--
-- Name: COLUMN cash_ledger.date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cash_ledger.date IS '일자';


--
-- Name: COLUMN cash_ledger.reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cash_ledger.reason IS '사유 (입금, 결제 등)';


--
-- Name: COLUMN cash_ledger.amount; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cash_ledger.amount IS '금액 (+입금 / −출금)';


--
-- Name: COLUMN cash_ledger.ref_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cash_ledger.ref_id IS '참조 ID';


--
-- Name: cash_balance_v; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.cash_balance_v AS
 SELECT COALESCE(sum(amount), (0)::numeric) AS balance
   FROM public.cash_ledger;


--
-- Name: VIEW cash_balance_v; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.cash_balance_v IS '예수금 잔액';


--
-- Name: COLUMN cash_balance_v.balance; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cash_balance_v.balance IS '잔액';


--
-- Name: cash_ledger_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.cash_ledger_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cash_ledger_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.cash_ledger_id_seq OWNED BY public.cash_ledger.id;


--
-- Name: instruments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instruments (
    symbol text NOT NULL,
    name text NOT NULL,
    market text NOT NULL,
    asset_type text NOT NULL,
    CONSTRAINT instruments_asset_type_check CHECK ((asset_type = ANY (ARRAY['STOCK'::text, 'ETF'::text]))),
    CONSTRAINT instruments_market_check CHECK ((market = ANY (ARRAY['KOSPI'::text, 'KOSDAQ'::text])))
);


--
-- Name: TABLE instruments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.instruments IS '종목 마스터';


--
-- Name: COLUMN instruments.symbol; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.instruments.symbol IS '종목코드';


--
-- Name: COLUMN instruments.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.instruments.name IS '종목명';


--
-- Name: COLUMN instruments.market; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.instruments.market IS '시장 (KOSPI / KOSDAQ)';


--
-- Name: COLUMN instruments.asset_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.instruments.asset_type IS '자산 유형 (STOCK / ETF)';


--
-- Name: journal_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal_entries (
    entry_id bigint NOT NULL,
    event_seq bigint NOT NULL,
    date date NOT NULL,
    kind text NOT NULL,
    source_id text NOT NULL,
    reverses_entry_id bigint
);


--
-- Name: TABLE journal_entries; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.journal_entries IS '분개 전표';


--
-- Name: COLUMN journal_entries.entry_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_entries.entry_id IS '전표 번호';


--
-- Name: COLUMN journal_entries.event_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_entries.event_seq IS '원장 이벤트 순번';


--
-- Name: COLUMN journal_entries.date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_entries.date IS '회계일';


--
-- Name: COLUMN journal_entries.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_entries.kind IS '전표 유형';


--
-- Name: COLUMN journal_entries.source_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_entries.source_id IS '원천 참조 (체결번호 등)';


--
-- Name: COLUMN journal_entries.reverses_entry_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.journal_entries.reverses_entry_id IS '역분개 대상 전표 번호';


--
-- Name: journal_entries_entry_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.journal_entries_entry_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: journal_entries_entry_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.journal_entries_entry_id_seq OWNED BY public.journal_entries.entry_id;


--
-- Name: journal_lines_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.journal_lines_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: journal_lines_line_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.journal_lines_line_id_seq OWNED BY public.journal_lines.line_id;


--
-- Name: ledger_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ledger_events (
    seq bigint NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ledger_events_seq_check CHECK ((seq > 0))
);


--
-- Name: TABLE ledger_events; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.ledger_events IS '원장 이벤트 (모든 기록의 원천, 수정·삭제 불가)';


--
-- Name: COLUMN ledger_events.seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ledger_events.seq IS '이벤트 순번';


--
-- Name: COLUMN ledger_events.event_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ledger_events.event_type IS '이벤트 유형';


--
-- Name: COLUMN ledger_events.payload; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ledger_events.payload IS '이벤트 내용';


--
-- Name: COLUMN ledger_events.recorded_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ledger_events.recorded_at IS '기록 시각';


--
-- Name: oms_order_acks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oms_order_acks (
    client_id text NOT NULL,
    outcome text NOT NULL,
    order_no text,
    org_no text,
    detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT oms_order_acks_check CHECK (((outcome = 'ACCEPTED'::text) = (order_no IS NOT NULL))),
    CONSTRAINT oms_order_acks_outcome_check CHECK ((outcome = ANY (ARRAY['ACCEPTED'::text, 'REJECTED'::text])))
);


--
-- Name: TABLE oms_order_acks; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.oms_order_acks IS '주문 접수 결과 (증권사 응답)';


--
-- Name: COLUMN oms_order_acks.client_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_order_acks.client_id IS '내부 주문번호';


--
-- Name: COLUMN oms_order_acks.outcome; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_order_acks.outcome IS '결과 (ACCEPTED 접수 / REJECTED 거부)';


--
-- Name: COLUMN oms_order_acks.order_no; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_order_acks.order_no IS '증권사 주문번호 (영업일마다 다시 시작될 수 있음)';


--
-- Name: COLUMN oms_order_acks.org_no; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_order_acks.org_no IS '주문조직번호 (취소 시 필요)';


--
-- Name: COLUMN oms_order_acks.detail; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_order_acks.detail IS '거부 사유';


--
-- Name: COLUMN oms_order_acks.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_order_acks.created_at IS '기록 시각';


--
-- Name: oms_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oms_orders (
    client_id text NOT NULL,
    trade_date date NOT NULL,
    symbol text NOT NULL,
    side text NOT NULL,
    qty bigint NOT NULL,
    limit_price bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT oms_orders_qty_check CHECK ((qty > 0)),
    CONSTRAINT oms_orders_side_check CHECK ((side = ANY (ARRAY['BUY'::text, 'SELL'::text])))
);


--
-- Name: TABLE oms_orders; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.oms_orders IS '주문 의도 (증권사 전송 전 기록)';


--
-- Name: COLUMN oms_orders.client_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_orders.client_id IS '내부 주문번호';


--
-- Name: COLUMN oms_orders.trade_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_orders.trade_date IS '주문일';


--
-- Name: COLUMN oms_orders.symbol; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_orders.symbol IS '종목코드';


--
-- Name: COLUMN oms_orders.side; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_orders.side IS '매매구분 (BUY / SELL)';


--
-- Name: COLUMN oms_orders.qty; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_orders.qty IS '주문수량';


--
-- Name: COLUMN oms_orders.limit_price; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_orders.limit_price IS '지정가 (없으면 시장가)';


--
-- Name: COLUMN oms_orders.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.oms_orders.created_at IS '생성 시각';


--
-- Name: position_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.position_ledger (
    id bigint NOT NULL,
    event_seq bigint NOT NULL,
    trade_date date NOT NULL,
    symbol text NOT NULL,
    exec_id text NOT NULL,
    qty_delta bigint NOT NULL,
    cost_delta bigint NOT NULL,
    realized_pnl bigint
);


--
-- Name: TABLE position_ledger; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.position_ledger IS '보유수량 원장 (매매일 기준)';


--
-- Name: COLUMN position_ledger.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.position_ledger.id IS '행 번호';


--
-- Name: COLUMN position_ledger.event_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.position_ledger.event_seq IS '원장 이벤트 순번';


--
-- Name: COLUMN position_ledger.trade_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.position_ledger.trade_date IS '매매일';


--
-- Name: COLUMN position_ledger.symbol; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.position_ledger.symbol IS '종목코드';


--
-- Name: COLUMN position_ledger.exec_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.position_ledger.exec_id IS '체결 ID (기초 보유는 OPEN-종목코드)';


--
-- Name: COLUMN position_ledger.qty_delta; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.position_ledger.qty_delta IS '수량 증감';


--
-- Name: COLUMN position_ledger.cost_delta; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.position_ledger.cost_delta IS '장부금액 증감';


--
-- Name: COLUMN position_ledger.realized_pnl; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.position_ledger.realized_pnl IS '실현손익';


--
-- Name: position_ledger_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.position_ledger_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: position_ledger_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.position_ledger_id_seq OWNED BY public.position_ledger.id;


--
-- Name: positions_v; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.positions_v AS
 SELECT symbol,
    sum(qty_delta) AS qty,
    sum(cost_delta) AS book_cost
   FROM public.position_ledger
  GROUP BY symbol
 HAVING (sum(qty_delta) <> (0)::numeric);


--
-- Name: VIEW positions_v; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.positions_v IS '종목별 보유 현황';


--
-- Name: COLUMN positions_v.symbol; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.positions_v.symbol IS '종목코드';


--
-- Name: COLUMN positions_v.qty; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.positions_v.qty IS '보유수량';


--
-- Name: COLUMN positions_v.book_cost; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.positions_v.book_cost IS '장부금액 (매입금액)';


--
-- Name: recon_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recon_results (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    date date NOT NULL,
    item text NOT NULL,
    key text NOT NULL,
    internal_value bigint NOT NULL,
    broker_value bigint NOT NULL,
    diff bigint NOT NULL,
    status text NOT NULL,
    cause_code text,
    note text,
    supersedes_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT recon_results_status_check CHECK ((status = ANY (ARRAY['MATCH'::text, 'BREAK'::text, 'RESOLVED'::text])))
);


--
-- Name: TABLE recon_results; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.recon_results IS '대사 결과 (판단은 새 행으로 추가)';


--
-- Name: COLUMN recon_results.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.id IS '행 번호';


--
-- Name: COLUMN recon_results.run_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.run_id IS '대사 실행 스냅샷 번호';


--
-- Name: COLUMN recon_results.date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.date IS '기준일';


--
-- Name: COLUMN recon_results.item; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.item IS '대사 항목';


--
-- Name: COLUMN recon_results.key; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.key IS '항목 키 (종목코드 등)';


--
-- Name: COLUMN recon_results.internal_value; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.internal_value IS '내부(원장) 값';


--
-- Name: COLUMN recon_results.broker_value; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.broker_value IS '증권사 값';


--
-- Name: COLUMN recon_results.diff; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.diff IS '차이 (절대값)';


--
-- Name: COLUMN recon_results.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.status IS '상태 (MATCH 일치 / BREAK 불일치 / RESOLVED 해결)';


--
-- Name: COLUMN recon_results.cause_code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.cause_code IS '원인 코드';


--
-- Name: COLUMN recon_results.note; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.note IS '판단 메모';


--
-- Name: COLUMN recon_results.supersedes_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.supersedes_id IS '해결 대상 행 번호';


--
-- Name: COLUMN recon_results.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.recon_results.created_at IS '기록 시각';


--
-- Name: recon_results_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.recon_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: recon_results_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.recon_results_id_seq OWNED BY public.recon_results.id;


--
-- Name: broker_snapshots snapshot_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broker_snapshots ALTER COLUMN snapshot_id SET DEFAULT nextval('public.broker_snapshots_snapshot_id_seq'::regclass);


--
-- Name: cash_ledger id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cash_ledger ALTER COLUMN id SET DEFAULT nextval('public.cash_ledger_id_seq'::regclass);


--
-- Name: journal_entries entry_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entries ALTER COLUMN entry_id SET DEFAULT nextval('public.journal_entries_entry_id_seq'::regclass);


--
-- Name: journal_lines line_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_lines ALTER COLUMN line_id SET DEFAULT nextval('public.journal_lines_line_id_seq'::regclass);


--
-- Name: position_ledger id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_ledger ALTER COLUMN id SET DEFAULT nextval('public.position_ledger_id_seq'::regclass);


--
-- Name: recon_results id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recon_results ALTER COLUMN id SET DEFAULT nextval('public.recon_results_id_seq'::regclass);


--
-- Data for Name: accounts_chart; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.accounts_chart VALUES ('1010', '예수금', 'D');
INSERT INTO public.accounts_chart VALUES ('1020', '매도미수금', 'D');
INSERT INTO public.accounts_chart VALUES ('1030', '미수배당금', 'D');
INSERT INTO public.accounts_chart VALUES ('1100', 'FVPL 주식', 'D');
INSERT INTO public.accounts_chart VALUES ('1110', 'FVPL 주식 평가조정', 'D');
INSERT INTO public.accounts_chart VALUES ('2010', '매수미지급금', 'C');
INSERT INTO public.accounts_chart VALUES ('3010', '투자원금', 'C');
INSERT INTO public.accounts_chart VALUES ('4010', '매매이익', 'C');
INSERT INTO public.accounts_chart VALUES ('4020', '평가이익', 'C');
INSERT INTO public.accounts_chart VALUES ('4030', '배당수익', 'C');
INSERT INTO public.accounts_chart VALUES ('5010', '매매손실', 'D');
INSERT INTO public.accounts_chart VALUES ('5020', '평가손실', 'D');
INSERT INTO public.accounts_chart VALUES ('5030', '지급수수료', 'D');
INSERT INTO public.accounts_chart VALUES ('5040', '세금과공과', 'D');
INSERT INTO public.accounts_chart VALUES ('5050', '법인세등(원천징수)', 'D');


--
-- Data for Name: broker_snapshots; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.broker_snapshots VALUES (1, '2026-09-01', 'OPENING_BALANCE', '[{"cash": 100000000, "simulated": true}]', '2026-11-22 10:19:26.606141+00');
INSERT INTO public.broker_snapshots VALUES (2, '2026-09-01', 'DAILY_RECON', '{"orders": [{"qty": 125, "side": "BUY", "symbol": "360750", "order_no": "0000000001", "filled_qty": 125, "canceled_qty": 0, "rejected_qty": 0, "filled_amount": 2625000}], "balance": [{"cash": 100000000, "simulated": true}], "anomalies": []}', '2026-11-22 10:19:26.621938+00');
INSERT INTO public.broker_snapshots VALUES (3, '2026-09-02', 'DAILY_RECON', '{"orders": [{"qty": 62, "side": "SELL", "symbol": "360750", "order_no": "0000000001", "filled_qty": 62, "canceled_qty": 0, "rejected_qty": 0, "filled_amount": 1288670}, {"qty": 116, "side": "BUY", "symbol": "360750", "order_no": "0000000002", "filled_qty": 116, "canceled_qty": 0, "rejected_qty": 0, "filled_amount": 2411060}], "balance": [{"cash": 100000000, "simulated": true}], "anomalies": []}', '2026-11-22 10:19:26.637434+00');
INSERT INTO public.broker_snapshots VALUES (4, '2026-09-03', 'DAILY_RECON', '{"orders": [{"qty": 158, "side": "BUY", "symbol": "360750", "order_no": "0000000001", "filled_qty": 158, "canceled_qty": 0, "rejected_qty": 0, "filled_amount": 3316420}, {"qty": 31, "side": "BUY", "symbol": "005930", "order_no": "0000000002", "filled_qty": 31, "canceled_qty": 0, "rejected_qty": 0, "filled_amount": 1887900}], "balance": [{"cash": 97374610, "simulated": true}], "anomalies": []}', '2026-11-22 10:19:26.65545+00');
INSERT INTO public.broker_snapshots VALUES (5, '2026-09-04', 'DAILY_RECON', '{"orders": [{"qty": 112, "side": "BUY", "symbol": "360750", "order_no": "0000000001", "filled_qty": 112, "canceled_qty": 0, "rejected_qty": 0, "filled_amount": 2326800}, {"qty": 18, "side": "BUY", "symbol": "005930", "order_no": "0000000002", "filled_qty": 18, "canceled_qty": 0, "rejected_qty": 0, "filled_amount": 1090800}, {"qty": 10, "side": "BUY", "symbol": "069500", "order_no": "0000000003", "filled_qty": 0, "canceled_qty": 10, "rejected_qty": 0, "filled_amount": 0}], "balance": [{"cash": 96251670, "simulated": true}], "anomalies": []}', '2026-11-22 10:19:26.680875+00');
INSERT INTO public.broker_snapshots VALUES (6, '2026-09-07', 'DAILY_RECON', '{"orders": [{"qty": 35, "side": "BUY", "symbol": "069500", "order_no": "0000000001", "filled_qty": 35, "canceled_qty": 0, "rejected_qty": 0, "filled_amount": 2721950}], "balance": [{"cash": 91046580, "simulated": true}], "anomalies": []}', '2026-11-22 10:19:26.697685+00');
INSERT INTO public.broker_snapshots VALUES (7, '2026-09-08', 'DAILY_RECON', '{"orders": [], "balance": [{"cash": 87628480, "simulated": true}], "anomalies": []}', '2026-11-22 10:19:26.710376+00');


--
-- Data for Name: cash_ledger; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.cash_ledger VALUES (1, 1, '2026-09-01', 'DEPOSIT', 100000000, '');
INSERT INTO public.cash_ledger VALUES (2, 11, '2026-09-03', 'SETTLEMENT', -2625390, '20260901-0000000001-1');
INSERT INTO public.cash_ledger VALUES (3, 16, '2026-09-04', 'SETTLEMENT', 1288480, '20260902-0000000001-1');
INSERT INTO public.cash_ledger VALUES (4, 17, '2026-09-04', 'SETTLEMENT', -2411420, '20260902-0000000002-1');
INSERT INTO public.cash_ledger VALUES (5, 21, '2026-09-07', 'SETTLEMENT', -3316910, '20260903-0000000001-1');
INSERT INTO public.cash_ledger VALUES (6, 22, '2026-09-07', 'SETTLEMENT', -1888180, '20260903-0000000002-1');
INSERT INTO public.cash_ledger VALUES (7, 26, '2026-09-08', 'SETTLEMENT', -2327140, '20260904-0000000001-1');
INSERT INTO public.cash_ledger VALUES (8, 27, '2026-09-08', 'SETTLEMENT', -1090960, '20260904-0000000002-1');


--
-- Data for Name: instruments; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.instruments VALUES ('069500', 'KODEX 200', 'KOSPI', 'ETF');
INSERT INTO public.instruments VALUES ('360750', 'TIGER 미국S&P500', 'KOSPI', 'ETF');
INSERT INTO public.instruments VALUES ('005930', '삼성전자', 'KOSPI', 'STOCK');


--
-- Data for Name: journal_entries; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.journal_entries VALUES (1, 1, '2026-09-01', 'DEPOSIT', '', NULL);
INSERT INTO public.journal_entries VALUES (2, 2, '2026-09-01', 'EXECUTION', '20260901-0000000001-1', NULL);
INSERT INTO public.journal_entries VALUES (3, 5, '2026-09-02', 'EXECUTION', '20260902-0000000001-1', NULL);
INSERT INTO public.journal_entries VALUES (4, 6, '2026-09-02', 'EXECUTION', '20260902-0000000002-1', NULL);
INSERT INTO public.journal_entries VALUES (5, 7, '2026-09-02', 'VALUATION', '2026-09-02', NULL);
INSERT INTO public.journal_entries VALUES (6, 9, '2026-09-03', 'EXECUTION', '20260903-0000000001-1', NULL);
INSERT INTO public.journal_entries VALUES (7, 10, '2026-09-03', 'EXECUTION', '20260903-0000000002-1', NULL);
INSERT INTO public.journal_entries VALUES (8, 11, '2026-09-03', 'SETTLEMENT', '20260901-0000000001-1', NULL);
INSERT INTO public.journal_entries VALUES (9, 12, '2026-09-03', 'VALUATION_REVERSAL', '2026-09-02', 5);
INSERT INTO public.journal_entries VALUES (10, 12, '2026-09-03', 'VALUATION', '2026-09-03', NULL);
INSERT INTO public.journal_entries VALUES (11, 14, '2026-09-04', 'EXECUTION', '20260904-0000000001-1', NULL);
INSERT INTO public.journal_entries VALUES (12, 15, '2026-09-04', 'EXECUTION', '20260904-0000000002-1', NULL);
INSERT INTO public.journal_entries VALUES (13, 16, '2026-09-04', 'SETTLEMENT', '20260902-0000000001-1', NULL);
INSERT INTO public.journal_entries VALUES (14, 17, '2026-09-04', 'SETTLEMENT', '20260902-0000000002-1', NULL);
INSERT INTO public.journal_entries VALUES (15, 18, '2026-09-04', 'VALUATION_REVERSAL', '2026-09-03', 10);
INSERT INTO public.journal_entries VALUES (16, 18, '2026-09-04', 'VALUATION', '2026-09-04', NULL);
INSERT INTO public.journal_entries VALUES (17, 20, '2026-09-07', 'EXECUTION', '20260907-0000000001-1', NULL);
INSERT INTO public.journal_entries VALUES (18, 21, '2026-09-07', 'SETTLEMENT', '20260903-0000000001-1', NULL);
INSERT INTO public.journal_entries VALUES (19, 22, '2026-09-07', 'SETTLEMENT', '20260903-0000000002-1', NULL);
INSERT INTO public.journal_entries VALUES (20, 23, '2026-09-07', 'EXECUTION', '20260907-0000000001-2', NULL);
INSERT INTO public.journal_entries VALUES (21, 24, '2026-09-07', 'VALUATION_REVERSAL', '2026-09-04', 16);
INSERT INTO public.journal_entries VALUES (22, 24, '2026-09-07', 'VALUATION', '2026-09-07', NULL);
INSERT INTO public.journal_entries VALUES (23, 26, '2026-09-08', 'SETTLEMENT', '20260904-0000000001-1', NULL);
INSERT INTO public.journal_entries VALUES (24, 27, '2026-09-08', 'SETTLEMENT', '20260904-0000000002-1', NULL);
INSERT INTO public.journal_entries VALUES (25, 28, '2026-09-08', 'VALUATION_REVERSAL', '2026-09-07', 22);
INSERT INTO public.journal_entries VALUES (26, 28, '2026-09-08', 'VALUATION', '2026-09-08', NULL);


--
-- Data for Name: journal_lines; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.journal_lines VALUES (1, 1, '1010', 100000000, 0);
INSERT INTO public.journal_lines VALUES (2, 1, '3010', 0, 100000000);
INSERT INTO public.journal_lines VALUES (3, 2, '1100', 2625000, 0);
INSERT INTO public.journal_lines VALUES (4, 2, '5030', 390, 0);
INSERT INTO public.journal_lines VALUES (5, 2, '2010', 0, 2625390);
INSERT INTO public.journal_lines VALUES (6, 3, '1020', 1288480, 0);
INSERT INTO public.journal_lines VALUES (7, 3, '5030', 190, 0);
INSERT INTO public.journal_lines VALUES (8, 3, '1100', 0, 1302000);
INSERT INTO public.journal_lines VALUES (9, 3, '5010', 13330, 0);
INSERT INTO public.journal_lines VALUES (10, 4, '1100', 2411060, 0);
INSERT INTO public.journal_lines VALUES (11, 4, '5030', 360, 0);
INSERT INTO public.journal_lines VALUES (12, 4, '2010', 0, 2411420);
INSERT INTO public.journal_lines VALUES (13, 5, '5020', 13545, 0);
INSERT INTO public.journal_lines VALUES (14, 5, '1110', 0, 13545);
INSERT INTO public.journal_lines VALUES (15, 6, '1100', 3316420, 0);
INSERT INTO public.journal_lines VALUES (16, 6, '5030', 490, 0);
INSERT INTO public.journal_lines VALUES (17, 6, '2010', 0, 3316910);
INSERT INTO public.journal_lines VALUES (18, 7, '1100', 1887900, 0);
INSERT INTO public.journal_lines VALUES (19, 7, '5030', 280, 0);
INSERT INTO public.journal_lines VALUES (20, 7, '2010', 0, 1888180);
INSERT INTO public.journal_lines VALUES (21, 8, '2010', 2625390, 0);
INSERT INTO public.journal_lines VALUES (22, 8, '1010', 0, 2625390);
INSERT INTO public.journal_lines VALUES (23, 9, '5020', 0, 13545);
INSERT INTO public.journal_lines VALUES (24, 9, '1110', 13545, 0);
INSERT INTO public.journal_lines VALUES (25, 10, '1110', 23150, 0);
INSERT INTO public.journal_lines VALUES (26, 10, '4020', 0, 23150);
INSERT INTO public.journal_lines VALUES (27, 11, '1100', 2326800, 0);
INSERT INTO public.journal_lines VALUES (28, 11, '5030', 340, 0);
INSERT INTO public.journal_lines VALUES (29, 11, '2010', 0, 2327140);
INSERT INTO public.journal_lines VALUES (30, 12, '1100', 1090800, 0);
INSERT INTO public.journal_lines VALUES (31, 12, '5030', 160, 0);
INSERT INTO public.journal_lines VALUES (32, 12, '2010', 0, 1090960);
INSERT INTO public.journal_lines VALUES (33, 13, '1010', 1288480, 0);
INSERT INTO public.journal_lines VALUES (34, 13, '1020', 0, 1288480);
INSERT INTO public.journal_lines VALUES (35, 14, '2010', 2411420, 0);
INSERT INTO public.journal_lines VALUES (36, 14, '1010', 0, 2411420);
INSERT INTO public.journal_lines VALUES (37, 15, '1110', 0, 23150);
INSERT INTO public.journal_lines VALUES (38, 15, '4020', 23150, 0);
INSERT INTO public.journal_lines VALUES (39, 16, '5020', 58605, 0);
INSERT INTO public.journal_lines VALUES (40, 16, '1110', 0, 58605);
INSERT INTO public.journal_lines VALUES (41, 17, '1100', 1322090, 0);
INSERT INTO public.journal_lines VALUES (42, 17, '5030', 190, 0);
INSERT INTO public.journal_lines VALUES (43, 17, '2010', 0, 1322280);
INSERT INTO public.journal_lines VALUES (44, 18, '2010', 3316910, 0);
INSERT INTO public.journal_lines VALUES (45, 18, '1010', 0, 3316910);
INSERT INTO public.journal_lines VALUES (46, 19, '2010', 1888180, 0);
INSERT INTO public.journal_lines VALUES (47, 19, '1010', 0, 1888180);
INSERT INTO public.journal_lines VALUES (48, 20, '1100', 1399860, 0);
INSERT INTO public.journal_lines VALUES (49, 20, '5030', 200, 0);
INSERT INTO public.journal_lines VALUES (50, 20, '2010', 0, 1400060);
INSERT INTO public.journal_lines VALUES (51, 21, '5020', 0, 58605);
INSERT INTO public.journal_lines VALUES (52, 21, '1110', 58605, 0);
INSERT INTO public.journal_lines VALUES (53, 22, '5020', 18585, 0);
INSERT INTO public.journal_lines VALUES (54, 22, '1110', 0, 18585);
INSERT INTO public.journal_lines VALUES (55, 23, '2010', 2327140, 0);
INSERT INTO public.journal_lines VALUES (56, 23, '1010', 0, 2327140);
INSERT INTO public.journal_lines VALUES (57, 24, '2010', 1090960, 0);
INSERT INTO public.journal_lines VALUES (58, 24, '1010', 0, 1090960);
INSERT INTO public.journal_lines VALUES (59, 25, '5020', 0, 18585);
INSERT INTO public.journal_lines VALUES (60, 25, '1110', 18585, 0);
INSERT INTO public.journal_lines VALUES (61, 26, '5020', 39980, 0);
INSERT INTO public.journal_lines VALUES (62, 26, '1110', 0, 39980);


--
-- Data for Name: ledger_events; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.ledger_events VALUES (1, 'DepositEvent', '{"date": "2026-09-01", "amount": 100000000}', '2026-11-22 10:19:26.607143+00');
INSERT INTO public.ledger_events VALUES (2, 'ExecutionEvent', '{"qty": 125, "side": "BUY", "price": 21000, "symbol": "360750", "exec_id": "20260901-0000000001-1", "trade_date": "2026-09-01"}', '2026-11-22 10:19:26.616198+00');
INSERT INTO public.ledger_events VALUES (3, 'RevaluationEvent', '{"date": "2026-09-01", "closes": [["360750", 21000]]}', '2026-11-22 10:19:26.620597+00');
INSERT INTO public.ledger_events VALUES (4, 'NavEvent', '{"date": "2026-09-01", "closes": [["360750", 21000]]}', '2026-11-22 10:19:26.621318+00');
INSERT INTO public.ledger_events VALUES (5, 'ExecutionEvent', '{"qty": 62, "side": "SELL", "price": 20785, "symbol": "360750", "exec_id": "20260902-0000000001-1", "trade_date": "2026-09-02"}', '2026-11-22 10:19:26.628256+00');
INSERT INTO public.ledger_events VALUES (6, 'ExecutionEvent', '{"qty": 116, "side": "BUY", "price": 20785, "symbol": "360750", "exec_id": "20260902-0000000002-1", "trade_date": "2026-09-02"}', '2026-11-22 10:19:26.631306+00');
INSERT INTO public.ledger_events VALUES (7, 'RevaluationEvent', '{"date": "2026-09-02", "closes": [["360750", 20785]]}', '2026-11-22 10:19:26.63558+00');
INSERT INTO public.ledger_events VALUES (8, 'NavEvent', '{"date": "2026-09-02", "closes": [["360750", 20785]]}', '2026-11-22 10:19:26.636923+00');
INSERT INTO public.ledger_events VALUES (9, 'ExecutionEvent', '{"qty": 158, "side": "BUY", "price": 20990, "symbol": "360750", "exec_id": "20260903-0000000001-1", "trade_date": "2026-09-03"}', '2026-11-22 10:19:26.642372+00');
INSERT INTO public.ledger_events VALUES (10, 'ExecutionEvent', '{"qty": 31, "side": "BUY", "price": 60900, "symbol": "005930", "exec_id": "20260903-0000000002-1", "trade_date": "2026-09-03"}', '2026-11-22 10:19:26.646862+00');
INSERT INTO public.ledger_events VALUES (11, 'SettlementEvent', '{"date": "2026-09-03", "exec_id": "20260901-0000000001-1"}', '2026-11-22 10:19:26.650402+00');
INSERT INTO public.ledger_events VALUES (12, 'RevaluationEvent', '{"date": "2026-09-03", "closes": [["005930", 60900], ["360750", 20990]]}', '2026-11-22 10:19:26.652617+00');
INSERT INTO public.ledger_events VALUES (13, 'NavEvent', '{"date": "2026-09-03", "closes": [["005930", 60900], ["360750", 20990]]}', '2026-11-22 10:19:26.654526+00');
INSERT INTO public.ledger_events VALUES (14, 'ExecutionEvent', '{"qty": 112, "side": "BUY", "price": 20775, "symbol": "360750", "exec_id": "20260904-0000000001-1", "trade_date": "2026-09-04"}', '2026-11-22 10:19:26.665001+00');
INSERT INTO public.ledger_events VALUES (15, 'ExecutionEvent', '{"qty": 18, "side": "BUY", "price": 60600, "symbol": "005930", "exec_id": "20260904-0000000002-1", "trade_date": "2026-09-04"}', '2026-11-22 10:19:26.668619+00');
INSERT INTO public.ledger_events VALUES (16, 'SettlementEvent', '{"date": "2026-09-04", "exec_id": "20260902-0000000001-1"}', '2026-11-22 10:19:26.674677+00');
INSERT INTO public.ledger_events VALUES (17, 'SettlementEvent', '{"date": "2026-09-04", "exec_id": "20260902-0000000002-1"}', '2026-11-22 10:19:26.676414+00');
INSERT INTO public.ledger_events VALUES (18, 'RevaluationEvent', '{"date": "2026-09-04", "closes": [["005930", 60600], ["360750", 20775]]}', '2026-11-22 10:19:26.678205+00');
INSERT INTO public.ledger_events VALUES (19, 'NavEvent', '{"date": "2026-09-04", "closes": [["005930", 60600], ["360750", 20775]]}', '2026-11-22 10:19:26.680108+00');
INSERT INTO public.ledger_events VALUES (20, 'ExecutionEvent', '{"qty": 17, "side": "BUY", "price": 77770, "symbol": "069500", "exec_id": "20260907-0000000001-1", "trade_date": "2026-09-07"}', '2026-11-22 10:19:26.687014+00');
INSERT INTO public.ledger_events VALUES (21, 'SettlementEvent', '{"date": "2026-09-07", "exec_id": "20260903-0000000001-1"}', '2026-11-22 10:19:26.69099+00');
INSERT INTO public.ledger_events VALUES (22, 'SettlementEvent', '{"date": "2026-09-07", "exec_id": "20260903-0000000002-1"}', '2026-11-22 10:19:26.692247+00');
INSERT INTO public.ledger_events VALUES (23, 'ExecutionEvent', '{"qty": 18, "side": "BUY", "price": 77770, "symbol": "069500", "exec_id": "20260907-0000000001-2", "trade_date": "2026-09-07"}', '2026-11-22 10:19:26.693801+00');
INSERT INTO public.ledger_events VALUES (24, 'RevaluationEvent', '{"date": "2026-09-07", "closes": [["005930", 61600], ["069500", 77770], ["360750", 20755]]}', '2026-11-22 10:19:26.695105+00');
INSERT INTO public.ledger_events VALUES (25, 'NavEvent', '{"date": "2026-09-07", "closes": [["005930", 61600], ["069500", 77770], ["360750", 20755]]}', '2026-11-22 10:19:26.696926+00');
INSERT INTO public.ledger_events VALUES (26, 'SettlementEvent', '{"date": "2026-09-08", "exec_id": "20260904-0000000001-1"}', '2026-11-22 10:19:26.70508+00');
INSERT INTO public.ledger_events VALUES (27, 'SettlementEvent', '{"date": "2026-09-08", "exec_id": "20260904-0000000002-1"}', '2026-11-22 10:19:26.706472+00');
INSERT INTO public.ledger_events VALUES (28, 'RevaluationEvent', '{"date": "2026-09-08", "closes": [["005930", 60700], ["069500", 77200], ["360750", 20850]]}', '2026-11-22 10:19:26.707863+00');
INSERT INTO public.ledger_events VALUES (29, 'NavEvent', '{"date": "2026-09-08", "closes": [["005930", 60700], ["069500", 77200], ["360750", 20850]]}', '2026-11-22 10:19:26.709734+00');


--
-- Data for Name: oms_order_acks; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.oms_order_acks VALUES ('S20260901-0001', 'ACCEPTED', '0000000001', 'SIM', NULL, '2026-11-22 10:19:26.615371+00');
INSERT INTO public.oms_order_acks VALUES ('S20260902-0001', 'ACCEPTED', '0000000001', 'SIM', NULL, '2026-11-22 10:19:26.627611+00');
INSERT INTO public.oms_order_acks VALUES ('S20260902-0002', 'ACCEPTED', '0000000002', 'SIM', NULL, '2026-11-22 10:19:26.630841+00');
INSERT INTO public.oms_order_acks VALUES ('S20260903-0001', 'ACCEPTED', '0000000001', 'SIM', NULL, '2026-11-22 10:19:26.641921+00');
INSERT INTO public.oms_order_acks VALUES ('S20260903-0002', 'ACCEPTED', '0000000002', 'SIM', NULL, '2026-11-22 10:19:26.646162+00');
INSERT INTO public.oms_order_acks VALUES ('S20260904-0001', 'ACCEPTED', '0000000001', 'SIM', NULL, '2026-11-22 10:19:26.664357+00');
INSERT INTO public.oms_order_acks VALUES ('S20260904-0002', 'ACCEPTED', '0000000002', 'SIM', NULL, '2026-11-22 10:19:26.667721+00');
INSERT INTO public.oms_order_acks VALUES ('S20260904-0003', 'ACCEPTED', '0000000003', 'SIM', NULL, '2026-11-22 10:19:26.671644+00');
INSERT INTO public.oms_order_acks VALUES ('S20260907-0001', 'ACCEPTED', '0000000001', 'SIM', NULL, '2026-11-22 10:19:26.686532+00');


--
-- Data for Name: oms_orders; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.oms_orders VALUES ('S20260901-0001', '2026-09-01', '360750', 'BUY', 125, NULL, '2026-11-22 10:19:26.614586+00');
INSERT INTO public.oms_orders VALUES ('S20260902-0001', '2026-09-02', '360750', 'SELL', 62, NULL, '2026-11-22 10:19:26.626986+00');
INSERT INTO public.oms_orders VALUES ('S20260902-0002', '2026-09-02', '360750', 'BUY', 116, NULL, '2026-11-22 10:19:26.630354+00');
INSERT INTO public.oms_orders VALUES ('S20260903-0001', '2026-09-03', '360750', 'BUY', 158, 20990, '2026-11-22 10:19:26.641299+00');
INSERT INTO public.oms_orders VALUES ('S20260903-0002', '2026-09-03', '005930', 'BUY', 31, 60900, '2026-11-22 10:19:26.645387+00');
INSERT INTO public.oms_orders VALUES ('S20260904-0001', '2026-09-04', '360750', 'BUY', 112, NULL, '2026-11-22 10:19:26.663521+00');
INSERT INTO public.oms_orders VALUES ('S20260904-0002', '2026-09-04', '005930', 'BUY', 18, NULL, '2026-11-22 10:19:26.667073+00');
INSERT INTO public.oms_orders VALUES ('S20260904-0003', '2026-09-04', '069500', 'BUY', 10, 77305, '2026-11-22 10:19:26.67104+00');
INSERT INTO public.oms_orders VALUES ('S20260907-0001', '2026-09-07', '069500', 'BUY', 35, NULL, '2026-11-22 10:19:26.685953+00');


--
-- Data for Name: position_ledger; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.position_ledger VALUES (1, 2, '2026-09-01', '360750', '20260901-0000000001-1', 125, 2625000, NULL);
INSERT INTO public.position_ledger VALUES (2, 5, '2026-09-02', '360750', '20260902-0000000001-1', -62, -1302000, -13330);
INSERT INTO public.position_ledger VALUES (3, 6, '2026-09-02', '360750', '20260902-0000000002-1', 116, 2411060, NULL);
INSERT INTO public.position_ledger VALUES (4, 9, '2026-09-03', '360750', '20260903-0000000001-1', 158, 3316420, NULL);
INSERT INTO public.position_ledger VALUES (5, 10, '2026-09-03', '005930', '20260903-0000000002-1', 31, 1887900, NULL);
INSERT INTO public.position_ledger VALUES (6, 14, '2026-09-04', '360750', '20260904-0000000001-1', 112, 2326800, NULL);
INSERT INTO public.position_ledger VALUES (7, 15, '2026-09-04', '005930', '20260904-0000000002-1', 18, 1090800, NULL);
INSERT INTO public.position_ledger VALUES (8, 20, '2026-09-07', '069500', '20260907-0000000001-1', 17, 1322090, NULL);
INSERT INTO public.position_ledger VALUES (9, 23, '2026-09-07', '069500', '20260907-0000000001-2', 18, 1399860, NULL);


--
-- Data for Name: recon_results; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.recon_results VALUES (1, 2, '2026-09-01', 'POSITION_QTY', '360750', 125, 125, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.622455+00');
INSERT INTO public.recon_results VALUES (2, 2, '2026-09-01', 'BOOK_COST', '360750', 2625000, 2625000, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.622455+00');
INSERT INTO public.recon_results VALUES (3, 2, '2026-09-01', 'CASH_D0', '', 100000000, 100000000, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.622455+00');
INSERT INTO public.recon_results VALUES (4, 2, '2026-09-01', 'CASH_PROJECTED', '', 97374610, 97374610, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.622455+00');
INSERT INTO public.recon_results VALUES (5, 2, '2026-09-01', 'EXEC_QTY', '2026-09-01', 125, 125, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.622455+00');
INSERT INTO public.recon_results VALUES (6, 3, '2026-09-02', 'POSITION_QTY', '360750', 179, 179, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.637858+00');
INSERT INTO public.recon_results VALUES (7, 3, '2026-09-02', 'BOOK_COST', '360750', 3734060, 3734060, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.637858+00');
INSERT INTO public.recon_results VALUES (8, 3, '2026-09-02', 'CASH_D0', '', 100000000, 100000000, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.637858+00');
INSERT INTO public.recon_results VALUES (9, 3, '2026-09-02', 'CASH_PROJECTED', '', 96251670, 96251670, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.637858+00');
INSERT INTO public.recon_results VALUES (10, 3, '2026-09-02', 'EXEC_QTY', '2026-09-02', 178, 178, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.637858+00');
INSERT INTO public.recon_results VALUES (11, 4, '2026-09-03', 'POSITION_QTY', '005930', 31, 31, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.656155+00');
INSERT INTO public.recon_results VALUES (12, 4, '2026-09-03', 'BOOK_COST', '005930', 1887900, 1887900, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.656155+00');
INSERT INTO public.recon_results VALUES (13, 4, '2026-09-03', 'POSITION_QTY', '360750', 337, 337, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.656155+00');
INSERT INTO public.recon_results VALUES (14, 4, '2026-09-03', 'BOOK_COST', '360750', 7050480, 7050480, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.656155+00');
INSERT INTO public.recon_results VALUES (15, 4, '2026-09-03', 'CASH_D0', '', 97374610, 97374605, 5, 'BREAK', 'FEE_DIFF', NULL, NULL, '2026-11-22 10:19:26.656155+00');
INSERT INTO public.recon_results VALUES (16, 4, '2026-09-03', 'CASH_PROJECTED', '', 91046580, 91046575, 5, 'BREAK', 'FEE_DIFF', NULL, NULL, '2026-11-22 10:19:26.656155+00');
INSERT INTO public.recon_results VALUES (17, 4, '2026-09-03', 'EXEC_QTY', '2026-09-03', 189, 189, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.656155+00');
INSERT INTO public.recon_results VALUES (18, 4, '2026-09-03', 'CASH_D0', '', 97374610, 97374605, 5, 'RESOLVED', 'FEE_DIFF', '샘플: 증권사 수수료 절사 차이로 판단', 15, '2026-11-22 10:19:26.659062+00');
INSERT INTO public.recon_results VALUES (19, 4, '2026-09-03', 'CASH_PROJECTED', '', 91046580, 91046575, 5, 'RESOLVED', 'FEE_DIFF', '샘플: 증권사 수수료 절사 차이로 판단', 16, '2026-11-22 10:19:26.660189+00');
INSERT INTO public.recon_results VALUES (20, 5, '2026-09-04', 'POSITION_QTY', '005930', 49, 49, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.681367+00');
INSERT INTO public.recon_results VALUES (21, 5, '2026-09-04', 'BOOK_COST', '005930', 2978700, 2978700, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.681367+00');
INSERT INTO public.recon_results VALUES (22, 5, '2026-09-04', 'POSITION_QTY', '360750', 449, 449, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.681367+00');
INSERT INTO public.recon_results VALUES (23, 5, '2026-09-04', 'BOOK_COST', '360750', 9377280, 9377280, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.681367+00');
INSERT INTO public.recon_results VALUES (24, 5, '2026-09-04', 'CASH_D0', '', 96251670, 96251670, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.681367+00');
INSERT INTO public.recon_results VALUES (25, 5, '2026-09-04', 'CASH_PROJECTED', '', 87628480, 87628480, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.681367+00');
INSERT INTO public.recon_results VALUES (26, 5, '2026-09-04', 'EXEC_QTY', '2026-09-04', 130, 130, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.681367+00');
INSERT INTO public.recon_results VALUES (27, 6, '2026-09-07', 'POSITION_QTY', '005930', 49, 49, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.698111+00');
INSERT INTO public.recon_results VALUES (28, 6, '2026-09-07', 'BOOK_COST', '005930', 2978700, 2978700, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.698111+00');
INSERT INTO public.recon_results VALUES (29, 6, '2026-09-07', 'POSITION_QTY', '069500', 35, 35, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.698111+00');
INSERT INTO public.recon_results VALUES (30, 6, '2026-09-07', 'BOOK_COST', '069500', 2721950, 2721950, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.698111+00');
INSERT INTO public.recon_results VALUES (31, 6, '2026-09-07', 'POSITION_QTY', '360750', 449, 449, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.698111+00');
INSERT INTO public.recon_results VALUES (32, 6, '2026-09-07', 'BOOK_COST', '360750', 9377280, 9377280, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.698111+00');
INSERT INTO public.recon_results VALUES (33, 6, '2026-09-07', 'CASH_D0', '', 91046580, 91046580, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.698111+00');
INSERT INTO public.recon_results VALUES (34, 6, '2026-09-07', 'CASH_PROJECTED', '', 84906140, 84906140, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.698111+00');
INSERT INTO public.recon_results VALUES (35, 6, '2026-09-07', 'EXEC_QTY', '2026-09-07', 35, 35, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.698111+00');
INSERT INTO public.recon_results VALUES (36, 7, '2026-09-08', 'POSITION_QTY', '005930', 49, 49, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.71076+00');
INSERT INTO public.recon_results VALUES (37, 7, '2026-09-08', 'BOOK_COST', '005930', 2978700, 2978700, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.71076+00');
INSERT INTO public.recon_results VALUES (38, 7, '2026-09-08', 'POSITION_QTY', '069500', 35, 35, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.71076+00');
INSERT INTO public.recon_results VALUES (39, 7, '2026-09-08', 'BOOK_COST', '069500', 2721950, 2721950, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.71076+00');
INSERT INTO public.recon_results VALUES (40, 7, '2026-09-08', 'POSITION_QTY', '360750', 449, 449, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.71076+00');
INSERT INTO public.recon_results VALUES (41, 7, '2026-09-08', 'BOOK_COST', '360750', 9377280, 9377280, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.71076+00');
INSERT INTO public.recon_results VALUES (42, 7, '2026-09-08', 'CASH_D0', '', 87628480, 87628480, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.71076+00');
INSERT INTO public.recon_results VALUES (43, 7, '2026-09-08', 'CASH_PROJECTED', '', 84906140, 84906140, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.71076+00');
INSERT INTO public.recon_results VALUES (44, 7, '2026-09-08', 'EXEC_QTY', '2026-09-08', 0, 0, 0, 'MATCH', NULL, NULL, NULL, '2026-11-22 10:19:26.71076+00');


--
-- Name: broker_snapshots_snapshot_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.broker_snapshots_snapshot_id_seq', 7, true);


--
-- Name: cash_ledger_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.cash_ledger_id_seq', 8, true);


--
-- Name: journal_entries_entry_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.journal_entries_entry_id_seq', 26, true);


--
-- Name: journal_lines_line_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.journal_lines_line_id_seq', 62, true);


--
-- Name: position_ledger_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.position_ledger_id_seq', 9, true);


--
-- Name: recon_results_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.recon_results_id_seq', 44, true);


--
-- Name: accounts_chart accounts_chart_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_chart
    ADD CONSTRAINT accounts_chart_pkey PRIMARY KEY (code);


--
-- Name: broker_snapshots broker_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broker_snapshots
    ADD CONSTRAINT broker_snapshots_pkey PRIMARY KEY (snapshot_id);


--
-- Name: cash_ledger cash_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cash_ledger
    ADD CONSTRAINT cash_ledger_pkey PRIMARY KEY (id);


--
-- Name: instruments instruments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruments
    ADD CONSTRAINT instruments_pkey PRIMARY KEY (symbol);


--
-- Name: journal_entries journal_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entries
    ADD CONSTRAINT journal_entries_pkey PRIMARY KEY (entry_id);


--
-- Name: journal_lines journal_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_lines
    ADD CONSTRAINT journal_lines_pkey PRIMARY KEY (line_id);


--
-- Name: ledger_events ledger_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ledger_events
    ADD CONSTRAINT ledger_events_pkey PRIMARY KEY (seq);


--
-- Name: oms_order_acks oms_order_acks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oms_order_acks
    ADD CONSTRAINT oms_order_acks_pkey PRIMARY KEY (client_id);


--
-- Name: oms_orders oms_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oms_orders
    ADD CONSTRAINT oms_orders_pkey PRIMARY KEY (client_id);


--
-- Name: position_ledger position_ledger_exec_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_ledger
    ADD CONSTRAINT position_ledger_exec_id_key UNIQUE (exec_id);


--
-- Name: position_ledger position_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_ledger
    ADD CONSTRAINT position_ledger_pkey PRIMARY KEY (id);


--
-- Name: recon_results recon_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recon_results
    ADD CONSTRAINT recon_results_pkey PRIMARY KEY (id);


--
-- Name: oms_order_acks_order_no_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX oms_order_acks_order_no_idx ON public.oms_order_acks USING btree (order_no);


--
-- Name: broker_snapshots broker_snapshots_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER broker_snapshots_append_only BEFORE DELETE OR UPDATE ON public.broker_snapshots FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: broker_snapshots broker_snapshots_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER broker_snapshots_no_truncate BEFORE TRUNCATE ON public.broker_snapshots FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: cash_ledger cash_ledger_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER cash_ledger_append_only BEFORE DELETE OR UPDATE ON public.cash_ledger FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: cash_ledger cash_ledger_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER cash_ledger_no_truncate BEFORE TRUNCATE ON public.cash_ledger FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: instruments instruments_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER instruments_append_only BEFORE DELETE OR UPDATE ON public.instruments FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: instruments instruments_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER instruments_no_truncate BEFORE TRUNCATE ON public.instruments FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: journal_entries journal_entries_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER journal_entries_append_only BEFORE DELETE OR UPDATE ON public.journal_entries FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: journal_entries journal_entries_balanced; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER journal_entries_balanced AFTER INSERT ON public.journal_entries DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.ministp_check_entry_balance();


--
-- Name: journal_entries journal_entries_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER journal_entries_no_truncate BEFORE TRUNCATE ON public.journal_entries FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: journal_lines journal_lines_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER journal_lines_append_only BEFORE DELETE OR UPDATE ON public.journal_lines FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: journal_lines journal_lines_balanced; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER journal_lines_balanced AFTER INSERT ON public.journal_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.ministp_check_entry_balance();


--
-- Name: journal_lines journal_lines_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER journal_lines_no_truncate BEFORE TRUNCATE ON public.journal_lines FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: ledger_events ledger_events_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER ledger_events_append_only BEFORE DELETE OR UPDATE ON public.ledger_events FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: ledger_events ledger_events_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER ledger_events_no_truncate BEFORE TRUNCATE ON public.ledger_events FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: oms_order_acks oms_order_acks_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER oms_order_acks_append_only BEFORE DELETE OR UPDATE ON public.oms_order_acks FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: oms_order_acks oms_order_acks_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER oms_order_acks_no_truncate BEFORE TRUNCATE ON public.oms_order_acks FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: oms_orders oms_orders_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER oms_orders_append_only BEFORE DELETE OR UPDATE ON public.oms_orders FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: oms_orders oms_orders_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER oms_orders_no_truncate BEFORE TRUNCATE ON public.oms_orders FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: position_ledger position_ledger_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER position_ledger_append_only BEFORE DELETE OR UPDATE ON public.position_ledger FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: position_ledger position_ledger_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER position_ledger_no_truncate BEFORE TRUNCATE ON public.position_ledger FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: recon_results recon_results_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER recon_results_append_only BEFORE DELETE OR UPDATE ON public.recon_results FOR EACH ROW EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: recon_results recon_results_no_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER recon_results_no_truncate BEFORE TRUNCATE ON public.recon_results FOR EACH STATEMENT EXECUTE FUNCTION public.ministp_forbid_change();


--
-- Name: cash_ledger cash_ledger_event_seq_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cash_ledger
    ADD CONSTRAINT cash_ledger_event_seq_fkey FOREIGN KEY (event_seq) REFERENCES public.ledger_events(seq);


--
-- Name: journal_entries journal_entries_event_seq_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entries
    ADD CONSTRAINT journal_entries_event_seq_fkey FOREIGN KEY (event_seq) REFERENCES public.ledger_events(seq);


--
-- Name: journal_entries journal_entries_reverses_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entries
    ADD CONSTRAINT journal_entries_reverses_entry_id_fkey FOREIGN KEY (reverses_entry_id) REFERENCES public.journal_entries(entry_id);


--
-- Name: journal_lines journal_lines_account_code_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_lines
    ADD CONSTRAINT journal_lines_account_code_fkey FOREIGN KEY (account_code) REFERENCES public.accounts_chart(code);


--
-- Name: journal_lines journal_lines_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_lines
    ADD CONSTRAINT journal_lines_entry_id_fkey FOREIGN KEY (entry_id) REFERENCES public.journal_entries(entry_id);


--
-- Name: oms_order_acks oms_order_acks_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oms_order_acks
    ADD CONSTRAINT oms_order_acks_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.oms_orders(client_id);


--
-- Name: oms_orders oms_orders_symbol_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oms_orders
    ADD CONSTRAINT oms_orders_symbol_fkey FOREIGN KEY (symbol) REFERENCES public.instruments(symbol);


--
-- Name: position_ledger position_ledger_event_seq_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_ledger
    ADD CONSTRAINT position_ledger_event_seq_fkey FOREIGN KEY (event_seq) REFERENCES public.ledger_events(seq);


--
-- Name: recon_results recon_results_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recon_results
    ADD CONSTRAINT recon_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.broker_snapshots(snapshot_id);


--
-- Name: recon_results recon_results_supersedes_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recon_results
    ADD CONSTRAINT recon_results_supersedes_id_fkey FOREIGN KEY (supersedes_id) REFERENCES public.recon_results(id);


--
-- PostgreSQL database dump complete
--


