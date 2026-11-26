-- title: 수동 조정: 조정 이벤트와 사유 코드, 계정과목 세 개
-- erd: 2.6
-- changes-data: accounts_chart, code_groups, common_codes
--
-- ERD v2.5 -> v2.6 (R-66). 뷰 하나, 인덱스 하나, 계정과목 세 개와 계정코드 형식 제약, 코드 그룹 하나와 코드를 더한다.
-- 기존 테이블의 기존 행은 건드리지 않는다. 위에 적은 세 테이블은 새 행이 늘어난다.
-- 조정과 그 취소는 원장 이벤트로 남고, 분개와 예수금 행은 기존 테이블에 들어간다.

-- 1. 계정과목: 채번 규칙(스펙 D4)을 제약으로, 그리고 조정이 쓰는 계정 세 개 -------------------
--    계정코드는 첫째 자리(대분류)가 1~5인 숫자 4자리다. 지금 있는 15개 계정은 모두 이 형식이다.
alter table accounts_chart add constraint accounts_chart_code_check check (code ~ '^[1-5][0-9]{3}$');

insert into accounts_chart (code, name, normal_side) values
    ('4040', '이자수익', 'C'),
    ('4090', '잡이익', 'C'),
    ('5090', '잡손실', 'D');

-- 2. 조정 번호 하나에 조정 이벤트 하나, 취소 이벤트 하나 ------------------------------------
create unique index ledger_events_adjustment_key on ledger_events ((payload ->> 'adjustment_id'), event_type)
    where event_type in ('AdjustmentEvent', 'AdjustmentCancelEvent');

-- 3. 뷰 --------------------------------------------------------------------------------
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

-- 4. 한글 명칭 ---------------------------------------------------------------------------
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

-- 5. 새 코드 그룹과 코드 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) -----------
insert into code_groups (group_id, name, description, used_by, fixed) values
    ('ADJUSTMENT_REASON', '수동 조정 사유', '조정하는 이유. 사유가 상대 계정을 정해요 (R-66).', 'adjustments_v.reason', true);
insert into common_codes (group_id, code, name, description, sort_order) values
    ('EVENT_TYPE', 'AdjustmentEvent', '수동 조정', '원장이 계산할 수 없는 예수금 변동을 사유와 함께 기록', 15),
    ('EVENT_TYPE', 'AdjustmentCancelEvent', '수동 조정 취소', '조정을 반대 전표로 되돌림', 16),
    ('JOURNAL_KIND', 'ADJUSTMENT', '수동 조정', '사유 코드가 정한 계정으로 기록', 14),
    ('JOURNAL_KIND', 'ADJUSTMENT_CANCEL', '수동 조정 취소', '조정 전표의 반대 전표', 15),
    ('CASH_REASON', 'ADJUSTMENT', '수동 조정', '예탁금 이용료, 수수료·세금·배당 차이', 7),
    ('CASH_REASON', 'ADJUSTMENT_CANCEL', '수동 조정 취소', '조정을 되돌림', 8),
    ('AUDIT_ACTION', 'ADJUSTMENT_ADD', '수동 조정', '예탁금 이용료나 증권사와의 차이를 사유와 함께 기록', 22),
    ('AUDIT_ACTION', 'ADJUSTMENT_CANCEL', '수동 조정 취소', '조정을 반대 전표로 되돌림', 23),
    ('ADJUSTMENT_REASON', 'INTEREST', '예탁금 이용료', '증권사가 예수금에 준 이자. 상대 계정은 이자수익, 세금은 법인세등', 1),
    ('ADJUSTMENT_REASON', 'FEE', '수수료 차이', '증권사가 실제로 뗀 수수료와 원장 계산의 차이. 상대 계정은 지급수수료', 2),
    ('ADJUSTMENT_REASON', 'TAX', '거래세 차이', '증권사가 실제로 뗀 거래세와 원장 계산의 차이. 상대 계정은 세금과공과', 3),
    ('ADJUSTMENT_REASON', 'DIVIDEND', '배당금 차이', '입금으로 기록한 배당금과 실제 금액의 차이. 상대 계정은 배당수익', 4),
    ('ADJUSTMENT_REASON', 'WITHHOLDING', '원천징수 차이', '배당에서 실제로 뗀 세금과 기록한 세금의 차이. 상대 계정은 법인세등', 5),
    ('ADJUSTMENT_REASON', 'OTHER', '기타', '위에 없는 사유. 상대 계정은 잡이익 또는 잡손실', 6);
