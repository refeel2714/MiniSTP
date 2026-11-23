-- title: 감사 로그: 누가 언제 무엇을 했는지, 해시 사슬
-- erd: 2.3
-- changes-data: code_groups, common_codes
--
-- ERD v2.2 -> v2.3. 새 테이블 하나와 그 트리거, 코드 그룹 세 개를 더한다.
-- 기존 테이블의 기존 행은 건드리지 않는다. code_groups와 common_codes는 새 행이 늘어나서 위에 적었다.

-- 1. 감사 로그 --------------------------------------------------------------------------
create table audit_log (
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
create index audit_log_account_idx on audit_log (account_id, id);

-- 2. 고칠 수 없는 기록이고 (R-04), 행마다 앞 행에 사슬로 묶인다 ---------------------------------
create trigger audit_log_append_only before update or delete on audit_log
    for each row execute function ministp_forbid_change();
create trigger audit_log_no_truncate before truncate on audit_log
    for each statement execute function ministp_forbid_change();

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

create trigger audit_log_chain before insert on audit_log
    for each row execute function ministp_audit_chain();

-- 3. 한글 명칭 ---------------------------------------------------------------------------
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

-- 4. 새 코드 그룹 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) ------------------
insert into code_groups (group_id, name, description, used_by, fixed) values
    ('AUDIT_CHANNEL', '감사 로그 경로', '어디로 들어온 요청인지.', 'audit_log.channel', true),
    ('AUDIT_OUTCOME', '감사 로그 결과', '요청이 어떻게 끝났는지.', 'audit_log.outcome', true),
    ('AUDIT_ACTION', '감사 로그 행위', '밖에서 시스템에 한 일의 종류.', 'audit_log.action', true);
insert into common_codes (group_id, code, name, description, sort_order) values
    ('AUDIT_CHANNEL', 'CLI', '명령', 'PC의 명령 창에서 실행', 1),
    ('AUDIT_CHANNEL', 'UI', '화면', '업무화면의 처리 버튼', 2),
    ('AUDIT_CHANNEL', 'SYSTEM', '시스템', '사람이 아닌 프로그램 (샘플 생성 등)', 3),
    ('AUDIT_OUTCOME', 'DONE', '처리', '요청대로 처리함', 1),
    ('AUDIT_OUTCOME', 'REFUSED', '거부', '규칙에 맞지 않아 시스템이 거부함. 바뀐 것은 없어요', 2),
    ('AUDIT_OUTCOME', 'FAILED', '실패', '증권사 응답 등 바깥 문제로 끝내지 못함', 3),
    ('AUDIT_ACTION', 'OPEN', '원장 시작', '증권사 잔고를 원장 시작값으로 기록', 1),
    ('AUDIT_ACTION', 'ORDER', '주문', '주문 한 건. 검증에서 거부된 주문도 남아요', 2),
    ('AUDIT_ACTION', 'ORDER_CANCEL', '주문 취소', '미체결 잔량 취소 요청 (R-45)', 3),
    ('AUDIT_ACTION', 'DAILY', '일일 마감·대사', '결제, 체결 반영, 기준가, 대사', 4),
    ('AUDIT_ACTION', 'CLOSE', '장부 마감', '불일치를 판단한 날을 직접 마감', 5),
    ('AUDIT_ACTION', 'RESOLVE', '불일치 원인 판단', null, 6),
    ('AUDIT_ACTION', 'INSTRUMENT_ADD', '종목 등록', null, 7),
    ('AUDIT_ACTION', 'CUSTOMER_ADD', '고객 등록', null, 8),
    ('AUDIT_ACTION', 'ACCOUNT_ADD', '계좌 등록', null, 9),
    ('AUDIT_ACTION', 'ACCOUNT_STATUS', '계좌 상태 변경', null, 10),
    ('AUDIT_ACTION', 'CODE_GROUP_ADD', '코드 그룹 추가', null, 11),
    ('AUDIT_ACTION', 'CODE_ADD', '코드 추가', null, 12),
    ('AUDIT_ACTION', 'CODE_RENAME', '코드명 변경', null, 13),
    ('AUDIT_ACTION', 'CODE_USE', '코드 사용 여부 변경', null, 14),
    ('AUDIT_ACTION', 'DB_MIGRATE', '스키마 마이그레이션', 'ERD 버전을 올림', 15);
