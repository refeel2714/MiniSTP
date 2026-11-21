-- MiniSTP ledger schema (spec 03).
-- ERD version: 1.0  (bump on every schema change, then run scripts/gen_erd.py) Append-only tables are enforced in the DB,
-- not just in application code.

create table if not exists accounts_chart (
    code        text primary key,
    name        text not null,
    normal_side char(1) not null check (normal_side in ('D', 'C'))
);

create table if not exists ledger_events (
    seq         bigint primary key check (seq > 0),
    event_type  text not null,
    payload     jsonb not null,
    recorded_at timestamptz not null default now()
);

create table if not exists journal_entries (
    entry_id          bigserial primary key,
    event_seq         bigint not null references ledger_events (seq),
    date              date not null,
    kind              text not null,
    source_id         text not null,
    reverses_entry_id bigint references journal_entries (entry_id)
);

create table if not exists journal_lines (
    line_id      bigserial primary key,
    entry_id     bigint not null references journal_entries (entry_id),
    account_code text not null references accounts_chart (code),
    debit        bigint not null default 0 check (debit >= 0),
    credit       bigint not null default 0 check (credit >= 0),
    check ((debit = 0) <> (credit = 0))
);

create table if not exists position_ledger (
    id           bigserial primary key,
    event_seq    bigint not null references ledger_events (seq),
    trade_date   date not null,
    symbol       text not null,
    exec_id      text not null unique,
    qty_delta    bigint not null,
    cost_delta   bigint not null,
    realized_pnl bigint
);

create table if not exists cash_ledger (
    id        bigserial primary key,
    event_seq bigint not null references ledger_events (seq),
    date      date not null,
    reason    text not null,
    amount    bigint not null,
    ref_id    text not null
);

-- Operations (Phase 2): instruments, orders, broker snapshots, reconciliation.
create table if not exists instruments (
    symbol     text primary key,
    name       text not null,
    market     text not null check (market in ('KOSPI', 'KOSDAQ')),
    asset_type text not null check (asset_type in ('STOCK', 'ETF'))
);

-- R-40: the order intent is stored before it is sent, the broker's answer after.
create table if not exists oms_orders (
    client_id   text primary key,
    trade_date  date not null,
    symbol      text not null references instruments (symbol),
    side        text not null check (side in ('BUY', 'SELL')),
    qty         bigint not null check (qty > 0),
    limit_price bigint,
    created_at  timestamptz not null default now()
);

create table if not exists oms_order_acks (
    client_id  text primary key references oms_orders (client_id),
    outcome    text not null check (outcome in ('ACCEPTED', 'REJECTED')),
    order_no   text unique,
    org_no     text,
    detail     text,
    created_at timestamptz not null default now(),
    check ((outcome = 'ACCEPTED') = (order_no is not null))
);

-- R-70: broker answers are stored as received, before comparing.
create table if not exists broker_snapshots (
    snapshot_id bigserial primary key,
    date        date not null,
    kind        text not null,
    payload     jsonb not null,
    taken_at    timestamptz not null default now()
);

-- R-72~73: one row per item per run; a later judgement is a new row (supersedes_id).
create table if not exists recon_results (
    id             bigserial primary key,
    run_id         bigint not null references broker_snapshots (snapshot_id),
    date           date not null,
    item           text not null,
    key            text not null,
    internal_value bigint not null,
    broker_value   bigint not null,
    diff           bigint not null,
    status         text not null check (status in ('MATCH', 'BREAK', 'RESOLVED')),
    cause_code     text,
    note           text,
    supersedes_id  bigint references recon_results (id),
    created_at     timestamptz not null default now()
);

-- R-04: no UPDATE / DELETE / TRUNCATE on ledger tables.
create or replace function ministp_forbid_change() returns trigger language plpgsql as $$
begin
    raise exception 'append-only table %: % is not allowed', tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end $$;

-- R-50: every journal entry has lines and debits equal credits (checked at commit).
create or replace function ministp_check_entry_balance() returns trigger language plpgsql as $$
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

do $$
declare t text;
begin
    foreach t in array array['ledger_events', 'journal_entries', 'journal_lines',
                             'position_ledger', 'cash_ledger', 'instruments', 'oms_orders',
                             'oms_order_acks', 'broker_snapshots', 'recon_results'] loop
        if not exists (select 1 from pg_trigger where tgname = t || '_append_only') then
            execute format('create trigger %I before update or delete on %I
                            for each row execute function ministp_forbid_change()',
                           t || '_append_only', t);
            execute format('create trigger %I before truncate on %I
                            for each statement execute function ministp_forbid_change()',
                           t || '_no_truncate', t);
        end if;
    end loop;
    if not exists (select 1 from pg_trigger where tgname = 'journal_entries_balanced') then
        create constraint trigger journal_entries_balanced after insert on journal_entries
            deferrable initially deferred for each row execute function ministp_check_entry_balance();
        create constraint trigger journal_lines_balanced after insert on journal_lines
            deferrable initially deferred for each row execute function ministp_check_entry_balance();
    end if;
end $$;

create or replace view account_balances_v as
select a.code, a.name, a.normal_side,
       case when a.normal_side = 'D'
            then coalesce(sum(l.debit - l.credit), 0)
            else coalesce(sum(l.credit - l.debit), 0) end as balance
  from accounts_chart a
  left join journal_lines l on l.account_code = a.code
 group by a.code, a.name, a.normal_side;

create or replace view positions_v as
select symbol, sum(qty_delta) as qty, sum(cost_delta) as book_cost
  from position_ledger group by symbol having sum(qty_delta) <> 0;

create or replace view cash_balance_v as
select coalesce(sum(amount), 0) as balance from cash_ledger;

-- Korean names (한글 명칭). Shown in pgAdmin and in docs/erd. Every table, view and
-- column must have one; tests/db/test_erd.py fails otherwise.
comment on table accounts_chart is '계정과목';
comment on column accounts_chart.code is '계정코드';
comment on column accounts_chart.name is '계정명';
comment on column accounts_chart.normal_side is '정상잔액 방향 (D 차변 / C 대변)';

comment on table ledger_events is '원장 이벤트 (모든 기록의 원천, 수정·삭제 불가)';
comment on column ledger_events.seq is '이벤트 순번';
comment on column ledger_events.event_type is '이벤트 유형';
comment on column ledger_events.payload is '이벤트 내용';
comment on column ledger_events.recorded_at is '기록 시각';

comment on table journal_entries is '분개 전표';
comment on column journal_entries.entry_id is '전표 번호';
comment on column journal_entries.event_seq is '원장 이벤트 순번';
comment on column journal_entries.date is '회계일';
comment on column journal_entries.kind is '전표 유형';
comment on column journal_entries.source_id is '원천 참조 (체결번호 등)';
comment on column journal_entries.reverses_entry_id is '역분개 대상 전표 번호';

comment on table journal_lines is '분개 명세 (차변·대변 라인)';
comment on column journal_lines.line_id is '라인 번호';
comment on column journal_lines.entry_id is '전표 번호';
comment on column journal_lines.account_code is '계정코드';
comment on column journal_lines.debit is '차변 금액';
comment on column journal_lines.credit is '대변 금액';

comment on table position_ledger is '보유수량 원장 (매매일 기준)';
comment on column position_ledger.id is '행 번호';
comment on column position_ledger.event_seq is '원장 이벤트 순번';
comment on column position_ledger.trade_date is '매매일';
comment on column position_ledger.symbol is '종목코드';
comment on column position_ledger.exec_id is '체결 ID (기초 보유는 OPEN-종목코드)';
comment on column position_ledger.qty_delta is '수량 증감';
comment on column position_ledger.cost_delta is '장부금액 증감';
comment on column position_ledger.realized_pnl is '실현손익';

comment on table cash_ledger is '예수금 원장 (결제일 기준)';
comment on column cash_ledger.id is '행 번호';
comment on column cash_ledger.event_seq is '원장 이벤트 순번';
comment on column cash_ledger.date is '일자';
comment on column cash_ledger.reason is '사유 (입금, 결제 등)';
comment on column cash_ledger.amount is '금액 (+입금 / −출금)';
comment on column cash_ledger.ref_id is '참조 ID';

comment on table instruments is '종목 마스터';
comment on column instruments.symbol is '종목코드';
comment on column instruments.name is '종목명';
comment on column instruments.market is '시장 (KOSPI / KOSDAQ)';
comment on column instruments.asset_type is '자산 유형 (STOCK / ETF)';

comment on table oms_orders is '주문 의도 (증권사 전송 전 기록)';
comment on column oms_orders.client_id is '내부 주문번호';
comment on column oms_orders.trade_date is '주문일';
comment on column oms_orders.symbol is '종목코드';
comment on column oms_orders.side is '매매구분 (BUY / SELL)';
comment on column oms_orders.qty is '주문수량';
comment on column oms_orders.limit_price is '지정가 (없으면 시장가)';
comment on column oms_orders.created_at is '생성 시각';

comment on table oms_order_acks is '주문 접수 결과 (증권사 응답)';
comment on column oms_order_acks.client_id is '내부 주문번호';
comment on column oms_order_acks.outcome is '결과 (ACCEPTED 접수 / REJECTED 거부)';
comment on column oms_order_acks.order_no is '증권사 주문번호';
comment on column oms_order_acks.org_no is '주문조직번호 (취소 시 필요)';
comment on column oms_order_acks.detail is '거부 사유';
comment on column oms_order_acks.created_at is '기록 시각';

comment on table broker_snapshots is '증권사 응답 원본 (대사 실행, 체결통보, 원장 시작)';
comment on column broker_snapshots.snapshot_id is '스냅샷 번호';
comment on column broker_snapshots.date is '기준일';
comment on column broker_snapshots.kind is '유형 (OPENING_BALANCE / DAILY_RECON / INTRADAY_RECON / FILL_NOTICE)';
comment on column broker_snapshots.payload is '응답 원본';
comment on column broker_snapshots.taken_at is '수신 시각';

comment on table recon_results is '대사 결과 (판단은 새 행으로 추가)';
comment on column recon_results.id is '행 번호';
comment on column recon_results.run_id is '대사 실행 스냅샷 번호';
comment on column recon_results.date is '기준일';
comment on column recon_results.item is '대사 항목';
comment on column recon_results.key is '항목 키 (종목코드 등)';
comment on column recon_results.internal_value is '내부(원장) 값';
comment on column recon_results.broker_value is '증권사 값';
comment on column recon_results.diff is '차이 (절대값)';
comment on column recon_results.status is '상태 (MATCH 일치 / BREAK 불일치 / RESOLVED 해결)';
comment on column recon_results.cause_code is '원인 코드';
comment on column recon_results.note is '판단 메모';
comment on column recon_results.supersedes_id is '해결 대상 행 번호';
comment on column recon_results.created_at is '기록 시각';

comment on view account_balances_v is '계정별 잔액 (시산표)';
comment on column account_balances_v.code is '계정코드';
comment on column account_balances_v.name is '계정명';
comment on column account_balances_v.normal_side is '정상잔액 방향';
comment on column account_balances_v.balance is '잔액';

comment on view positions_v is '종목별 보유 현황';
comment on column positions_v.symbol is '종목코드';
comment on column positions_v.qty is '보유수량';
comment on column positions_v.book_cost is '장부금액 (매입금액)';

comment on view cash_balance_v is '예수금 잔액';
comment on column cash_balance_v.balance is '잔액';
