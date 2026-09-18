-- ═══════════════════════════════════════════════════════════════════════
-- Armenia AI Guide — ПОЛНЫЙ СБРОС БАЗЫ ДАННЫХ (обнуление)
-- ═══════════════════════════════════════════════════════════════════════
-- ВНИМАНИЕ: удаляет ВСЕ таблицы проекта вместе с данными. Необратимо.
-- Дропаются только таблицы этого проекта (схема public). Служебные
-- объекты Supabase (auth, storage, extensions) НЕ трогаются.
--
-- Запуск (пример): psql "$DATABASE_URL" -f reset_database.sql
-- Либо: python reset_database.py
-- ═══════════════════════════════════════════════════════════════════════

BEGIN;

DROP TABLE IF EXISTS
    admin_audit_log,
    admin_clarifications,
    admin_settings,
    ai_catalog_proposals,
    ai_messages,
    ai_research_tasks,
    ai_sessions,
    booking_cancellations,
    booking_checkins,
    bookings,
    categories,
    category_settings,
    client_profiles,
    contact_disclosures,
    master_categories,
    master_skills,
    negotiation_messages,
    negotiations,
    notifications,
    partner_direction_categories,
    partner_directions,
    partner_employees,
    partner_financial_ledger,
    partner_locations,
    partner_objects,
    partner_payouts,
    partner_verification_documents,
    partners,
    payments,
    potential_partner_sources,
    potential_partners,
    request_candidates,
    reviews,
    service_options,
    service_packages,
    service_requests,
    service_schedule,
    services,
    services_catalog_dummy_guard,
    support_ticket_messages,
    support_tickets,
    users
CASCADE;

COMMIT;

-- После сброса запустите: python setup_fresh_supabase.py
