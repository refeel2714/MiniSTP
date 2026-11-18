-- MiniSTP ledger schema (spec 03). Append-only tables are enforced in the DB,
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
