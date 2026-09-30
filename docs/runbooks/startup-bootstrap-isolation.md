# Startup Bootstrap Isolation — Runbook

## Scope

This runbook validates the separation between runtime bootstrap and versioned
database migration for the business/direction schema.

Production Supabase and Render are not modified by this runbook.

## 1. Preconditions

- Work only on branch `refactor/startup-bootstrap-isolation`.
- PostgreSQL 17 is used for the integration database.
- The migration file is applied to an isolated database only.
- Do not execute the migration against production during this phase.

## 2. Static source checks

Verify that these runtime builders contain only create-if-missing bootstrap DDL:

- `platform_schema.py`
- `partner_business_application_api.py`
- `partner_directions_api.py`
- `partner_lifecycle_schema.py`

Forbidden in runtime builders:

- `ALTER TABLE`
- `DROP TABLE/INDEX/CONSTRAINT`
- `UPDATE`
- `DELETE`
- `INSERT ... SELECT` backfills
- trigger/function installation or replacement

Allowed runtime schema operations are create-only bootstrap operations such as
`CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`.

## 3. Migration invariants

Before applying the migration, verify:

1. `partner_verification_documents.partner_direction_id` remains a plain
   `BIGINT`.
2. The migration does not add a foreign key for that column.
3. `application_id`, `business_id`, `is_current`, and `replaced_by`
   are added with `ADD COLUMN IF NOT EXISTS`.
4. Document-to-direction reconciliation is DML-only and contains:
   `partner_direction_id IS NULL` plus an `EXISTS` check.
5. The final direction uniqueness is:
   `uq_partner_direction_business_master` on
   `(business_id, master_category_id)` with `status <> 'deleted'`.
6. The legacy
   `partner_directions_partner_id_master_category_id_key` is removed only
   after duplicate repair.

## 4. Scenario A — Legacy database migration

Create a PostgreSQL 17 database containing the real legacy dependency graph.

Capture a pre-migration snapshot of:

- table/column definitions for all affected tables;
- constraints and indexes;
- counts of partners, businesses, applications, directions, direction
  categories, services, documents and objects;
- rows with NULL `business_id`;
- duplicate direction groups;
- documents whose `partner_direction_id` does not resolve to a direction.

Apply:

`migrations/20260930_isolate_runtime_schema_mutations.sql`

Then verify:

- all required business columns exist;
- default businesses exist for eligible legacy partners;
- NULL business references were reconciled where a default business exists;
- approved application links were reconciled;
- directions missing from active/approved services were reconstructed;
- direction-category links were rebuilt;
- document direction links were filled only where the direction/category
  relationship exists;
- stale `partner_service` applications for archived businesses were removed;
- duplicate non-deleted directions were repaired before the final unique index;
- the final partial unique index exists;
- no FK exists from
  `partner_verification_documents.partner_direction_id` to
  `partner_directions`.

## 5. Scenario B — Clean bootstrap

Start with an empty PostgreSQL 17 database.

Run the application's real startup lifecycle, not individual schema-builder
functions.

The test must include the real `DatabaseManager` initialization path and the
real bootstrap lifecycle callback.

Verify:

- dependency order is valid;
- clean startup succeeds without running the migration;
- required base tables are created;
- business/direction builders do not require migration-only columns to exist
  before their dependencies are created;
- no business data backfill is performed during startup.

## 6. Scenario C — Runtime idempotency

Start from a database that already has the modern schema.

Capture snapshot A.

Run the complete application startup twice.

Capture snapshot B.

Assert:

- schema objects are unchanged;
- row counts are unchanged;
- business/direction/application/document data are unchanged;
- no UPDATE/DELETE/INSERT backfill is emitted by runtime builders;
- no trigger/function replacement occurs during startup.

A third startup may be used as an additional idempotency check.

## 7. SQL guard

The integration harness should intercept SQL at the psycopg v3 connection
boundary before `DatabaseManager` is instantiated.

Do not replace individual business functions with mocks: the purpose is to
exercise the real startup path.

The guard must normalize SQL comments and whitespace before classifying
statements. It must inspect every statement, including multi-statement
`execute()` calls.

Forbidden startup statements must fail the test immediately.

## 8. Acceptance criteria

The phase is complete only when all three scenarios pass:

- A: legacy migration succeeds and preserves historical references;
- B: clean bootstrap succeeds in dependency order;
- C: repeated runtime startup is idempotent and mutation-free.

Only after these tests pass should the migration be considered for controlled
production rollout.
