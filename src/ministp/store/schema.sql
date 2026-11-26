-- MiniSTP ledger schema (spec 03).
-- ERD version: 2.14  (bump on every schema change, then run scripts/gen_erd.py) Append-only tables are enforced in the DB,
-- not just in application code.

create table if not exists accounts_chart (
    code        text primary key check (code ~ '^[1-5][0-9]{3}$'),   -- 채번 규칙 (스펙 D4): 대분류, 중분류, 순번, 세목
    name        text not null,
    normal_side char(1) not null check (normal_side in ('D', 'C')),
    in_use      boolean not null default true              -- v2.13: 사용 중지한 계정은 지난 전표만 가리킴
);

-- v2.0 다계좌 (스펙 D7): 법인 -> 위탁계좌 (v2.14: 고객이 법인으로). 계좌번호와 인증키는 DB에 넣지 않는다.
create table if not exists companies (
    company_id  text primary key check (company_id ~ '^C[0-9]{4}$'),
    alias       text not null,
    created_at  timestamptz not null default now()
);

create table if not exists trading_accounts (
    account_id     text primary key check (account_id ~ '^A[0-9]{4}$'),
    company_id     text not null references companies (company_id),
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

-- v2.14: 법인의 ERP 회사코드. 바꿀 때는 새 행을 더하고, 날짜마다 그날 적용되는 코드를 쓴다.
create table if not exists company_erp_codes (
    id               bigserial primary key,
    company_id       text not null references companies (company_id),
    erp_company_code text not null check (erp_company_code ~ '^[A-Za-z0-9_-]{1,20}$'),
    effective_from   date not null,
    reason           text,
    created_at       timestamptz not null default now()
);
create index if not exists company_erp_codes_company_idx on company_erp_codes (company_id, effective_from);

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
-- v2.5: 대체 번호 하나에 보낸 쪽 이벤트 하나, 받은 쪽 이벤트 하나 (R-65).
create unique index if not exists ledger_events_transfer_key on ledger_events ((payload ->> 'transfer_id'), event_type)
    where event_type in ('TransferOutEvent', 'TransferInEvent');
-- v2.6: 조정 번호 하나에 조정 이벤트 하나, 취소 이벤트 하나 (R-66).
create unique index if not exists ledger_events_adjustment_key on ledger_events ((payload ->> 'adjustment_id'), event_type)
    where event_type in ('AdjustmentEvent', 'AdjustmentCancelEvent');

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

-- 권리 원장 (ERD v2.4, R-57~59). 종목에 생긴 현금배당을 한 번 등록하면 모든 계좌의 daily가 읽는다.
-- 행은 고치지 않는다. 잘못 넣었거나 취소된 권리는 취소 행(action_type = 'CANCEL')을 더해 무효로 한다.
create table if not exists corporate_actions (
    action_id        bigserial primary key,
    symbol           text not null references instruments (symbol),
    action_type      text not null check (action_type in ('CASH_DIVIDEND', 'CANCEL')),
    record_date      date,
    pay_date         date,
    amount_per_share numeric(18,2) check (amount_per_share > 0),
    cancels_id       bigint references corporate_actions (action_id),
    note             text,
    registered_at    timestamptz not null default now(),
    constraint corporate_actions_cancel_key unique (cancels_id),
    constraint corporate_actions_dividend_check check ((action_type = 'CASH_DIVIDEND')
        = (record_date is not null and pay_date is not null and amount_per_share is not null and cancels_id is null)),
    constraint corporate_actions_cancel_check check ((action_type = 'CANCEL') = (cancels_id is not null)),
    constraint corporate_actions_dates_check check (pay_date >= record_date)
);
create index if not exists corporate_actions_symbol_idx on corporate_actions (symbol, record_date);

-- 종목 마스터 이력 (ERD v2.7, R-94~95). 시장 전 종목의 속성(가격 제외)이 바뀐 날에만 한 행이 들어간다.
-- 적재(master load) 한 번에 master_loads 한 행. 등록 종목(instruments)과는 FK로 묶지 않고 대조 뷰가 맞춰 본다.
create table if not exists master_loads (
    load_id       bigserial primary key,
    base_date     date not null,
    fetched_at    timestamptz not null,
    kospi_sha256  text not null check (kospi_sha256 ~ '^[0-9a-f]{64}$'),
    kosdaq_sha256 text not null check (kosdaq_sha256 ~ '^[0-9a-f]{64}$'),
    rows_total    integer not null check (rows_total >= 0),
    rows_new      integer not null check (rows_new >= 0),
    rows_changed  integer not null check (rows_changed >= 0),
    rows_delisted integer not null check (rows_delisted >= 0),
    loaded_at     timestamptz not null default now(),
    constraint master_loads_base_date_key unique (base_date),
    constraint master_loads_files_key unique (kospi_sha256, kosdaq_sha256)
);

-- 2. 종목 마스터 이력: 속성이 바뀐 날에만 한 행 (가격은 담지 않는다) ------------------------------
create table if not exists instrument_master (
    master_id       bigserial primary key,
    symbol          text not null,
    valid_from      date not null,
    load_id         bigint not null references master_loads (load_id),
    listed          boolean not null,
    isin            text not null check (isin ~ '^[A-Z0-9]{12}$'),
    name            text not null check (name <> ''),
    market          text not null check (market in ('KOSPI', 'KOSDAQ')),
    sec_group       text not null check (sec_group ~ '^[A-Z]{2}$'),
    lot             integer not null check (lot >= 0),
    par_value       bigint not null check (par_value >= 0),
    listed_on       date,
    listed_shares_k bigint not null check (listed_shares_k >= 0),
    preferred       text not null,
    spac            boolean not null,
    halted          boolean not null,
    liquidation     boolean not null,
    managed         boolean not null,
    warning         text not null,
    constraint instrument_master_symbol_key unique (symbol, valid_from)
);
create index if not exists instrument_master_load_idx on instrument_master (load_id);

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

-- 주문 결재 (ERD v2.8, 스펙 D9, R-99~101): 사용자, 결재 권한 이력, 주문 요청(주문일 + 요청 번호)과 줄,
-- 결재 이력(요청 단위), 전송 이력(줄 단위). 모두 추가만 한다.
create table if not exists app_users (
    user_id    text primary key check (user_id ~ '^[^[:space:]]+$'),
    name       text not null check (name <> ''),
    created_by text not null check (created_by <> ''),
    created_at timestamptz not null default now()
);

create table if not exists user_rights (
    right_row_id    bigserial primary key,
    user_id         text not null references app_users (user_id),
    account_id      text references trading_accounts (account_id),
    right_code      text not null check (right_code in ('ORDER_WRITE', 'ORDER_APPROVE', 'RIGHTS_ADMIN')),
    granted         boolean not null,
    max_order_value bigint check (max_order_value > 0),
    self_approve    boolean not null default false,
    effective_from  date not null,
    granted_by      text not null check (granted_by <> ''),
    reason          text,
    created_at      timestamptz not null default now(),
    check (granted or (max_order_value is null and not self_approve)),
    check (right_code = 'ORDER_APPROVE' or (max_order_value is null and not self_approve))
);
create index if not exists user_rights_user_idx on user_rights (user_id, right_code);

create table if not exists order_requests (
    trade_date  date not null,
    request_no  integer not null check (request_no > 0),
    account_id  text not null references trading_accounts (account_id),
    order_type  text not null check (order_type in ('SINGLE', 'BASKET')),
    created_by  text not null references app_users (user_id),
    created_at  timestamptz not null default now(),
    primary key (trade_date, request_no)
);
create index if not exists order_requests_account_idx on order_requests (account_id, trade_date);

create table if not exists order_request_lines (
    trade_date  date not null,
    request_no  integer not null,
    line_no     integer not null check (line_no > 0),
    symbol      text not null references instruments (symbol),
    side        text not null check (side in ('BUY', 'SELL')),
    qty         bigint not null check (qty > 0),
    limit_price bigint check (limit_price > 0),
    primary key (trade_date, request_no, line_no),
    foreign key (trade_date, request_no) references order_requests (trade_date, request_no)
);

create table if not exists order_approvals (
    id            bigserial primary key,
    trade_date    date not null,
    request_no    integer not null,
    action        text not null check (action in ('REQUEST', 'APPROVE', 'REJECT', 'CANCEL', 'EXPIRE')),
    actor         text references app_users (user_id),
    self_approved boolean not null default false,
    reason        text,
    set_check     jsonb not null default '{}',
    at            timestamptz not null default now(),
    foreign key (trade_date, request_no) references order_requests (trade_date, request_no),
    check (actor is not null or action = 'EXPIRE'),
    check (action <> 'REJECT' or coalesce(reason, '') <> ''),
    check (self_approved = false or action = 'APPROVE')
);
create index if not exists order_approvals_request_idx on order_approvals (trade_date, request_no, id);

create table if not exists order_sends (
    id          bigserial primary key,
    trade_date  date not null,
    request_no  integer not null,
    line_no     integer not null,
    outcome     text not null check (outcome in ('SENT', 'FAILED')),
    client_id   text unique references oms_orders (client_id),
    reason      text,
    set_check   jsonb not null default '{}',
    at          timestamptz not null default now(),
    foreign key (trade_date, request_no, line_no) references order_request_lines (trade_date, request_no, line_no),
    constraint order_sends_line_key unique (trade_date, request_no, line_no),
    check ((outcome = 'SENT') = (client_id is not null)),
    check (outcome <> 'FAILED' or coalesce(reason, '') <> '')
);

-- ERP 전송 이력 (ERD v2.11, R-104): 내보낸 파일마다 한 행, 그 파일의 전표마다 한 행, ERP가 받지 않은 파일의 무효.
create table if not exists erp_exports (
    export_id      bigserial primary key,
    account_id     text references trading_accounts (account_id),
    date_from      date not null,
    date_to        date not null,
    unit           text not null check (unit in ('ENTRY', 'DAY')),
    company_code   text not null,
    document_type  text not null,
    mapping_sha256 text check (mapping_sha256 ~ '^[0-9a-f]{64}$'),
    documents      integer not null check (documents > 0),
    lines          integer not null check (lines > 0),
    debit_total    bigint not null check (debit_total >= 0),
    credit_total   bigint not null,
    file_name      text not null check (file_name <> ''),
    file_sha256    text not null check (file_sha256 ~ '^[0-9a-f]{64}$'),
    created_by     text not null check (created_by <> ''),
    created_at     timestamptz not null default now(),
    check (date_from <= date_to),
    check (debit_total = credit_total)
);

create table if not exists erp_export_docs (
    export_id  bigint not null references erp_exports (export_id),
    doc_no     text not null check (doc_no ~ '^MSTP-'),
    account_id text not null references trading_accounts (account_id),
    doc_date   date not null,
    primary key (export_id, doc_no)
);
create index if not exists erp_export_docs_doc_idx on erp_export_docs (doc_no);
create index if not exists erp_export_docs_day_idx on erp_export_docs (account_id, doc_date);

create table if not exists erp_export_voids (
    export_id  bigint primary key references erp_exports (export_id),
    reason     text not null check (reason <> ''),
    voided_by  text not null check (voided_by <> ''),
    voided_at  timestamptz not null default now()
);

-- 마감 점검 (ERD v2.12, R-105): 단계 기준(고치되 지우지 않음)과 점검할 때마다 남는 결과.
create table if not exists close_steps (
    step_code   text primary key check (step_code ~ '^[A-Z][A-Z0-9_]{1,39}$'),
    name        text not null check (name <> ''),
    sort_order  integer not null check (sort_order > 0),
    in_use      boolean not null default true,
    required    boolean not null default true,
    screen      text check (screen ~ '^STP-[0-9]{4}$'),
    asset_type  text,
    description text not null default '',
    updated_at  timestamptz not null default now()
);

create table if not exists close_checks (
    check_id   bigserial primary key,
    account_id text not null references trading_accounts (account_id),
    check_date date not null,
    day_state  text not null check (day_state in ('DONE', 'OPEN', 'ATTENTION')),
    done       integer not null check (done >= 0),
    steps      integer not null check (steps >= done),
    command    text not null check (command <> ''),
    checked_by text not null check (checked_by <> ''),
    checked_at timestamptz not null default now()
);
create index if not exists close_checks_day_idx on close_checks (account_id, check_date, check_id);

create table if not exists close_check_steps (
    check_id  bigint not null references close_checks (check_id),
    step_no   integer not null check (step_no > 0),
    step_code text not null references close_steps (step_code),
    name      text not null,
    required  boolean not null,
    state     text not null check (state in ('DONE', 'NONE', 'WAIT', 'WARN', 'BAD', 'MISSING')),
    detail    text not null,
    primary key (check_id, step_no)
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
                             'companies', 'trading_accounts', 'account_status_history', 'day_closes',
                             'audit_log', 'corporate_actions', 'master_loads', 'instrument_master',
                             'app_users', 'user_rights', 'order_requests', 'order_request_lines',
                             'order_approvals', 'order_sends', 'erp_exports', 'erp_export_docs',
                             'erp_export_voids', 'close_checks', 'close_check_steps', 'company_erp_codes'] loop
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

-- 결재 흐름 (ERD v2.8, R-100): 요청 → 승인·반려·취소, 만료. 승인된 요청의 줄마다 전송·전송 실패 한 번. 그 밖의 순서는 DB가 거부한다.
-- 같은 요청에 두 사람이 동시에 손대도 하나만 통과하도록 요청마다 잠그고 읽는다.
-- 요청과 취소는 작성자만, 셀프 승인 표시는 DB가 작성자와 승인자를 비교해 채운다(프로그램이 넣은 값은 무시).
create or replace function ministp_order_approval_flow() returns trigger language plpgsql as $$
declare
    last_action text;
    writer text;
    key text := new.trade_date || '-' || new.request_no;
begin
    perform pg_advisory_xact_lock(hashtext('ministp.order_request:' || key));
    select created_by into writer from order_requests where trade_date = new.trade_date and request_no = new.request_no;
    select action into last_action from order_approvals
     where trade_date = new.trade_date and request_no = new.request_no order by id desc limit 1;
    if not ((new.action = 'REQUEST' and last_action is null)
         or (new.action in ('APPROVE', 'REJECT') and last_action = 'REQUEST')
         or (new.action in ('CANCEL', 'EXPIRE') and last_action in ('REQUEST', 'APPROVE'))) then
        raise exception 'order request %: % after % is not allowed', key, new.action,
            coalesce(last_action, 'nothing') using errcode = 'check_violation';
    end if;
    if new.action in ('CANCEL', 'EXPIRE') and exists (select 1 from order_sends s
            where s.trade_date = new.trade_date and s.request_no = new.request_no) then
        raise exception 'order request %: % after a line was sent is not allowed', key, new.action
            using errcode = 'check_violation';
    end if;
    if new.action = 'REQUEST' and not exists (select 1 from order_request_lines l
            where l.trade_date = new.trade_date and l.request_no = new.request_no) then
        raise exception 'order request %: no lines', key using errcode = 'check_violation';
    end if;
    if new.action in ('REQUEST', 'CANCEL') and new.actor is distinct from writer then
        raise exception 'order request %: only its writer % may %', key, writer, new.action
            using errcode = 'check_violation';
    end if;
    new.self_approved := new.action = 'APPROVE' and new.actor = writer;
    return new;
end $$;

-- 줄은 요청과 함께 쓰고(결재 요청 전), 결재가 시작된 요청에는 줄을 더하지 못한다.
create or replace function ministp_order_lines_fixed() returns trigger language plpgsql as $$
begin
    if exists (select 1 from order_approvals a where a.trade_date = new.trade_date and a.request_no = new.request_no) then
        raise exception 'order request %-%: lines are fixed once it is requested', new.trade_date, new.request_no
            using errcode = 'check_violation';
    end if;
    return new;
end $$;

-- 전송은 승인된 요청의 줄마다 한 번 (R-101). 승인 뒤 취소·만료와 겹치지 않게 같은 잠금을 잡는다.
create or replace function ministp_order_send_flow() returns trigger language plpgsql as $$
declare
    last_action text;
    key text := new.trade_date || '-' || new.request_no;
begin
    perform pg_advisory_xact_lock(hashtext('ministp.order_request:' || key));
    select action into last_action from order_approvals
     where trade_date = new.trade_date and request_no = new.request_no order by id desc limit 1;
    if last_action is distinct from 'APPROVE' then
        raise exception 'order request %: % after % is not allowed', key, new.outcome,
            coalesce(last_action, 'nothing') using errcode = 'check_violation';
    end if;
    return new;
end $$;

do $$
begin
    if not exists (select 1 from pg_trigger where tgname = 'order_approvals_flow') then
        create trigger order_approvals_flow before insert on order_approvals
            for each row execute function ministp_order_approval_flow();
    end if;
    if not exists (select 1 from pg_trigger where tgname = 'order_request_lines_fixed') then
        create trigger order_request_lines_fixed before insert on order_request_lines
            for each row execute function ministp_order_lines_fixed();
    end if;
    if not exists (select 1 from pg_trigger where tgname = 'order_sends_flow') then
        create trigger order_sends_flow before insert on order_sends
            for each row execute function ministp_order_send_flow();
    end if;
end $$;

-- R-104: 같은 전표를 ERP로 두 번 내지 않는다.
-- 무효가 아닌 내보내기에 같은 증빙번호가 있거나, 같은 계좌·같은 날이 다른 단위로 나갔으면 거부한다.
-- 건별과 일별 합계를 같은 날에 함께 올리면 ERP에 그날이 두 번 잡히기 때문이다. 계좌·날마다 잠그고 읽는다.
create or replace function ministp_erp_doc_once() returns trigger language plpgsql as $$
declare
    my_unit text;
    clash text;
begin
    perform pg_advisory_xact_lock(hashtext('ministp.erp:' || new.account_id || ':' || new.doc_date));
    select unit into my_unit from erp_exports where export_id = new.export_id;
    if exists (select 1 from erp_export_voids v where v.export_id = new.export_id) then
        raise exception 'erp export %: already void', new.export_id using errcode = 'check_violation';
    end if;
    select d.doc_no || ' (내보내기 ' || d.export_id || ')' into clash
      from erp_export_docs d join erp_exports e using (export_id)
     where d.export_id <> new.export_id
       and not exists (select 1 from erp_export_voids v where v.export_id = d.export_id)
       and (d.doc_no = new.doc_no or (d.account_id = new.account_id and d.doc_date = new.doc_date and e.unit <> my_unit))
     limit 1;
    if clash is not null then
        raise exception 'erp document % % already exported as %', new.doc_no, new.account_id, clash
            using errcode = 'unique_violation';
    end if;
    return new;
end $$;

do $$
begin
    if not exists (select 1 from pg_trigger where tgname = 'erp_export_docs_once') then
        create trigger erp_export_docs_once before insert on erp_export_docs
            for each row execute function ministp_erp_doc_once();
    end if;
end $$;

-- R-105: 마감 점검 단계 기준은 고치되 지우지 않는다(지난 점검 결과가 가리킴). 단계 코드도 바꾸지 않는다.
create or replace function ministp_guard_close_steps() returns trigger language plpgsql as $$
begin
    if tg_op = 'UPDATE' and new.step_code = old.step_code then
        new.updated_at := now();
        return new;
    end if;
    raise exception 'close_steps: % of a step is not allowed (rename it or switch it off)', lower(tg_op)
        using errcode = 'insufficient_privilege';
end $$;

do $$
begin
    if not exists (select 1 from pg_trigger where tgname = 'close_steps_guard') then
        create trigger close_steps_guard before update or delete on close_steps
            for each row execute function ministp_guard_close_steps();
        create trigger close_steps_no_truncate before truncate on close_steps
            for each statement execute function ministp_forbid_change();
    end if;
end $$;

insert into close_steps (step_code, name, sort_order, in_use, required, screen, asset_type, description) values
    ('SETTLEMENT', '결제', 1, true, true, 'STP-3400', null, '그날 결제일인 체결이 모두 결제됐는지'),
    ('ORDERS', '주문·체결', 2, true, true, 'STP-1200', null, '낸 주문마다 접수·체결·결제 진행'),
    ('DIVIDENDS', '배당', 3, true, true, 'STP-3200', null, '그날 인식·입금할 배당이 기록됐는지 (R-57~59)'),
    ('PRICES', '시세', 4, true, true, 'STP-2300', null, '보유 종목 종가를 받았고 크게 튄 값이 없는지'),
    ('NAV', '기준가', 5, true, true, 'STP-2210', null, '기준가를 냈고 순자산 변동이 분개로 설명되는지'),
    ('RECON', '증권사 대사', 6, true, true, 'STP-2100', null, '일 마감 대사의 불일치를 모두 판단했는지'),
    ('DAY_CLOSE', '일 마감', 7, true, true, 'STP-2200', null, '장부를 그날까지 마감했는지 (R-77)'),
    ('ERP_EXPORT', 'ERP 전송', 8, true, false, null, null, '그날 전표를 ERP로 내보냈는지 (R-104). 기본은 선택 단계'),
    ('INTEGRITY', '원장 내부 대사', 9, true, true, 'STP-2110', null, '지금 원장이 이벤트를 다시 계산한 결과와 같은지')
on conflict (step_code) do nothing;

-- 계정과목표 v2 (ERD v2.13): 사용 중지한 계정으로는 새 분개 줄이 들어가지 않는다.
create or replace function ministp_account_in_use() returns trigger language plpgsql as $$
begin
    if not (select in_use from accounts_chart where code = new.account_code) then
        raise exception 'account % is retired: no new journal line may use it', new.account_code
            using errcode = 'check_violation';
    end if;
    return new;
end $$;

do $$
begin
    if not exists (select 1 from pg_trigger where tgname = 'journal_lines_account_in_use') then
        create trigger journal_lines_account_in_use before insert on journal_lines
            for each row execute function ministp_account_in_use();
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
select t.account_id, t.company_id, t.alias, s.status, s.effective_from
  from trading_accounts t
  join lateral (select h.status, h.effective_from from account_status_history h
                 where h.account_id = t.account_id order by h.effective_from desc, h.id desc limit 1) s on true;

create or replace view company_positions_v as
select t.company_id, p.symbol, sum(p.qty_delta) as qty, sum(p.cost_delta) as book_cost
  from position_ledger p join trading_accounts t on t.account_id = p.account_id
 group by t.company_id, p.symbol having sum(p.qty_delta) <> 0;

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

-- 권리 목록과 계좌별 배당 현황 (ERD v2.4). 배당 현황은 원장 이벤트에서 바로 읽는다.
create or replace view corporate_actions_v as
select a.action_id, a.symbol, a.action_type, a.record_date, a.pay_date, a.amount_per_share, a.note,
       a.registered_at, c.registered_at as canceled_at, c.note as cancel_reason
  from corporate_actions a left join corporate_actions c on c.cancels_id = a.action_id
 where a.action_type <> 'CANCEL';

create or replace view dividends_v as
select e.account_id, (e.payload ->> 'action_id')::bigint as action_id, max(e.payload ->> 'symbol') as symbol,
       max((e.payload ->> 'qty')::bigint) filter (where e.event_type = 'DividendAccrualEvent') as qty,
       coalesce(max((e.payload ->> 'gross')::bigint) filter (where e.event_type = 'DividendAccrualEvent'), 0)
           as accrued_gross,
       max((e.payload ->> 'date')::date) filter (where e.event_type = 'DividendAccrualEvent') as accrued_on,
       max((e.payload ->> 'gross')::bigint) filter (where e.event_type = 'DividendPaymentEvent') as paid_gross,
       max((e.payload ->> 'tax')::bigint) filter (where e.event_type = 'DividendPaymentEvent') as tax,
       max((e.payload ->> 'date')::date) filter (where e.event_type = 'DividendPaymentEvent') as paid_on,
       max((e.payload ->> 'date')::date) filter (where e.event_type = 'DividendCancelEvent') as canceled_on,
       case when bool_or(e.event_type = 'DividendCancelEvent') then 'CANCELED'
            when bool_or(e.event_type = 'DividendPaymentEvent') then 'PAID' else 'ACCRUED' end as status
  from ledger_events e
 where e.event_type in ('DividendAccrualEvent', 'DividendPaymentEvent', 'DividendCancelEvent')
 group by e.account_id, (e.payload ->> 'action_id')::bigint;

-- v2.5 입출금·대체 (R-56, R-65): 원장 이벤트에서 바로 읽는다 (프로젝션 테이블 없음).
create or replace view cash_movements_v as
select e.account_id, e.seq as event_seq, (e.payload ->> 'date')::date as date,
       case e.event_type when 'DepositEvent' then 'DEPOSIT' when 'WithdrawalEvent' then 'WITHDRAWAL'
                         when 'TransferInEvent' then 'TRANSFER_IN' else 'TRANSFER_OUT' end as kind,
       case when e.event_type in ('DepositEvent', 'TransferInEvent') then 1 else -1 end
           * (e.payload ->> 'amount')::bigint as amount,
       (e.payload ->> 'transfer_id')::bigint as transfer_id,
       coalesce(e.payload ->> 'to_account', e.payload ->> 'from_account') as counter_account
  from ledger_events e
 where e.event_type in ('DepositEvent', 'WithdrawalEvent', 'TransferInEvent', 'TransferOutEvent');

create or replace view transfers_v as
select o.transfer_id, o.date, o.account_id as from_account, i.account_id as to_account, -o.amount as amount,
       o.event_seq as out_seq, i.event_seq as in_seq
  from cash_movements_v o join cash_movements_v i on i.transfer_id = o.transfer_id and i.kind = 'TRANSFER_IN'
 where o.kind = 'TRANSFER_OUT';

-- v2.6 수동 조정 (R-66): 조정 이벤트와 그 취소를 한 행으로. 원장 이벤트에서 바로 읽는다.
create or replace view adjustments_v as
select a.account_id, (a.payload ->> 'adjustment_id')::bigint as adjustment_id, a.seq as event_seq,
       (a.payload ->> 'date')::date as date, a.payload ->> 'reason' as reason,
       (a.payload ->> 'amount')::bigint as amount, (a.payload ->> 'tax')::bigint as tax,
       (a.payload ->> 'amount')::bigint - (a.payload ->> 'tax')::bigint as cash_amount,
       a.payload ->> 'memo' as memo, (a.payload ->> 'recon_result_id')::bigint as recon_result_id,
       (c.payload ->> 'date')::date as canceled_on, c.payload ->> 'memo' as cancel_reason
  from ledger_events a
  left join ledger_events c on c.event_type = 'AdjustmentCancelEvent'
       and c.payload ->> 'adjustment_id' = a.payload ->> 'adjustment_id'
 where a.event_type = 'AdjustmentEvent';

-- v2.7 종목 마스터 (R-94~95): 종목마다 최근 이력 행, 그리고 등록 종목과의 대조.
create or replace view instrument_master_current_v as
select distinct on (m.symbol) m.symbol, m.listed, m.name, m.market, m.sec_group, m.isin, m.lot, m.par_value,
       m.listed_on, m.listed_shares_k, m.preferred, m.spac, m.halted, m.liquidation, m.managed, m.warning,
       m.valid_from, m.load_id
  from instrument_master m
 order by m.symbol, m.valid_from desc;

create or replace view instrument_master_match_v as
select i.symbol, i.name, i.market, i.asset_type,
       c.name as master_name, c.market as master_market, c.sec_group as master_group, c.listed,
       c.halted, c.liquidation, c.managed, c.warning, c.valid_from,
       case when c.symbol is null then 'MISSING'
            when not c.listed then 'DELISTED'
            when c.market <> i.market
              or c.sec_group <> case i.asset_type when 'STOCK' then 'ST' when 'ETF' then 'EF' end then 'DIFFERENT'
            when replace(c.name, ' ', '') <> replace(i.name, ' ', '') then 'RENAMED'
            else 'MATCH' end as status
  from instruments i left join instrument_master_current_v c using (symbol);

-- v2.8 주문 결재 (R-99~100): 지금 권한과 요청마다의 상태.
create or replace view user_rights_v as
select r.user_id, u.name as user_name, r.account_id, r.right_code, r.max_order_value, r.self_approve,
       r.effective_from, r.granted_by, r.reason, r.right_row_id
  from (select distinct on (user_id, coalesce(account_id, ''), right_code) *
          from user_rights
         where effective_from <= current_date
         order by user_id, coalesce(account_id, ''), right_code, effective_from desc, right_row_id desc) r
  join app_users u using (user_id)
 where r.granted;

create or replace view order_requests_v as
select q.trade_date, q.request_no, q.account_id, q.order_type, q.created_by, q.created_at,
       n.lines as line_count, n.amount,
       case when l.action = 'APPROVE' and s.unknown > 0 then 'SEND_UNKNOWN'
            when l.action = 'APPROVE' and s.done = n.lines then case when s.accepted > 0 then 'SENT' else 'SEND_FAILED' end
            else case l.action when 'REQUEST' then 'PENDING' when 'APPROVE' then 'APPROVED' when 'REJECT' then 'REJECTED'
                               when 'CANCEL' then 'CANCELED' when 'EXPIRE' then 'EXPIRED' end end as status,
       a.actor as approved_by, a.self_approved, a.at as approved_at,
       coalesce(s.sent, 0) as sent_lines, coalesce(s.failed, 0) as failed_lines,
       case when l.action = 'APPROVE' and (s.failed > 0 or s.rejected > 0) then s.reason else l.reason end as last_reason,
       greatest(l.at, s.at) as status_at,
       coalesce(s.accepted, 0) as accepted_lines, coalesce(s.rejected, 0) as rejected_lines,
       coalesce(s.unknown, 0) as unknown_lines
  from order_requests q
  join lateral (select count(*) as lines, sum(qty * limit_price) as amount from order_request_lines r
                 where r.trade_date = q.trade_date and r.request_no = q.request_no) n on true
  join lateral (select action, reason, at from order_approvals o
                 where o.trade_date = q.trade_date and o.request_no = q.request_no order by id desc limit 1) l on true
  left join lateral (select actor, self_approved, at from order_approvals o
                      where o.trade_date = q.trade_date and o.request_no = q.request_no and o.action = 'APPROVE'
                      order by id desc limit 1) a on true
  left join lateral (select count(*) as done, count(*) filter (where x.outcome = 'SENT') as sent,
                            count(*) filter (where x.outcome = 'FAILED') as failed,
                            count(*) filter (where k.outcome = 'ACCEPTED') as accepted,
                            count(*) filter (where k.outcome = 'REJECTED') as rejected,
                            count(*) filter (where x.outcome = 'SENT' and k.client_id is null) as unknown,
                            greatest(max(x.at), max(k.created_at)) as at,
                            (array_agg(coalesce(x.reason, k.detail) order by x.id desc)
                               filter (where x.outcome = 'FAILED' or k.outcome = 'REJECTED'))[1] as reason
                       from order_sends x left join oms_order_acks k on k.client_id = x.client_id
                      where x.trade_date = q.trade_date and x.request_no = q.request_no) s on true;

create or replace view order_request_lines_v as
select r.trade_date, r.request_no, r.line_no, q.account_id, q.order_type, r.symbol, r.side, r.qty, r.limit_price,
       r.qty * r.limit_price as amount, s.outcome as send_outcome, s.client_id, s.reason as send_reason, s.at as sent_at,
       k.outcome as ack_outcome, k.order_no, k.detail as ack_detail,
       (s.outcome = 'SENT' and k.client_id is null) as awaiting_ack
  from order_request_lines r
  join order_requests q using (trade_date, request_no)
  left join order_sends s using (trade_date, request_no, line_no)
  left join oms_order_acks k on k.client_id = s.client_id;

create or replace view erp_exports_v as
select e.export_id, e.account_id, e.date_from, e.date_to, e.unit, e.company_code, e.document_type, e.mapping_sha256,
       e.documents, e.lines, e.debit_total, e.credit_total, e.file_name, e.file_sha256, e.created_by, e.created_at,
       case when v.export_id is null then 'EXPORTED' else 'VOIDED' end as status,
       v.reason as void_reason, v.voided_by, v.voided_at
  from erp_exports e
  left join erp_export_voids v using (export_id);

create or replace view close_checks_last_v as
select distinct on (c.account_id, c.check_date)
       c.account_id, c.check_date, c.check_id, c.day_state, c.done, c.steps, c.command, c.checked_by, c.checked_at
  from close_checks c
 order by c.account_id, c.check_date, c.check_id desc;

-- Korean names (한글 명칭). Shown in pgAdmin and in docs/erd. Every table, view and
-- column must have one; tests/db/test_erd.py fails otherwise.
comment on table accounts_chart is '계정과목';
comment on column accounts_chart.code is '계정코드';
comment on column accounts_chart.name is '계정명';
comment on column accounts_chart.normal_side is '정상잔액 방향 (D 차변 / C 대변)';
comment on column accounts_chart.in_use is '사용 여부 (사용 중지한 계정은 지난 전표만 가리키고 새 분개에 쓰지 않음)';

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

comment on table companies is '법인 (장부를 가진 회사, 별칭만 관리)';
comment on column companies.company_id is '법인 ID';
comment on column companies.alias is '법인 별칭';
comment on column companies.created_at is '등록 시각';

comment on table trading_accounts is '위탁계좌 (계좌번호·인증키는 DB에 없음)';
comment on column trading_accounts.account_id is '계좌 ID';
comment on column trading_accounts.company_id is '법인 ID';
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

comment on table company_erp_codes is '법인 ERP 회사코드 이력 (변경은 새 행으로 추가)';
comment on column company_erp_codes.id is '행 번호';
comment on column company_erp_codes.company_id is '법인 ID';
comment on column company_erp_codes.erp_company_code is 'ERP 회사코드';
comment on column company_erp_codes.effective_from is '적용 시작일';
comment on column company_erp_codes.reason is '사유';
comment on column company_erp_codes.created_at is '기록 시각';

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
comment on column account_status_v.company_id is '법인 ID';
comment on column account_status_v.alias is '계좌 별칭';
comment on column account_status_v.status is '현재 상태';
comment on column account_status_v.effective_from is '상태 적용 시작일';

comment on view company_positions_v is '법인별·종목별 보유 합계 (여러 계좌 합산)';
comment on column company_positions_v.company_id is '법인 ID';
comment on column company_positions_v.symbol is '종목코드';
comment on column company_positions_v.qty is '보유수량';
comment on column company_positions_v.book_cost is '장부금액 (매입금액)';

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

comment on table corporate_actions is '권리 원장 (종목에 생긴 현금배당과 그 취소, 수정·삭제 불가)';
comment on column corporate_actions.action_id is '권리 번호';
comment on column corporate_actions.symbol is '종목코드';
comment on column corporate_actions.action_type is '권리 유형 (CASH_DIVIDEND 현금배당 / CANCEL 취소)';
comment on column corporate_actions.record_date is '배당기준일';
comment on column corporate_actions.pay_date is '지급일';
comment on column corporate_actions.amount_per_share is '주당 배당금 (세전, 원)';
comment on column corporate_actions.cancels_id is '취소 대상 권리 번호 (취소 행일 때)';
comment on column corporate_actions.note is '메모 (취소 행은 취소 사유)';
comment on column corporate_actions.registered_at is '등록 시각';

comment on view corporate_actions_v is '권리 목록 (취소 여부 포함)';
comment on column corporate_actions_v.action_id is '권리 번호';
comment on column corporate_actions_v.symbol is '종목코드';
comment on column corporate_actions_v.action_type is '권리 유형';
comment on column corporate_actions_v.record_date is '배당기준일';
comment on column corporate_actions_v.pay_date is '지급일';
comment on column corporate_actions_v.amount_per_share is '주당 배당금 (세전, 원)';
comment on column corporate_actions_v.note is '메모';
comment on column corporate_actions_v.registered_at is '등록 시각';
comment on column corporate_actions_v.canceled_at is '취소 시각 (취소되지 않았으면 비움)';
comment on column corporate_actions_v.cancel_reason is '취소 사유';

comment on view dividends_v is '계좌별 배당 현황 (인식, 입금, 취소)';
comment on column dividends_v.account_id is '계좌 ID';
comment on column dividends_v.action_id is '권리 번호';
comment on column dividends_v.symbol is '종목코드';
comment on column dividends_v.qty is '권리 수량 (기준일까지 결제된 보유)';
comment on column dividends_v.accrued_gross is '인식한 배당금 (세전)';
comment on column dividends_v.accrued_on is '인식일';
comment on column dividends_v.paid_gross is '입금된 배당금 (세전)';
comment on column dividends_v.tax is '원천징수세액';
comment on column dividends_v.paid_on is '입금일';
comment on column dividends_v.canceled_on is '인식 취소일';
comment on column dividends_v.status is '상태 (ACCRUED 인식 / PAID 입금 / CANCELED 취소)';

comment on view cash_movements_v is '입출금·대체 내역 (계좌에 넣거나 뺀 돈, 원장 이벤트에서 계산)';
comment on column cash_movements_v.account_id is '계좌 ID';
comment on column cash_movements_v.event_seq is '원장 이벤트 순번';
comment on column cash_movements_v.date is '기록일 (돈이 움직인 날짜)';
comment on column cash_movements_v.kind is '구분 (DEPOSIT 입금 / WITHDRAWAL 출금 / TRANSFER_IN 대체 입금 / TRANSFER_OUT 대체 출금)';
comment on column cash_movements_v.amount is '금액 (들어오면 +, 나가면 -)';
comment on column cash_movements_v.transfer_id is '대체 번호 (대체일 때)';
comment on column cash_movements_v.counter_account is '상대 계좌 ID (대체일 때)';

comment on view transfers_v is '계좌 간 대체 (보낸 쪽과 받은 쪽을 한 행으로)';
comment on column transfers_v.transfer_id is '대체 번호';
comment on column transfers_v.date is '대체일';
comment on column transfers_v.from_account is '보낸 계좌 ID';
comment on column transfers_v.to_account is '받은 계좌 ID';
comment on column transfers_v.amount is '금액';
comment on column transfers_v.out_seq is '보낸 쪽 원장 이벤트 순번';
comment on column transfers_v.in_seq is '받은 쪽 원장 이벤트 순번';

comment on view adjustments_v is '수동 조정 내역 (예탁금 이용료, 증권사와의 수수료·세금·배당 차이, 취소 여부 포함)';
comment on column adjustments_v.account_id is '계좌 ID';
comment on column adjustments_v.adjustment_id is '조정 번호';
comment on column adjustments_v.event_seq is '원장 이벤트 순번';
comment on column adjustments_v.date is '기록일';
comment on column adjustments_v.reason is '조정 사유 (INTEREST 예탁금 이용료 / FEE 수수료 차이 / TAX 거래세 차이 / DIVIDEND 배당금 차이 / WITHHOLDING 원천징수 차이 / OTHER 기타)';
comment on column adjustments_v.amount is '금액 (세전, 예수금이 늘면 +, 줄면 -)';
comment on column adjustments_v.tax is '원천징수세액 (예탁금 이용료에서 뗀 세금)';
comment on column adjustments_v.cash_amount is '예수금 변동액 (금액 - 세금)';
comment on column adjustments_v.memo is '메모 (조정하는 이유)';
comment on column adjustments_v.recon_result_id is '이 조정으로 정리하는 대사 불일치 행 ID';
comment on column adjustments_v.canceled_on is '취소일 (취소되지 않았으면 비움)';
comment on column adjustments_v.cancel_reason is '취소 사유';

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

comment on table master_loads is '종목 마스터 적재 기록 (KIS 마스터 파일을 DB에 넣은 날마다 한 행, 수정·삭제 불가)';
comment on column master_loads.load_id is '적재 번호';
comment on column master_loads.base_date is '기준일 (파일을 받은 날)';
comment on column master_loads.fetched_at is '파일을 받은 시각';
comment on column master_loads.kospi_sha256 is '코스피 파일 해시 (SHA-256)';
comment on column master_loads.kosdaq_sha256 is '코스닥 파일 해시 (SHA-256)';
comment on column master_loads.rows_total is '종목 수';
comment on column master_loads.rows_new is '신규 (처음 나타난 종목 수)';
comment on column master_loads.rows_changed is '변경 (속성이 바뀐 종목 수)';
comment on column master_loads.rows_delisted is '빠짐 (파일에서 빠진 종목 수)';
comment on column master_loads.loaded_at is '적재 시각';

comment on table instrument_master is '종목 마스터 이력 (시장 전 종목, 속성이 바뀐 날에만 한 행, 수정·삭제 불가)';
comment on column instrument_master.master_id is '이력 번호';
comment on column instrument_master.symbol is '단축코드';
comment on column instrument_master.valid_from is '적용 시작일 (이 속성이 처음 보인 기준일)';
comment on column instrument_master.load_id is '적재 번호';
comment on column instrument_master.listed is '상장 여부 (파일에서 빠지면 거짓인 행이 생김)';
comment on column instrument_master.isin is '표준코드 (ISIN)';
comment on column instrument_master.name is '종목명';
comment on column instrument_master.market is '시장 (KOSPI / KOSDAQ)';
comment on column instrument_master.sec_group is '증권그룹 구분 (ST 주권, EF ETF 등)';
comment on column instrument_master.lot is '매매수량단위';
comment on column instrument_master.par_value is '액면가 (원)';
comment on column instrument_master.listed_on is '상장일';
comment on column instrument_master.listed_shares_k is '상장주수 (천 주)';
comment on column instrument_master.preferred is '우선주 구분 (0 보통주)';
comment on column instrument_master.spac is '스팩 여부';
comment on column instrument_master.halted is '거래정지 여부';
comment on column instrument_master.liquidation is '정리매매 여부';
comment on column instrument_master.managed is '관리종목 여부';
comment on column instrument_master.warning is '시장경고 구분 (00 없음, 01 투자주의, 02 투자경고, 03 투자위험)';

comment on view instrument_master_current_v is '종목 마스터 현재 (종목마다 가장 최근 이력 행)';
comment on column instrument_master_current_v.symbol is '단축코드';
comment on column instrument_master_current_v.listed is '상장 여부';
comment on column instrument_master_current_v.name is '종목명';
comment on column instrument_master_current_v.market is '시장';
comment on column instrument_master_current_v.sec_group is '증권그룹 구분';
comment on column instrument_master_current_v.isin is '표준코드 (ISIN)';
comment on column instrument_master_current_v.lot is '매매수량단위';
comment on column instrument_master_current_v.par_value is '액면가 (원)';
comment on column instrument_master_current_v.listed_on is '상장일';
comment on column instrument_master_current_v.listed_shares_k is '상장주수 (천 주)';
comment on column instrument_master_current_v.preferred is '우선주 구분';
comment on column instrument_master_current_v.spac is '스팩 여부';
comment on column instrument_master_current_v.halted is '거래정지 여부';
comment on column instrument_master_current_v.liquidation is '정리매매 여부';
comment on column instrument_master_current_v.managed is '관리종목 여부';
comment on column instrument_master_current_v.warning is '시장경고 구분';
comment on column instrument_master_current_v.valid_from is '적용 시작일';
comment on column instrument_master_current_v.load_id is '적재 번호';

comment on view instrument_master_match_v is '등록 종목과 종목 마스터 대조 (원장이 쓰는 종목마다 한 행)';
comment on column instrument_master_match_v.symbol is '종목코드';
comment on column instrument_master_match_v.name is '등록 종목명';
comment on column instrument_master_match_v.market is '등록 시장';
comment on column instrument_master_match_v.asset_type is '등록 자산 유형';
comment on column instrument_master_match_v.master_name is '마스터 종목명';
comment on column instrument_master_match_v.master_market is '마스터 시장';
comment on column instrument_master_match_v.master_group is '마스터 증권그룹 구분';
comment on column instrument_master_match_v.listed is '마스터 상장 여부';
comment on column instrument_master_match_v.halted is '거래정지 여부';
comment on column instrument_master_match_v.liquidation is '정리매매 여부';
comment on column instrument_master_match_v.managed is '관리종목 여부';
comment on column instrument_master_match_v.warning is '시장경고 구분';
comment on column instrument_master_match_v.valid_from is '마스터 적용 시작일';
comment on column instrument_master_match_v.status is '대조 결과 (MATCH 일치 / RENAMED 이름만 다름 / DIFFERENT 시장·구분 다름 / DELISTED 상장폐지 / MISSING 마스터에 없음)';

comment on table app_users is '사용자 (주문을 쓰고 승인하는 사람, 추가만 가능)';
comment on column app_users.user_id is '사용자 ID (지금은 PC 로그인 이름)';
comment on column app_users.name is '이름';
comment on column app_users.created_by is '등록한 사람';
comment on column app_users.created_at is '등록 시각';

comment on table user_rights is '결재 권한 이력 (부여·회수마다 한 행, 수정·삭제 불가)';
comment on column user_rights.right_row_id is '권한 행 번호';
comment on column user_rights.user_id is '사용자 ID';
comment on column user_rights.account_id is '계좌 ID (비어 있으면 모든 계좌)';
comment on column user_rights.right_code is '권한 (ORDER_WRITE 주문 작성 / ORDER_APPROVE 주문 승인 / RIGHTS_ADMIN 권한 관리)';
comment on column user_rights.granted is '부여 여부 (거짓이면 회수 행)';
comment on column user_rights.max_order_value is '1건 승인 한도 (원, 비어 있으면 한도 없음)';
comment on column user_rights.self_approve is '셀프 승인 허용 (자기가 쓴 요청을 승인할 수 있음)';
comment on column user_rights.effective_from is '적용 시작일';
comment on column user_rights.granted_by is '부여·회수한 사람';
comment on column user_rights.reason is '사유';
comment on column user_rights.created_at is '기록 시각';

comment on table order_requests is '주문 요청 (결재 한 건: 단건은 종목 한 줄, 바스켓은 여러 줄, 수정·삭제 불가)';
comment on column order_requests.trade_date is '주문일 (요청 키, 이날 장중에만 나가고 장 마감에 만료)';
comment on column order_requests.request_no is '요청 번호 (주문일마다 1부터, 계좌와 무관)';
comment on column order_requests.account_id is '계좌 ID';
comment on column order_requests.order_type is '주문 유형 (SINGLE 단건 / BASKET 바스켓)';
comment on column order_requests.created_by is '작성자';
comment on column order_requests.created_at is '작성 시각';

comment on table order_request_lines is '주문 요청 줄 (요청의 종목 줄, 결재 요청 뒤에는 더하지 못함, 수정·삭제 불가)';
comment on column order_request_lines.trade_date is '주문일';
comment on column order_request_lines.request_no is '요청 번호';
comment on column order_request_lines.line_no is '줄 번호 (보내는 순서)';
comment on column order_request_lines.symbol is '종목코드';
comment on column order_request_lines.side is '매매 구분';
comment on column order_request_lines.qty is '수량';
comment on column order_request_lines.limit_price is '지정가 (비어 있으면 시장가)';

comment on table order_approvals is '결재 이력 (요청 단위: 요청·승인·반려·취소·만료마다 한 행, 수정·삭제 불가)';
comment on column order_approvals.id is '이력 번호';
comment on column order_approvals.trade_date is '주문일';
comment on column order_approvals.request_no is '요청 번호';
comment on column order_approvals.action is '결재 행위 (REQUEST 요청 / APPROVE 승인 / REJECT 반려 / CANCEL 취소 / EXPIRE 만료)';
comment on column order_approvals.actor is '처리한 사람 (만료는 비어 있음: run이 처리)';
comment on column order_approvals.self_approved is '셀프 승인 (작성자가 승인, DB가 채움)';
comment on column order_approvals.reason is '사유 (반려는 필수)';
comment on column order_approvals.set_check is '그때의 묶음 검증 결과 (R-98)';
comment on column order_approvals.at is '처리 시각';

comment on table order_sends is '전송 이력 (줄 단위: 승인된 요청의 줄마다 한 번, 수정·삭제 불가)';
comment on column order_sends.id is '전송 이력 번호';
comment on column order_sends.trade_date is '주문일';
comment on column order_sends.request_no is '요청 번호';
comment on column order_sends.line_no is '줄 번호';
comment on column order_sends.outcome is '전송 결과 (SENT 전송 / FAILED 전송 실패)';
comment on column order_sends.client_id is '주문 ID (전송한 주문)';
comment on column order_sends.reason is '사유 (전송 실패는 필수)';
comment on column order_sends.set_check is '보내기 직전 묶음 검증 결과 (R-98)';
comment on column order_sends.at is '처리 시각';

comment on view user_rights_v is '현재 결재 권한 (사용자·계좌·권한마다 오늘 적용되는 최근 행, 부여된 것만)';
comment on column user_rights_v.user_id is '사용자 ID';
comment on column user_rights_v.user_name is '이름';
comment on column user_rights_v.account_id is '계좌 ID (비어 있으면 모든 계좌)';
comment on column user_rights_v.right_code is '권한';
comment on column user_rights_v.max_order_value is '1건 승인 한도 (원)';
comment on column user_rights_v.self_approve is '셀프 승인 허용';
comment on column user_rights_v.effective_from is '적용 시작일';
comment on column user_rights_v.granted_by is '부여한 사람';
comment on column user_rights_v.reason is '사유';
comment on column user_rights_v.right_row_id is '권한 행 번호';

comment on view order_requests_v is '주문 요청 현황 (요청마다 한 행, 상태는 결재 이력의 마지막 행과 줄별 전송 결과)';
comment on column order_requests_v.trade_date is '주문일';
comment on column order_requests_v.request_no is '요청 번호';
comment on column order_requests_v.account_id is '계좌 ID';
comment on column order_requests_v.order_type is '주문 유형';
comment on column order_requests_v.created_by is '작성자';
comment on column order_requests_v.created_at is '작성 시각';
comment on column order_requests_v.line_count is '줄 수';
comment on column order_requests_v.amount is '주문금액 합계 (지정가 줄만, 시장가 줄은 빠짐)';
comment on column order_requests_v.status is '상태 (PENDING 결재 대기 / APPROVED 승인·전송 대기 / REJECTED 반려 / CANCELED 취소 / SENT 전송 / SEND_FAILED 전송 실패 / SEND_UNKNOWN 결과 미확인 / EXPIRED 만료)';
comment on column order_requests_v.approved_by is '승인자';
comment on column order_requests_v.self_approved is '셀프 승인';
comment on column order_requests_v.approved_at is '승인 시각';
comment on column order_requests_v.sent_lines is '전송한 줄 수';
comment on column order_requests_v.failed_lines is '전송 실패한 줄 수';
comment on column order_requests_v.last_reason is '마지막 사유';
comment on column order_requests_v.status_at is '상태 시각';
comment on column order_requests_v.accepted_lines is '증권사가 접수한 줄 수';
comment on column order_requests_v.rejected_lines is '증권사가 거부한 줄 수';
comment on column order_requests_v.unknown_lines is '보냈지만 접수 결과를 모르는 줄 수 (R-103)';

comment on view order_request_lines_v is '주문 요청 줄 현황 (줄마다 한 행, 전송 결과와 주문 ID)';
comment on column order_request_lines_v.trade_date is '주문일';
comment on column order_request_lines_v.request_no is '요청 번호';
comment on column order_request_lines_v.line_no is '줄 번호';
comment on column order_request_lines_v.account_id is '계좌 ID';
comment on column order_request_lines_v.order_type is '주문 유형';
comment on column order_request_lines_v.symbol is '종목코드';
comment on column order_request_lines_v.side is '매매 구분';
comment on column order_request_lines_v.qty is '수량';
comment on column order_request_lines_v.limit_price is '지정가';
comment on column order_request_lines_v.amount is '주문금액 (지정가 줄만)';
comment on column order_request_lines_v.send_outcome is '전송 결과';
comment on column order_request_lines_v.client_id is '주문 ID';
comment on column order_request_lines_v.send_reason is '전송 실패 사유';
comment on column order_request_lines_v.sent_at is '전송 시각';
comment on column order_request_lines_v.ack_outcome is '증권사 접수 결과 (ACCEPTED 접수 / REJECTED 거부, 비어 있으면 아직 모름)';
comment on column order_request_lines_v.order_no is '증권사 주문번호';
comment on column order_request_lines_v.ack_detail is '접수 결과 내용 (거부 사유, 조회로 찾음 등)';
comment on column order_request_lines_v.awaiting_ack is '보냈지만 접수 결과를 모름 (R-103)';

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

comment on table erp_exports is 'ERP 전송 이력 (erp export가 만든 파일마다 한 행, 수정·삭제 불가)';
comment on column erp_exports.export_id is '내보내기 번호';
comment on column erp_exports.account_id is '계좌 ID (비어 있으면 모든 계좌)';
comment on column erp_exports.date_from is '시작일';
comment on column erp_exports.date_to is '종료일';
comment on column erp_exports.unit is '전표 단위 (ENTRY 건별 / DAY 일별 합계)';
comment on column erp_exports.company_code is '회사코드 (계좌 법인의 ERP 회사코드, 없으면 매핑 파일의 값, 법인이 여럿이면 쉼표로)';
comment on column erp_exports.document_type is '전표유형 (매핑 파일, 없으면 빈 값)';
comment on column erp_exports.mapping_sha256 is '매핑 파일 해시 (SHA-256, 비어 있으면 MiniSTP 계정 그대로)';
comment on column erp_exports.documents is '전표 수';
comment on column erp_exports.lines is '줄 수';
comment on column erp_exports.debit_total is '차변 합계';
comment on column erp_exports.credit_total is '대변 합계';
comment on column erp_exports.file_name is '파일 이름';
comment on column erp_exports.file_sha256 is '파일 해시 (SHA-256, 통제 파일에도 적음)';
comment on column erp_exports.created_by is '내보낸 사람 (PC 로그인 이름)';
comment on column erp_exports.created_at is '내보낸 시각';

comment on table erp_export_docs is 'ERP 전송 전표 (내보내기에 든 증빙번호마다 한 행, 수정·삭제 불가)';
comment on column erp_export_docs.export_id is '내보내기 번호';
comment on column erp_export_docs.doc_no is '증빙번호';
comment on column erp_export_docs.account_id is '계좌 ID';
comment on column erp_export_docs.doc_date is '전표일자';

comment on table erp_export_voids is 'ERP 전송 무효 (ERP가 받지 않은 내보내기, 그 전표는 다시 낼 수 있음, 수정·삭제 불가)';
comment on column erp_export_voids.export_id is '내보내기 번호';
comment on column erp_export_voids.reason is '사유';
comment on column erp_export_voids.voided_by is '처리자 (PC 로그인 이름)';
comment on column erp_export_voids.voided_at is '처리 시각';

comment on view erp_exports_v is 'ERP 전송 현황 (내보내기마다 한 행, 무효 여부 포함)';
comment on column erp_exports_v.export_id is '내보내기 번호';
comment on column erp_exports_v.account_id is '계좌 ID (비어 있으면 모든 계좌)';
comment on column erp_exports_v.date_from is '시작일';
comment on column erp_exports_v.date_to is '종료일';
comment on column erp_exports_v.unit is '전표 단위 (ENTRY 건별 / DAY 일별 합계)';
comment on column erp_exports_v.company_code is '회사코드';
comment on column erp_exports_v.document_type is '전표유형';
comment on column erp_exports_v.mapping_sha256 is '매핑 파일 해시';
comment on column erp_exports_v.documents is '전표 수';
comment on column erp_exports_v.lines is '줄 수';
comment on column erp_exports_v.debit_total is '차변 합계';
comment on column erp_exports_v.credit_total is '대변 합계';
comment on column erp_exports_v.file_name is '파일 이름';
comment on column erp_exports_v.file_sha256 is '파일 해시';
comment on column erp_exports_v.created_by is '내보낸 사람';
comment on column erp_exports_v.created_at is '내보낸 시각';
comment on column erp_exports_v.status is '상태 (EXPORTED 내보냄 / VOIDED 무효)';
comment on column erp_exports_v.void_reason is '무효 사유';
comment on column erp_exports_v.voided_by is '무효 처리자';
comment on column erp_exports_v.voided_at is '무효 처리 시각';

comment on table close_steps is '마감 점검 단계 기준 (단계마다 한 행, 이름·순서·사용·필수·화면·자산 유형을 고침, 삭제 불가)';
comment on column close_steps.step_code is '단계 코드 (프로그램의 점검 로직과 짝)';
comment on column close_steps.name is '단계 이름';
comment on column close_steps.sort_order is '순서';
comment on column close_steps.in_use is '사용 여부 (끄면 점검에서 빠짐)';
comment on column close_steps.required is '필수 여부 (선택 단계는 상태만 보이고 그날의 점검 완료를 막지 않음)';
comment on column close_steps.screen is '상세 화면';
comment on column close_steps.asset_type is '적용 자산 유형 (비어 있으면 모든 계좌, 있으면 그 자산을 거래·보유한 계좌에만)';
comment on column close_steps.description is '설명';
comment on column close_steps.updated_at is '마지막 변경 시각';

comment on table close_checks is '마감 점검 결과 (점검할 때마다 계좌·영업일마다 한 행, 수정·삭제 불가)';
comment on column close_checks.check_id is '점검 번호';
comment on column close_checks.account_id is '계좌 ID';
comment on column close_checks.check_date is '영업일';
comment on column close_checks.day_state is '그날 상태 (DONE 점검 완료 / OPEN 진행 중 / ATTENTION 확인 필요)';
comment on column close_checks.done is '끝난 필수 단계 수';
comment on column close_checks.steps is '필수 단계 수';
comment on column close_checks.command is '점검한 명령 (daily, close, close-check)';
comment on column close_checks.checked_by is '점검한 사람 (PC 로그인 이름)';
comment on column close_checks.checked_at is '점검 시각';

comment on table close_check_steps is '마감 점검 단계 결과 (점검 한 번의 단계마다 한 행, 수정·삭제 불가)';
comment on column close_check_steps.check_id is '점검 번호';
comment on column close_check_steps.step_no is '순서';
comment on column close_check_steps.step_code is '단계 코드';
comment on column close_check_steps.name is '단계 이름 (점검 때의 이름)';
comment on column close_check_steps.required is '필수 여부 (점검 때의 기준)';
comment on column close_check_steps.state is '상태 (DONE 완료 / NONE 대상 없음 / WAIT 대기 / WARN 주의 / BAD 확인 필요 / MISSING 누락)';
comment on column close_check_steps.detail is '내용 (그렇게 본 까닭)';

comment on view close_checks_last_v is '계좌·영업일마다 마지막 마감 점검 결과';
comment on column close_checks_last_v.account_id is '계좌 ID';
comment on column close_checks_last_v.check_date is '영업일';
comment on column close_checks_last_v.check_id is '점검 번호';
comment on column close_checks_last_v.day_state is '그날 상태 (DONE 점검 완료 / OPEN 진행 중 / ATTENTION 확인 필요)';
comment on column close_checks_last_v.done is '끝난 필수 단계 수';
comment on column close_checks_last_v.steps is '필수 단계 수';
comment on column close_checks_last_v.command is '점검한 명령';
comment on column close_checks_last_v.checked_by is '점검한 사람';
comment on column close_checks_last_v.checked_at is '점검 시각';
