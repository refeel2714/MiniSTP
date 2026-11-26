-- title: 권리처리: 현금배당 권리 원장과 계좌별 배당 현황
-- erd: 2.4
-- changes-data: code_groups, common_codes
--
-- ERD v2.3 -> v2.4 (R-57~59). 새 테이블 하나, 뷰 두 개, 코드 그룹 두 개와 코드를 더한다.
-- 기존 테이블의 기존 행은 건드리지 않는다. code_groups와 common_codes는 새 행이 늘어나서 위에 적었다.
-- 배당 인식·입금·취소는 원장 이벤트로 남고, 분개와 예수금 행은 기존 테이블에 들어간다.

-- 1. 권리 원장 --------------------------------------------------------------------------
create table corporate_actions (
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
create index corporate_actions_symbol_idx on corporate_actions (symbol, record_date);

create trigger corporate_actions_append_only before update or delete on corporate_actions
    for each row execute function ministp_forbid_change();
create trigger corporate_actions_no_truncate before truncate on corporate_actions
    for each statement execute function ministp_forbid_change();

-- 2. 뷰 --------------------------------------------------------------------------------
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

-- 3. 한글 명칭 ---------------------------------------------------------------------------
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

-- 4. 새 코드 그룹과 코드 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) -----------
insert into code_groups (group_id, name, description, used_by, fixed) values
    ('CORPORATE_ACTION_TYPE', '권리 유형', '종목에 생긴 권리의 종류.', 'corporate_actions.action_type', true),
    ('DIVIDEND_STATUS', '배당 상태', '계좌에서 배당이 어디까지 진행됐는지.', 'dividends_v.status', true);
insert into common_codes (group_id, code, name, description, sort_order) values
    ('EVENT_TYPE', 'DividendAccrualEvent', '배당 인식', '받을 배당을 미수배당금으로 인식', 9),
    ('EVENT_TYPE', 'DividendPaymentEvent', '배당 입금', '배당금이 세금을 떼고 예수금으로 들어옴', 10),
    ('EVENT_TYPE', 'DividendCancelEvent', '배당 인식 취소', '취소된 권리의 인식을 되돌림', 11),
    ('JOURNAL_KIND', 'DIVIDEND_ACCRUAL', '배당 인식', '미수배당금과 배당수익으로 인식', 8),
    ('JOURNAL_KIND', 'DIVIDEND', '배당 입금', '지급일에 예수금과 원천징수세액으로 정리', 9),
    ('JOURNAL_KIND', 'DIVIDEND_CANCEL', '배당 인식 취소', '취소된 권리의 인식을 되돌림', 10),
    ('CASH_REASON', 'DIVIDEND', '배당 입금', '세금을 뗀 배당금 수령', 3),
    ('AUDIT_ACTION', 'CORPORATE_ACTION_ADD', '권리 등록', '현금배당 일정과 주당 배당금 등록', 16),
    ('AUDIT_ACTION', 'CORPORATE_ACTION_CANCEL', '권리 취소', '잘못 등록했거나 취소된 권리를 무효로', 17),
    ('AUDIT_ACTION', 'DIVIDEND_PAY', '배당 입금 기록', '증권사가 실제로 준 금액으로 직접 기록', 18),
    ('CORPORATE_ACTION_TYPE', 'CASH_DIVIDEND', '현금배당', '주식 배당금과 ETF 분배금', 1),
    ('CORPORATE_ACTION_TYPE', 'CANCEL', '취소', '앞서 등록한 권리를 무효로 하는 행', 2),
    ('DIVIDEND_STATUS', 'ACCRUED', '인식', '미수배당금으로 잡혀 있고 아직 입금 전', 1),
    ('DIVIDEND_STATUS', 'PAID', '입금', '배당금이 예수금으로 들어옴', 2),
    ('DIVIDEND_STATUS', 'CANCELED', '취소', '권리가 취소돼 인식을 되돌림', 3);
