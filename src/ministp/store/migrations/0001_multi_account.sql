-- title: 다계좌: 고객·위탁계좌, 원장에 계좌ID
-- erd: 2.0
--
-- ERD v1.1 -> v2.0 (스펙 D7). 기존 행은 하나도 바뀌지 않는다: 새 컬럼은 기본값 'A0001'로
-- 추가한 뒤 기본값을 없애고, 기존 데이터는 모두 기본 계좌 A0001의 것이 된다.
-- 계좌번호와 인증키는 DB에 넣지 않는다 (credential_ref는 PC 환경변수 이름의 머리말).

-- 1. 고객, 위탁계좌, 계좌 상태 이력 -------------------------------------------------------
create table customers (
    customer_id text primary key check (customer_id ~ '^C[0-9]{4}$'),
    alias       text not null,
    created_at  timestamptz not null default now()
);

create table trading_accounts (
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

create table account_status_history (
    id             bigserial primary key,
    account_id     text not null references trading_accounts (account_id),
    status         text not null check (status in ('OPEN', 'SUSPENDED', 'CLOSED')),
    effective_from date not null,
    reason         text,
    created_at     timestamptz not null default now()
);

-- 기존 데이터의 주인: 고객 C0001의 계좌 A0001. 개설일은 원장의 첫 이벤트 날짜.
insert into customers (customer_id, alias) values ('C0001', '기본 고객');
insert into trading_accounts (account_id, customer_id, alias, broker, env, account_type, credential_ref, opened_on)
select 'A0001', 'C0001', '기본 계좌', 'KIS', 'PAPER', 'GENERAL', 'KIS',
       coalesce((select coalesce(payload->>'date', payload->>'trade_date')::date from ledger_events where seq = 1),
                current_date);
insert into account_status_history (account_id, status, effective_from, reason)
select account_id, 'OPEN', opened_on, '다계좌 전환 시 기본 계좌' from trading_accounts where account_id = 'A0001';

-- 2. 원장·주문·대사 7개 테이블에 계좌ID ---------------------------------------------------
alter table ledger_events add column account_id text not null default 'A0001' references trading_accounts (account_id);
alter table ledger_events alter column account_id drop default;
alter table ledger_events add constraint ledger_events_seq_account_key unique (seq, account_id);
create index ledger_events_account_idx on ledger_events (account_id, seq);

-- 파생 행은 자기 이벤트와 같은 계좌여야 한다: (이벤트 순번, 계좌ID)를 함께 참조한다.
alter table journal_entries add column account_id text not null default 'A0001';
alter table journal_entries alter column account_id drop default;
alter table journal_entries drop constraint journal_entries_event_seq_fkey;
alter table journal_entries add constraint journal_entries_event_account_fkey
    foreign key (event_seq, account_id) references ledger_events (seq, account_id);

alter table position_ledger add column account_id text not null default 'A0001';
alter table position_ledger alter column account_id drop default;
alter table position_ledger drop constraint position_ledger_event_seq_fkey;
alter table position_ledger add constraint position_ledger_event_account_fkey
    foreign key (event_seq, account_id) references ledger_events (seq, account_id);
-- 체결 ID는 계좌 안에서만 유일하다 (증권사 주문번호는 계좌마다 따로 매겨진다).
alter table position_ledger drop constraint position_ledger_exec_id_key;
alter table position_ledger add constraint position_ledger_account_exec_key unique (account_id, exec_id);

alter table cash_ledger add column account_id text not null default 'A0001';
alter table cash_ledger alter column account_id drop default;
alter table cash_ledger drop constraint cash_ledger_event_seq_fkey;
alter table cash_ledger add constraint cash_ledger_event_account_fkey
    foreign key (event_seq, account_id) references ledger_events (seq, account_id);

alter table oms_orders add column account_id text not null default 'A0001' references trading_accounts (account_id);
alter table oms_orders alter column account_id drop default;

alter table broker_snapshots add column account_id text not null default 'A0001' references trading_accounts (account_id);
alter table broker_snapshots alter column account_id drop default;
alter table broker_snapshots add constraint broker_snapshots_id_account_key unique (snapshot_id, account_id);

-- 대사 결과는 자기 대사 실행(스냅샷)과 같은 계좌여야 한다.
alter table recon_results add column account_id text not null default 'A0001';
alter table recon_results alter column account_id drop default;
alter table recon_results drop constraint recon_results_run_id_fkey;
alter table recon_results add constraint recon_results_run_account_fkey
    foreign key (run_id, account_id) references broker_snapshots (snapshot_id, account_id);

-- 3. 새 테이블도 고칠 수 없는 기록이다 (R-04) ---------------------------------------------
do $$
declare t text;
begin
    foreach t in array array['customers', 'trading_accounts', 'account_status_history'] loop
        execute format('create trigger %I before update or delete on %I
                        for each row execute function ministp_forbid_change()', t || '_append_only', t);
        execute format('create trigger %I before truncate on %I
                        for each statement execute function ministp_forbid_change()', t || '_no_truncate', t);
    end loop;
end $$;

-- 4. 뷰: 계좌별로 ------------------------------------------------------------------------
drop view account_balances_v;
drop view positions_v;
drop view cash_balance_v;

create view account_balances_v as
select t.account_id, a.code, a.name, a.normal_side,
       case when a.normal_side = 'D'
            then coalesce(sum(l.debit - l.credit), 0)
            else coalesce(sum(l.credit - l.debit), 0) end as balance
  from trading_accounts t
 cross join accounts_chart a
  left join journal_entries e on e.account_id = t.account_id
  left join journal_lines l on l.entry_id = e.entry_id and l.account_code = a.code
 group by t.account_id, a.code, a.name, a.normal_side;

create view positions_v as
select account_id, symbol, sum(qty_delta) as qty, sum(cost_delta) as book_cost
  from position_ledger group by account_id, symbol having sum(qty_delta) <> 0;

create view cash_balance_v as
select t.account_id, coalesce(sum(c.amount), 0) as balance
  from trading_accounts t left join cash_ledger c on c.account_id = t.account_id
 group by t.account_id;

create view account_status_v as
select t.account_id, t.customer_id, t.alias, s.status, s.effective_from
  from trading_accounts t
  join lateral (select h.status, h.effective_from from account_status_history h
                 where h.account_id = t.account_id order by h.effective_from desc, h.id desc limit 1) s on true;

create view customer_positions_v as
select t.customer_id, p.symbol, sum(p.qty_delta) as qty, sum(p.cost_delta) as book_cost
  from position_ledger p join trading_accounts t on t.account_id = p.account_id
 group by t.customer_id, p.symbol having sum(p.qty_delta) <> 0;

-- 5. 한글 명칭 ---------------------------------------------------------------------------
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
