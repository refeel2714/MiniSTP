-- title: 입출금·대체: 출금과 계좌 간 대체 이벤트, 입출금 뷰
-- erd: 2.5
-- changes-data: common_codes
--
-- ERD v2.4 -> v2.5 (R-56, R-61, R-64, R-65). 뷰 두 개, 인덱스 하나, 공통코드 행을 더한다.
-- 기존 테이블의 기존 행은 건드리지 않는다. common_codes는 새 행이 늘어나서 위에 적었다.
-- 출금과 대체는 원장 이벤트로 남고, 분개와 예수금 행은 기존 테이블에 들어간다.

-- 1. 대체 번호 하나에 보낸 쪽 이벤트 하나, 받은 쪽 이벤트 하나 ------------------------------
create unique index ledger_events_transfer_key on ledger_events ((payload ->> 'transfer_id'), event_type)
    where event_type in ('TransferOutEvent', 'TransferInEvent');

-- 2. 뷰 --------------------------------------------------------------------------------
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

-- 3. 한글 명칭 ---------------------------------------------------------------------------
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

-- 4. 새 코드 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) ----------------------
insert into common_codes (group_id, code, name, description, sort_order) values
    ('EVENT_TYPE', 'WithdrawalEvent', '출금', '투자원금 출금', 12),
    ('EVENT_TYPE', 'TransferOutEvent', '대체 출금', '같은 고객의 다른 계좌로 보냄', 13),
    ('EVENT_TYPE', 'TransferInEvent', '대체 입금', '같은 고객의 다른 계좌에서 받음', 14),
    ('JOURNAL_KIND', 'WITHDRAWAL', '출금', null, 11),
    ('JOURNAL_KIND', 'TRANSFER_OUT', '대체 출금', '다른 계좌로 보낸 돈', 12),
    ('JOURNAL_KIND', 'TRANSFER_IN', '대체 입금', '다른 계좌에서 받은 돈', 13),
    ('CASH_REASON', 'WITHDRAWAL', '출금', null, 4),
    ('CASH_REASON', 'TRANSFER_OUT', '대체 출금', '다른 계좌로 보낸 돈', 5),
    ('CASH_REASON', 'TRANSFER_IN', '대체 입금', '다른 계좌에서 받은 돈', 6),
    ('AUDIT_ACTION', 'CASH_DEPOSIT', '입금 기록', '증권사 계좌에 들어온 돈', 19),
    ('AUDIT_ACTION', 'CASH_WITHDRAW', '출금 기록', '증권사 계좌에서 나간 돈', 20),
    ('AUDIT_ACTION', 'CASH_TRANSFER', '계좌 간 대체', '같은 고객의 두 계좌 사이로 옮긴 돈', 21);
