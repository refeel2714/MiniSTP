-- title: ERP 전송 이력: 내보낸 파일과 전표, 무효 처리, 같은 전표 두 번 막기
-- erd: 2.11
-- changes-data: code_groups, common_codes
--
-- ERD v2.10 -> v2.11 (스펙 R-104). 새 테이블 셋(셋 다 수정·삭제 금지), 중복 차단 트리거 하나, 뷰 하나, 코드 그룹 둘과 코드를 더한다.
-- 기존 테이블의 기존 행은 건드리지 않는다. code_groups와 common_codes는 새 행이 늘어나서 위에 적었다.
-- `erp export`가 파일을 만들 때마다 내보내기 한 행과 그 파일에 든 전표(증빙번호)마다 한 행을 남긴다. 무효로 하지 않은
-- 내보내기에 이미 든 전표, 또는 같은 계좌·같은 날을 다른 단위(건별 ↔ 일별 합계)로 낸 전표는 DB가 거부한다.

-- 1. 테이블 ---------------------------------------------------------------------------------
create table erp_exports (
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

create table erp_export_docs (
    export_id  bigint not null references erp_exports (export_id),
    doc_no     text not null check (doc_no ~ '^MSTP-'),
    account_id text not null references trading_accounts (account_id),
    doc_date   date not null,
    primary key (export_id, doc_no)
);
create index erp_export_docs_doc_idx on erp_export_docs (doc_no);
create index erp_export_docs_day_idx on erp_export_docs (account_id, doc_date);

create table erp_export_voids (
    export_id  bigint primary key references erp_exports (export_id),
    reason     text not null check (reason <> ''),
    voided_by  text not null check (voided_by <> ''),
    voided_at  timestamptz not null default now()
);

do $$
declare t text;
begin
    foreach t in array array['erp_exports', 'erp_export_docs', 'erp_export_voids'] loop
        execute format('create trigger %I before update or delete on %I
                        for each row execute function ministp_forbid_change()', t || '_append_only', t);
        execute format('create trigger %I before truncate on %I
                        for each statement execute function ministp_forbid_change()', t || '_no_truncate', t);
    end loop;
end $$;

-- 2. 같은 전표 두 번 막기 (R-104) ----------------------------------------------------------------
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

create trigger erp_export_docs_once before insert on erp_export_docs
    for each row execute function ministp_erp_doc_once();

-- 3. 뷰 --------------------------------------------------------------------------------
create or replace view erp_exports_v as
select e.export_id, e.account_id, e.date_from, e.date_to, e.unit, e.company_code, e.document_type, e.mapping_sha256,
       e.documents, e.lines, e.debit_total, e.credit_total, e.file_name, e.file_sha256, e.created_by, e.created_at,
       case when v.export_id is null then 'EXPORTED' else 'VOIDED' end as status,
       v.reason as void_reason, v.voided_by, v.voided_at
  from erp_exports e
  left join erp_export_voids v using (export_id);

-- 4. 설명 -------------------------------------------------------------------------------
comment on table erp_exports is 'ERP 전송 이력 (erp export가 만든 파일마다 한 행, 수정·삭제 불가)';
comment on column erp_exports.export_id is '내보내기 번호';
comment on column erp_exports.account_id is '계좌 ID (비어 있으면 모든 계좌)';
comment on column erp_exports.date_from is '시작일';
comment on column erp_exports.date_to is '종료일';
comment on column erp_exports.unit is '전표 단위 (ENTRY 건별 / DAY 일별 합계)';
comment on column erp_exports.company_code is '회사코드 (매핑 파일, 없으면 빈 값)';
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

-- 5. 새 코드 그룹과 코드 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) -----------
insert into code_groups (group_id, name, description, used_by, fixed) values
    ('ERP_EXPORT_UNIT', 'ERP 전표 단위', 'ERP로 내보낸 전표의 모양 (R-96).', 'erp_exports.unit', true),
    ('ERP_EXPORT_STATUS', 'ERP 전송 상태', '내보낸 파일을 ERP가 받았는지 (R-104).', 'erp_exports_v.status', true);
insert into common_codes (group_id, code, name, description, sort_order) values
    ('AUDIT_ACTION', 'ERP_EXPORT', 'ERP 전표 내보내기', '일반전표 업로드 파일과 통제 파일을 만들고 전송 이력을 남김', 33),
    ('AUDIT_ACTION', 'ERP_EXPORT_VOID', 'ERP 전송 무효', 'ERP가 받지 않은 파일을 무효로 해서 그 전표를 다시 낼 수 있게 함', 34),
    ('ERP_EXPORT_UNIT', 'ENTRY', '건별', '분개 1건이 전표 1건', 1),
    ('ERP_EXPORT_UNIT', 'DAY', '일별 합계', '계좌·날마다 전표 1건, 계정별 순액', 2),
    ('ERP_EXPORT_STATUS', 'EXPORTED', '내보냄', '파일과 통제 파일을 만듦. 그 전표는 다시 내지 않아요', 1),
    ('ERP_EXPORT_STATUS', 'VOIDED', '무효', 'ERP가 받지 않음. 그 전표를 다시 낼 수 있어요', 2);
