-- MiniSTP ledger schema (spec 03).
-- ERD version: 2.3  (bump on every schema change, then run scripts/gen_erd.py) Append-only tables are enforced in the DB,
-- not just in application code.

create table if not exists accounts_chart (
    code        text primary key,
    name        text not null,
    normal_side char(1) not null check (normal_side in ('D', 'C'))
);

-- v2.0 다계좌 (스펙 D7): 고객 -> 위탁계좌. 계좌번호와 인증키는 DB에 넣지 않는다.
create table if not exists customers (
    customer_id text primary key check (customer_id ~ '^C[0-9]{4}$'),
    alias       text not null,
    created_at  timestamptz not null default now()
);

create table if not exists trading_accounts (
    account_id     text primary key check (account_id ~ '^A[0-9]{4}$'),
    customer_id    text not null references customers (customer_id),
    alias          text not null,
    broker         text not null check (broker in ('KIS')),
    env            text not null check (env in ('PAPER', 'LIVE')),
    account_type   text not null check (account_type in ('GENERAL')),
    credential_ref text not null,
    opened_on      date not null,
    created_at     timestamptz not null default now(),
    constraint trading_accounts_credential_key unique (broker, env, credential_ref)
);

create table if not exists account_status_history (
    id             bigserial primary key,
    account_id     text not null references trading_accounts (account_id),
    status         text not null check (status in ('OPEN', 'SUSPENDED', 'CLOSED')),
    effective_from date not null,
    reason         text,
    created_at     timestamptz not null default now()
);

-- 원장은 하나, 순번도 하나. 모든 이벤트와 그 파생 행은 계좌 하나에 속한다 (account_id).
create table if not exists ledger_events (
    seq         bigint primary key check (seq > 0),
    event_type  text not null,
    payload     jsonb not null,
    recorded_at timestamptz not null default now(),
    account_id  text not null references trading_accounts (account_id),
    constraint ledger_events_seq_account_key unique (seq, account_id)
);
create index if not exists ledger_events_account_idx on ledger_events (account_id, seq);

create table if not exists journal_entries (
    entry_id          bigserial primary key,
    event_seq         bigint not null,
    date              date not null,
    kind              text not null,
    source_id         text not null,
    reverses_entry_id bigint references journal_entries (entry_id),
    account_id        text not null,
    constraint journal_entries_event_account_fkey
        foreign key (event_seq, account_id) references ledger_events (seq, account_id)
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
    event_seq    bigint not null,
    trade_date   date not null,
    symbol       text not null,
    exec_id      text not null,
    qty_delta    bigint not null,
    cost_delta   bigint not null,
    realized_pnl bigint,
    account_id   text not null,
    constraint position_ledger_event_account_fkey
        foreign key (event_seq, account_id) references ledger_events (seq, account_id),
    constraint position_ledger_account_exec_key unique (account_id, exec_id)
);

create table if not exists cash_ledger (
    id        bigserial primary key,
    event_seq  bigint not null,
    date       date not null,
    reason     text not null,
    amount     bigint not null,
    ref_id     text not null,
    account_id text not null,
    constraint cash_ledger_event_account_fkey
        foreign key (event_seq, account_id) references ledger_events (seq, account_id)
);

-- v2.2 일 마감 (스펙 D6, R-77~79): 마감 이벤트(DayClosedEvent)마다 한 행. 그날의 확정 기준가를 함께 남긴다.
-- 마감일과 그 이전 날짜로는 전표와 예수금 행이 더 들어가지 않는다 (아래 ministp_forbid_closed_day).
create table if not exists day_closes (
    account_id   text not null,
    date         date not null,
    event_seq    bigint not null,
    net_assets   bigint not null,
    units        bigint not null check (units >= 0),
    nav_per_1000 numeric(18, 2) not null,
    closed_at    timestamptz not null default now(),
    primary key (account_id, date),
    constraint day_closes_event_key unique (event_seq),
    constraint day_closes_event_account_fkey
        foreign key (event_seq, account_id) references ledger_events (seq, account_id)
);

-- v2.3 감사 로그: 누가, 언제, 어디로, 무엇을 했고 어떻게 끝났는지. 거부된 요청도 한 줄이 남는다.
-- 번호, 시각, 앞 행의 해시와 자기 해시는 DB가 채운다 (아래 ministp_audit_chain). 인증키와 계좌번호는 넣지 않는다.
create table if not exists audit_log (
    id         bigint primary key check (id > 0),
    at         timestamptz not null,
    channel    text not null check (channel in ('CLI', 'UI', 'SYSTEM')),
    actor      text not null check (actor <> ''),
    action     text not null check (action <> ''),
    outcome    text not null check (outcome in ('DONE', 'REFUSED', 'FAILED')),
    account_id text references trading_accounts (account_id),
    target     text,
    detail     jsonb not null default '{}',
    message    text,
    prev_hash  text not null,
    hash       text not null,
    constraint audit_log_hash_key unique (hash)
);
create index if not exists audit_log_account_idx on audit_log (account_id, id);

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
    created_at  timestamptz not null default now(),
    account_id  text not null references trading_accounts (account_id)
);

create table if not exists oms_order_acks (
    client_id  text primary key references oms_orders (client_id),
    outcome    text not null check (outcome in ('ACCEPTED', 'REJECTED')),
    order_no   text,
    org_no     text,
    detail     text,
    created_at timestamptz not null default now(),
    check ((outcome = 'ACCEPTED') = (order_no is not null))
);
-- v1.1: broker order numbers restart every business day, so they are unique only
-- within a day (client_id, which carries the date, is the key). Drops the v1.0 constraint.
alter table oms_order_acks drop constraint if exists oms_order_acks_order_no_key;
create index if not exists oms_order_acks_order_no_idx on oms_order_acks (order_no);

-- R-70: broker answers are stored as received, before comparing.
create table if not exists broker_snapshots (
    snapshot_id bigserial primary key,
    date        date not null,
    kind        text not null,
    payload     jsonb not null,
    taken_at    timestamptz not null default now(),
    account_id  text not null references trading_accounts (account_id),
    constraint broker_snapshots_id_account_key unique (snapshot_id, account_id)
);

-- R-72~73: one row per item per run; a later judgement is a new row (supersedes_id).
create table if not exists recon_results (
    id             bigserial primary key,
    run_id         bigint not null,
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
    created_at     timestamptz not null default now(),
    account_id     text not null,
    constraint recon_results_run_account_fkey
        foreign key (run_id, account_id) references broker_snapshots (snapshot_id, account_id)
);

-- 공통코드 (v2.1): 코드와 코드명. 원장이 직접 참조하는 코드(계정과목)는 전용 테이블에 둔다.
create table if not exists code_groups (
    group_id    text primary key check (group_id ~ '^[A-Z][A-Z0-9_]{1,39}$'),
    name        text not null check (name <> ''),
    description text,
    used_by     text,
    fixed       boolean not null default true,
    created_at  timestamptz not null default now()
);

create table if not exists common_codes (
    group_id    text not null references code_groups (group_id),
    code        text not null check (code <> ''),
    name        text not null check (name <> ''),
    description text,
    sort_order  integer not null default 0,
    in_use      boolean not null default true,
    updated_at  timestamptz not null default now(),
    primary key (group_id, code)
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
                             'oms_order_acks', 'broker_snapshots', 'recon_results',
                             'customers', 'trading_accounts', 'account_status_history', 'day_closes',
                             'audit_log'] loop
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

-- R-78: a closed day takes no more entries. An entry, a cash row or another close dated on or
-- before a close of the same account is refused, whoever tries (the program books a correction
-- on the open day instead).
create or replace function ministp_forbid_closed_day() returns trigger language plpgsql as $$
begin
    if exists (select 1 from day_closes c where c.account_id = new.account_id and c.date >= new.date) then
        raise exception '%: % is inside a closed day of account % (book a correction on the open day)',
            tg_table_name, new.date, new.account_id using errcode = 'check_violation';
    end if;
    return new;
end $$;

do $$
declare t text;
begin
    foreach t in array array['journal_entries', 'cash_ledger', 'day_closes'] loop
        if not exists (select 1 from pg_trigger where tgname = t || '_closed_day') then
            execute format('create trigger %I before insert on %I
                            for each row execute function ministp_forbid_closed_day()', t || '_closed_day', t);
        end if;
    end loop;
end $$;

-- 감사 로그의 해시 사슬. 한 행의 해시는 앞 행의 해시와 자기 내용 전체로 계산한다.
-- 가운데 행을 고치거나 지우면 그 뒤의 사슬이 맞지 않게 된다 (verify가 확인).
create or replace function ministp_audit_hash(prev text, r audit_log) returns text language sql stable as $$
    select encode(sha256(convert_to(jsonb_build_array(
        prev, r.id, to_char(r.at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), r.channel, r.actor,
        r.action, r.outcome, r.account_id, r.target, r.detail, r.message)::text, 'UTF8')), 'hex')
$$;

create or replace function ministp_audit_chain() returns trigger language plpgsql as $$
declare
    last_id bigint; last_hash text;
begin
    perform pg_advisory_xact_lock(7412002);   -- one row at a time: the chain has one end
    select id, hash into last_id, last_hash from audit_log order by id desc limit 1;
    new.id := coalesce(last_id, 0) + 1;
    new.at := clock_timestamp();
    new.prev_hash := coalesce(last_hash, repeat('0', 64));
    new.hash := ministp_audit_hash(new.prev_hash, new);
    return new;
end $$;

do $$
begin
    if not exists (select 1 from pg_trigger where tgname = 'audit_log_chain') then
        create trigger audit_log_chain before insert on audit_log
            for each row execute function ministp_audit_chain();
    end if;
end $$;

-- A code is renamed or switched off, never removed: old rows still carry it.
create or replace function ministp_guard_codes() returns trigger language plpgsql as $$
begin
    if tg_op = 'UPDATE' and tg_table_name = 'common_codes' then
        if new.group_id = old.group_id and new.code = old.code then
            new.updated_at := now();
            return new;
        end if;
    elsif tg_op = 'UPDATE' then
        if new.group_id = old.group_id and new.fixed = old.fixed then
            return new;
        end if;
    end if;
    raise exception 'code table %: % is not allowed (a code is renamed or switched off, never removed)',
        tg_table_name, tg_op using errcode = 'insufficient_privilege';
end $$;

do $$
declare t text;
begin
    foreach t in array array['code_groups', 'common_codes'] loop
        if not exists (select 1 from pg_trigger where tgname = t || '_guard') then
            execute format('create trigger %I before update or delete on %I
                            for each row execute function ministp_guard_codes()', t || '_guard', t);
            execute format('create trigger %I before truncate on %I
                            for each statement execute function ministp_guard_codes()', t || '_no_truncate', t);
        end if;
    end loop;
end $$;

create or replace view account_balances_v as
select t.account_id, a.code, a.name, a.normal_side,
       case when a.normal_side = 'D'
            then coalesce(sum(l.debit - l.credit), 0)
            else coalesce(sum(l.credit - l.debit), 0) end as balance
  from trading_accounts t
 cross join accounts_chart a
  left join journal_entries e on e.account_id = t.account_id
  left join journal_lines l on l.entry_id = e.entry_id and l.account_code = a.code
 group by t.account_id, a.code, a.name, a.normal_side;

create or replace view positions_v as
select account_id, symbol, sum(qty_delta) as qty, sum(cost_delta) as book_cost
  from position_ledger group by account_id, symbol having sum(qty_delta) <> 0;

create or replace view cash_balance_v as
select t.account_id, coalesce(sum(c.amount), 0) as balance
  from trading_accounts t left join cash_ledger c on c.account_id = t.account_id
 group by t.account_id;

create or replace view account_status_v as
select t.account_id, t.customer_id, t.alias, s.status, s.effective_from
  from trading_accounts t
  join lateral (select h.status, h.effective_from from account_status_history h
                 where h.account_id = t.account_id order by h.effective_from desc, h.id desc limit 1) s on true;

create or replace view customer_positions_v as
select t.customer_id, p.symbol, sum(p.qty_delta) as qty, sum(p.cost_delta) as book_cost
  from position_ledger p join trading_accounts t on t.account_id = p.account_id
 group by t.customer_id, p.symbol having sum(p.qty_delta) <> 0;

-- ABOR (스펙 D6, R-79): 마지막 마감이 남긴 장부. 그 뒤에 쌓인 이벤트는 들어 있지 않다.
create or replace view last_close_v as
select distinct on (account_id) account_id, date, event_seq, net_assets, units, nav_per_1000, closed_at
  from day_closes order by account_id, date desc;

create or replace view abor_positions_v as
select c.account_id, c.date as closed_date, p.symbol, sum(p.qty_delta) as qty, sum(p.cost_delta) as book_cost
  from last_close_v c join position_ledger p on p.account_id = c.account_id and p.event_seq <= c.event_seq
 group by c.account_id, c.date, p.symbol having sum(p.qty_delta) <> 0;

-- IBOR와 ABOR의 차이는 이 이벤트들로만 설명된다.
create or replace view unclosed_events_v as
select e.account_id, e.seq, e.event_type, e.recorded_at
  from ledger_events e left join last_close_v c on c.account_id = e.account_id
 where c.event_seq is null or e.seq > c.event_seq;

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
comment on column oms_order_acks.order_no is '증권사 주문번호 (영업일마다 다시 시작될 수 있음)';
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

comment on table customers is '고객 (별칭만 관리, 개인정보 없음)';
comment on column customers.customer_id is '고객 ID';
comment on column customers.alias is '고객 별칭';
comment on column customers.created_at is '등록 시각';

comment on table trading_accounts is '위탁계좌 (계좌번호·인증키는 DB에 없음)';
comment on column trading_accounts.account_id is '계좌 ID';
comment on column trading_accounts.customer_id is '고객 ID';
comment on column trading_accounts.alias is '계좌 별칭';
comment on column trading_accounts.broker is '증권사 (KIS)';
comment on column trading_accounts.env is '환경 (PAPER 모의 / LIVE 실전)';
comment on column trading_accounts.account_type is '계좌 유형 (GENERAL 위탁종합)';
comment on column trading_accounts.credential_ref is '인증정보 참조 (PC 환경변수 이름 머리말)';
comment on column trading_accounts.opened_on is '개설일';
comment on column trading_accounts.created_at is '등록 시각';

comment on table account_status_history is '계좌 상태 이력 (변경은 새 행으로 추가)';
comment on column account_status_history.id is '행 번호';
comment on column account_status_history.account_id is '계좌 ID';
comment on column account_status_history.status is '상태 (OPEN 정상 / SUSPENDED 정지 / CLOSED 해지)';
comment on column account_status_history.effective_from is '적용 시작일';
comment on column account_status_history.reason is '사유';
comment on column account_status_history.created_at is '기록 시각';

comment on column ledger_events.account_id is '계좌 ID';
comment on column journal_entries.account_id is '계좌 ID';
comment on column position_ledger.account_id is '계좌 ID';
comment on column position_ledger.exec_id is '체결 ID (계좌 안에서 유일, 기초 보유는 OPEN-종목코드)';
comment on column cash_ledger.account_id is '계좌 ID';
comment on column oms_orders.account_id is '계좌 ID';
comment on column broker_snapshots.account_id is '계좌 ID';
comment on column recon_results.account_id is '계좌 ID';

comment on view account_balances_v is '계좌별·계정별 잔액 (시산표)';
comment on column account_balances_v.account_id is '계좌 ID';
comment on column account_balances_v.code is '계정코드';
comment on column account_balances_v.name is '계정명';
comment on column account_balances_v.normal_side is '정상잔액 방향';
comment on column account_balances_v.balance is '잔액';

comment on view positions_v is '계좌별·종목별 보유 현황';
comment on column positions_v.account_id is '계좌 ID';
comment on column positions_v.symbol is '종목코드';
comment on column positions_v.qty is '보유수량';
comment on column positions_v.book_cost is '장부금액 (매입금액)';

comment on view cash_balance_v is '계좌별 예수금 잔액';
comment on column cash_balance_v.account_id is '계좌 ID';
comment on column cash_balance_v.balance is '잔액';

comment on view account_status_v is '계좌별 현재 상태';
comment on column account_status_v.account_id is '계좌 ID';
comment on column account_status_v.customer_id is '고객 ID';
comment on column account_status_v.alias is '계좌 별칭';
comment on column account_status_v.status is '현재 상태';
comment on column account_status_v.effective_from is '상태 적용 시작일';

comment on view customer_positions_v is '고객별·종목별 보유 합계 (여러 계좌 합산)';
comment on column customer_positions_v.customer_id is '고객 ID';
comment on column customer_positions_v.symbol is '종목코드';
comment on column customer_positions_v.qty is '보유수량';
comment on column customer_positions_v.book_cost is '장부금액 (매입금액)';

comment on table code_groups is '공통코드 그룹';
comment on column code_groups.group_id is '코드 그룹 ID';
comment on column code_groups.name is '코드 그룹명';
comment on column code_groups.description is '코드 그룹 설명';
comment on column code_groups.used_by is '쓰는 곳 (이 코드를 저장하는 테이블.컬럼)';
comment on column code_groups.fixed is '코드 고정 여부 (프로그램이 정한 코드는 이름만 바꿀 수 있음)';
comment on column code_groups.created_at is '등록 시각';

comment on table common_codes is '공통코드 (코드와 코드명)';
comment on column common_codes.group_id is '코드 그룹 ID';
comment on column common_codes.code is '코드';
comment on column common_codes.name is '코드명';
comment on column common_codes.description is '코드 설명';
comment on column common_codes.sort_order is '표시 순서';
comment on column common_codes.in_use is '사용 여부';
comment on column common_codes.updated_at is '수정 시각';

comment on table day_closes is '일 마감 (마감 이벤트마다 한 행, 그날의 확정 기준가)';
comment on column day_closes.account_id is '계좌 ID';
comment on column day_closes.date is '마감일 (이 날짜까지 장부 마감)';
comment on column day_closes.event_seq is '마감 이벤트 순번';
comment on column day_closes.net_assets is '마감 순자산';
comment on column day_closes.units is '마감 좌수';
comment on column day_closes.nav_per_1000 is '마감 기준가 (1,000좌당)';
comment on column day_closes.closed_at is '마감 기록 시각';

comment on view last_close_v is '계좌별 마지막 마감';
comment on column last_close_v.account_id is '계좌 ID';
comment on column last_close_v.date is '마지막 마감일';
comment on column last_close_v.event_seq is '마감 이벤트 순번';
comment on column last_close_v.net_assets is '마감 순자산';
comment on column last_close_v.units is '마감 좌수';
comment on column last_close_v.nav_per_1000 is '마감 기준가 (1,000좌당)';
comment on column last_close_v.closed_at is '마감 기록 시각';

comment on view abor_positions_v is '마지막 마감 시점의 계좌별·종목별 보유 (ABOR)';
comment on column abor_positions_v.account_id is '계좌 ID';
comment on column abor_positions_v.closed_date is '마감일';
comment on column abor_positions_v.symbol is '종목코드';
comment on column abor_positions_v.qty is '마감 보유수량';
comment on column abor_positions_v.book_cost is '마감 장부금액 (매입금액)';

comment on view unclosed_events_v is '마지막 마감 뒤에 쌓인 이벤트 (IBOR와 ABOR의 차이)';
comment on column unclosed_events_v.account_id is '계좌 ID';
comment on column unclosed_events_v.seq is '이벤트 순번';
comment on column unclosed_events_v.event_type is '이벤트 유형';
comment on column unclosed_events_v.recorded_at is '기록 시각';

comment on table audit_log is '감사 로그 (누가 언제 무엇을 했고 어떻게 끝났는지, 수정·삭제 불가)';
comment on column audit_log.id is '행 번호 (1부터 빈 번호 없이)';
comment on column audit_log.at is '기록 시각';
comment on column audit_log.channel is '경로 (CLI 명령 / UI 화면 / SYSTEM 시스템)';
comment on column audit_log.actor is '실행한 PC 계정';
comment on column audit_log.action is '행위 코드';
comment on column audit_log.outcome is '결과 (DONE 처리 / REFUSED 거부 / FAILED 실패)';
comment on column audit_log.account_id is '계좌 ID (계좌와 무관한 행위는 비움)';
comment on column audit_log.target is '대상 (주문번호, 대사 행 번호, 코드 등)';
comment on column audit_log.detail is '입력 내용 (인증키·계좌번호 제외)';
comment on column audit_log.message is '결과 메시지 (거부·실패 사유)';
comment on column audit_log.prev_hash is '앞 행의 해시';
comment on column audit_log.hash is '이 행의 해시 (앞 행 해시 + 이 행 내용)';

-- 마이그레이션 적용 기록. 새 DB는 이 스키마로 만들어지면서 그때까지의 마이그레이션이 적용된 것으로 기록된다.
create table if not exists schema_migrations (
    version     integer primary key check (version > 0),
    name        text not null,
    erd_version text,
    checksum    text not null,
    applied_at  timestamptz not null default now()
);
comment on table schema_migrations is '스키마 마이그레이션 적용 기록';
comment on column schema_migrations.version is '마이그레이션 번호';
comment on column schema_migrations.name is '마이그레이션 이름';
comment on column schema_migrations.erd_version is '적용 후 ERD 버전';
comment on column schema_migrations.checksum is '적용한 파일의 해시';
comment on column schema_migrations.applied_at is '적용 시각';
