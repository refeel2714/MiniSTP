-- title: 마감 점검 단계 기준과 점검 결과 이력
-- erd: 2.12
-- changes-data: code_groups, common_codes
--
-- ERD v2.11 -> v2.12 (스펙 R-105). 새 테이블 셋, 단계 기준 보호 트리거 하나, 뷰 하나, 코드 그룹 둘과 코드를 더한다.
-- 기존 테이블의 기존 행은 건드리지 않는다. code_groups와 common_codes는 새 행이 늘어나서 위에 적었다.
-- 마감 점검(STP-2400)의 단계를 프로그램 상수에서 DB 기준 `close_steps`로 옮긴다. 단계마다 무엇을 보는지(점검 로직)는
-- 프로그램에 있고, 이름·순서·사용 여부·필수 여부·상세 화면·적용 자산 유형은 이 표에서 고친다(행은 지우지 않음).
-- 점검을 할 때마다(`daily`, `close`, `close-check`) 그날 단계별 결과를 `close_checks`·`close_check_steps`에 남긴다.

-- 1. 테이블 ---------------------------------------------------------------------------------
create table close_steps (
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

create table close_checks (
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
create index close_checks_day_idx on close_checks (account_id, check_date, check_id);

create table close_check_steps (
    check_id  bigint not null references close_checks (check_id),
    step_no   integer not null check (step_no > 0),
    step_code text not null references close_steps (step_code),
    name      text not null,
    required  boolean not null,
    state     text not null check (state in ('DONE', 'NONE', 'WAIT', 'WARN', 'BAD', 'MISSING')),
    detail    text not null,
    primary key (check_id, step_no)
);

do $$
declare t text;
begin
    foreach t in array array['close_checks', 'close_check_steps'] loop
        execute format('create trigger %I before update or delete on %I
                        for each row execute function ministp_forbid_change()', t || '_append_only', t);
        execute format('create trigger %I before truncate on %I
                        for each statement execute function ministp_forbid_change()', t || '_no_truncate', t);
    end loop;
end $$;

-- 2. 단계 기준은 고치되 지우지 않는다: 지난 점검 결과가 그 단계를 가리킨다. 단계 코드도 바꾸지 않는다. -----------
create or replace function ministp_guard_close_steps() returns trigger language plpgsql as $$
begin
    if tg_op = 'UPDATE' and new.step_code = old.step_code then
        new.updated_at := now();
        return new;
    end if;
    raise exception 'close_steps: % of a step is not allowed (rename it or switch it off)', lower(tg_op)
        using errcode = 'insufficient_privilege';
end $$;

create trigger close_steps_guard before update or delete on close_steps
    for each row execute function ministp_guard_close_steps();
create trigger close_steps_no_truncate before truncate on close_steps
    for each statement execute function ministp_forbid_change();

-- 3. 단계 (ministp.closeboard.STEP_DEFAULTS와 같다) -----------------------------------------------
insert into close_steps (step_code, name, sort_order, in_use, required, screen, asset_type, description) values
    ('SETTLEMENT', '결제', 1, true, true, 'STP-3400', null, '그날 결제일인 체결이 모두 결제됐는지'),
    ('ORDERS', '주문·체결', 2, true, true, 'STP-1200', null, '낸 주문마다 접수·체결·결제 진행'),
    ('DIVIDENDS', '배당', 3, true, true, 'STP-3200', null, '그날 인식·입금할 배당이 기록됐는지 (R-57~59)'),
    ('PRICES', '시세', 4, true, true, 'STP-2300', null, '보유 종목 종가를 받았고 크게 튄 값이 없는지'),
    ('NAV', '기준가', 5, true, true, 'STP-2210', null, '기준가를 냈고 순자산 변동이 분개로 설명되는지'),
    ('RECON', '증권사 대사', 6, true, true, 'STP-2100', null, '일 마감 대사의 불일치를 모두 판단했는지'),
    ('DAY_CLOSE', '일 마감', 7, true, true, 'STP-2200', null, '장부를 그날까지 마감했는지 (R-77)'),
    ('ERP_EXPORT', 'ERP 전송', 8, true, false, null, null, '그날 전표를 ERP로 내보냈는지 (R-104). 기본은 선택 단계'),
    ('INTEGRITY', '원장 내부 대사', 9, true, true, 'STP-2110', null, '지금 원장이 이벤트를 다시 계산한 결과와 같은지');

-- 4. 뷰 --------------------------------------------------------------------------------
create or replace view close_checks_last_v as
select distinct on (c.account_id, c.check_date)
       c.account_id, c.check_date, c.check_id, c.day_state, c.done, c.steps, c.command, c.checked_by, c.checked_at
  from close_checks c
 order by c.account_id, c.check_date, c.check_id desc;

-- 5. 설명 -------------------------------------------------------------------------------
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

-- 6. 새 코드 그룹과 코드 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) -----------
insert into code_groups (group_id, name, description, used_by, fixed) values
    ('CLOSE_STEP_STATE', '마감 점검 단계 상태', '마감 점검 단계마다 본 결과 (R-105).', 'close_check_steps.state', true),
    ('CLOSE_DAY_STATE', '마감 점검 일 상태', '필수 단계를 모아 본 그날의 상태 (R-105).', 'close_checks.day_state', true);
insert into common_codes (group_id, code, name, description, sort_order) values
    ('AUDIT_ACTION', 'CLOSE_STEP_SET', '마감 점검 단계 변경', '단계 이름·순서·사용·필수·화면·자산 유형을 바꿈', 35),
    ('CLOSE_STEP_STATE', 'DONE', '완료', null, 1),
    ('CLOSE_STEP_STATE', 'NONE', '대상 없음', '그날 할 일이 없음', 2),
    ('CLOSE_STEP_STATE', 'WAIT', '대기', '오늘 아직 하지 않음', 3),
    ('CLOSE_STEP_STATE', 'WARN', '주의', '끝났지만 한번 볼 것', 4),
    ('CLOSE_STEP_STATE', 'BAD', '확인 필요', '누가 판단해야 함', 5),
    ('CLOSE_STEP_STATE', 'MISSING', '누락', '날이 지났는데 하지 않음', 6),
    ('CLOSE_DAY_STATE', 'DONE', '점검 완료', '필수 단계가 모두 끝남', 1),
    ('CLOSE_DAY_STATE', 'OPEN', '진행 중', '오늘 아직 남은 필수 단계가 있음', 2),
    ('CLOSE_DAY_STATE', 'ATTENTION', '확인 필요', '필수 단계에 확인 필요나 누락이 있음', 3);
