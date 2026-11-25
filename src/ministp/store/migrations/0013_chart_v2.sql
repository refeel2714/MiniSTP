-- title: 계정과목표 v2: 법인 장부 기준 이름, 선납세금·분배금수익, 원천징수 비용 계정 사용 중지
-- erd: 2.13
-- changes-data: accounts_chart, common_codes
--
-- ERD v2.12 -> v2.13 (스펙 D4 v2, docs/spec/05-chart-of-accounts-v2.md). 계정과목에 사용 여부 열을 더하고, 이름 다섯 개를
-- 법인 장부 기준으로 바꾸고(코드는 그대로라 지난 전표의 뜻이 바뀌지 않음), 계정 두 개(1040 선납세금, 4050 분배금수익)를
-- 더하고, 5050 법인세등(원천징수)을 사용 중지한다. 사용 중지한 계정으로는 새 분개 줄이 들어가지 않는다(트리거).
-- 바뀌는 기존 행은 accounts_chart의 이름과 공통코드 설명 일곱 개(옛 계정 이름을 적은 것)뿐이라 위에 적었다. 전표는 건드리지 않는다.

alter table accounts_chart add column in_use boolean not null default true;

update accounts_chart set name = '증권예치금' where code = '1010';
update accounts_chart set name = 'FVPL 금융자산' where code = '1100';
update accounts_chart set name = 'FVPL 금융자산 평가조정' where code = '1110';
update accounts_chart set name = '본사 자금 대체' where code = '3010';
update accounts_chart set name = '배당금수익' where code = '4030';
update accounts_chart set in_use = false where code = '5050';
insert into accounts_chart (code, name, normal_side) values ('1040', '선납세금', 'D'), ('4050', '분배금수익', 'C');

create or replace function ministp_account_in_use() returns trigger language plpgsql as $$
begin
    if not (select in_use from accounts_chart where code = new.account_code) then
        raise exception 'account % is retired: no new journal line may use it', new.account_code
            using errcode = 'check_violation';
    end if;
    return new;
end $$;

create trigger journal_lines_account_in_use before insert on journal_lines
    for each row execute function ministp_account_in_use();

update common_codes set description = '본사 자금 입금' where group_id = 'EVENT_TYPE' and code = 'DepositEvent';
update common_codes set description = '본사 자금 출금' where group_id = 'EVENT_TYPE' and code = 'WithdrawalEvent';
update common_codes set description = '미수배당금과 배당금수익(ETF는 분배금수익)으로 인식' where group_id = 'JOURNAL_KIND' and code = 'DIVIDEND_ACCRUAL';
update common_codes set description = '지급일에 증권예치금과 선납세금(원천징수)으로 정리' where group_id = 'JOURNAL_KIND' and code = 'DIVIDEND';
update common_codes set description = '증권사가 예수금에 준 이자. 상대 계정은 이자수익, 세금은 선납세금' where group_id = 'ADJUSTMENT_REASON' and code = 'INTEREST';
update common_codes set description = '입금으로 기록한 배당금과 실제 금액의 차이. 상대 계정은 배당금수익' where group_id = 'ADJUSTMENT_REASON' and code = 'DIVIDEND';
update common_codes set description = '배당에서 실제로 뗀 세금과 기록한 세금의 차이. 상대 계정은 선납세금' where group_id = 'ADJUSTMENT_REASON' and code = 'WITHHOLDING';

comment on column accounts_chart.in_use is '사용 여부 (사용 중지한 계정은 지난 전표만 가리키고 새 분개에 쓰지 않음)';
