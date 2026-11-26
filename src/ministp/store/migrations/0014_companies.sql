-- title: 고객을 법인으로: 법인·ERP 회사코드 이력, 계좌의 법인 ID
-- erd: 2.14
-- renames: customers -> companies, customers.customer_id -> company_id, trading_accounts.customer_id -> company_id
-- changes-data: common_codes
--
-- ERD v2.13 -> v2.14 (스펙 D7 v2). 계좌 위의 층은 리테일 위탁 고객이 아니라 장부를 가진 법인(회사)이다(D9).
-- 테이블 하나와 컬럼 둘의 이름을 바꾸고(행은 그대로: 바뀐 이름으로 다시 지문을 떠서 확인한다), 법인마다 ERP 회사코드를
-- 적용시작일과 함께 쌓는 이력 테이블을 더한다. 법인 ID의 C는 Company로 읽는다(값과 규칙은 그대로).
-- 기존 법인의 별칭(예: '기본 고객')은 고치지 않는다. 법인 테이블도 추가 전용이다.
-- common_codes는 감사 행위 두 개(법인 등록, ERP 회사코드 지정)가 늘고, 예전 행위 CUSTOMER_ADD의 이름·설명이 바뀐다.

-- 1. 이름 바꾸기 ---------------------------------------------------------------------------
alter table customers rename to companies;
alter table companies rename column customer_id to company_id;
alter table companies rename constraint customers_pkey to companies_pkey;
alter table companies rename constraint customers_customer_id_check to companies_company_id_check;
alter trigger customers_append_only on companies rename to companies_append_only;
alter trigger customers_no_truncate on companies rename to companies_no_truncate;

alter table trading_accounts rename column customer_id to company_id;
alter table trading_accounts rename constraint trading_accounts_customer_id_fkey to trading_accounts_company_id_fkey;

alter view account_status_v rename column customer_id to company_id;
alter view customer_positions_v rename to company_positions_v;
alter view company_positions_v rename column customer_id to company_id;

-- 2. 법인의 ERP 회사코드 이력: 바꿀 때는 새 행을 더한다 ------------------------------------------
create table company_erp_codes (
    id               bigserial primary key,
    company_id       text not null references companies (company_id),
    erp_company_code text not null check (erp_company_code ~ '^[A-Za-z0-9_-]{1,20}$'),
    effective_from   date not null,
    reason           text,
    created_at       timestamptz not null default now()
);
create index company_erp_codes_company_idx on company_erp_codes (company_id, effective_from);

create trigger company_erp_codes_append_only before update or delete on company_erp_codes
    for each row execute function ministp_forbid_change();
create trigger company_erp_codes_no_truncate before truncate on company_erp_codes
    for each statement execute function ministp_forbid_change();

-- 3. 공통코드: 감사 로그 행위 ------------------------------------------------------------------
update common_codes set name = '법인 등록 (v2.14 전 코드)', description = 'ERD v2.14 전에 남은 기록에만 있어요. 지금은 COMPANY_ADD'
 where group_id = 'AUDIT_ACTION' and code = 'CUSTOMER_ADD';
insert into common_codes (group_id, code, name, description, sort_order) values
    ('AUDIT_ACTION', 'COMPANY_ADD', '법인 등록', '장부를 가진 법인(회사)을 별칭으로 등록', 36),
    ('AUDIT_ACTION', 'COMPANY_ERP_CODE', 'ERP 회사코드 지정', '법인의 ERP 회사코드를 적용시작일과 함께 기록', 37);

-- 4. 한글 이름 --------------------------------------------------------------------------------
comment on table companies is '법인 (장부를 가진 회사, 별칭만 관리)';
comment on column companies.company_id is '법인 ID';
comment on column companies.alias is '법인 별칭';
comment on column companies.created_at is '등록 시각';
comment on column trading_accounts.company_id is '법인 ID';
comment on column account_status_v.company_id is '법인 ID';
comment on view company_positions_v is '법인별·종목별 보유 합계 (여러 계좌 합산)';
comment on column company_positions_v.company_id is '법인 ID';

comment on table company_erp_codes is '법인 ERP 회사코드 이력 (변경은 새 행으로 추가)';
comment on column company_erp_codes.id is '행 번호';
comment on column company_erp_codes.company_id is '법인 ID';
comment on column company_erp_codes.erp_company_code is 'ERP 회사코드';
comment on column company_erp_codes.effective_from is '적용 시작일';
comment on column company_erp_codes.reason is '사유';
comment on column company_erp_codes.created_at is '기록 시각';
comment on column erp_exports.company_code is '회사코드 (계좌 법인의 ERP 회사코드, 없으면 매핑 파일의 값, 법인이 여럿이면 쉼표로)';
