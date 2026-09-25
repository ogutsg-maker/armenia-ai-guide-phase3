# Armenia AI Guide — Final AI-First Architecture

## 1. Non-negotiable rule

The existing Supabase/PostgreSQL database is the source of truth.

**Do not recreate, reset, or replace the existing database for this architecture.**

The architecture is introduced above the existing schema. A database migration is added only when a real business capability cannot be represented by the current schema.

## 2. Layers

```
CLIENT / PARTNER / ADMIN
          |
          v
       AI CORE
          |
          v
      AI CONTEXT
          |
          v
       AI TOOLS
          |
          v
      DATA CORE
          |
          v
 SUPABASE / POSTGRESQL
```

The return path is the same in reverse.

### AI Core

Understands natural language, extracts intent, classifies requests, plans tool usage and produces human-readable responses.

AI may receive the user's raw message directly.

AI must not:
- access SQL;
- access Supabase credentials;
- invent entity IDs;
- invent partners/services/prices;
- bypass permissions;
- mutate business data without an allowed action.

### AI Context

Builds the smallest verified business context required for the current AI task.

Examples:
- Client Context: request + real search results + relevant negotiation state.
- Partner Context: own company/service/order/negotiation data.
- Admin Context: question + verified Data Core result + relevant history.
- Registration Context: draft application + extracted facts + allowed catalog subset.

AI Context does not become a second database.

### AI Tools

Tools are the controlled action interface exposed to AI.

Read examples:
- get_application
- search_applications
- count
- get_partner
- search_partners
- search_services
- search_catalog
- get_documents
- get_orders
- get_negotiation

Write examples:
- make_offer
- counter_offer
- accept_offer
- reject_offer
- update_service_price
- approve_application
- reject_application
- approve_document
- delete_company

Write tools are confirmation-required unless an explicit server-side business rule says otherwise.

### Data Core

Data Core is the only application-owned gateway from AI Context/Tools to real business data.

Responsibilities:
- database reads/writes;
- validation;
- role and ownership checks;
- allowed entity IDs;
- business rules;
- search/filtering;
- persistence;
- history/audit integration;
- compatibility with the current database schema.

The model never receives arbitrary SQL.

## 3. Client

Natural language is the primary interface.

Example:

> Мне нужна стрижка в Раздане до 5000 драм.

Flow:

```
message
 -> Client AI
 -> Client Context
 -> search_services tool
 -> Data Core
 -> existing DB
 -> real candidates
 -> Client Context
 -> AI response
```

Only real active/approved marketplace data may be presented as marketplace results.

## 4. Partner

Partner registration is free-form.

Partner describes the business, location, services, prices, phone and schedule.

Flow:

```
free text
 -> Partner AI extraction
 -> validation
 -> application draft
 -> partner review
 -> submit
 -> admin review
 -> approval
 -> company/services
```

The partner does not need to select master direction/subcategory during onboarding.

Catalog classification is an internal system task and uses IDs from the existing catalog.

## 5. Partner companies

A partner may own multiple companies.

```
Partner
  + Company A
      + addresses
      + services
      + documents
      + orders
  + Company B
      + addresses
      + services
      + documents
      + orders
```

Partner ownership is enforced by Data Core, not by AI prompts.

## 6. Negotiations

Negotiation is a real business object, not AI memory.

```
Client
 -> Client AI
 -> Client Context
 -> Negotiation Tool
 -> Data Core
 -> database
 -> Partner Context
 -> Partner AI
 -> Partner
```

Every meaningful offer is persisted.

Example:

```
6000 AMD -> client offer
7500 AMD -> partner counter-offer
7000 AMD -> client counter-offer
7000 AMD -> accepted
```

The final agreed price comes from the persisted negotiation state, not from the model's memory.

The same negotiation is visible to both sides through separate role-specific contexts.

## 7. Admin

Admin has normal UI controls plus the AI Secretary.

Natural language examples:

- «Քանի հայտ ունենք»
- «Покажи полную заявку #36»
- «Какие заявки из Котайка?»
- «Покажи услуги BYUTI»
- «Покажи историю цены Armenia Auto»

Read requests can execute immediately.

Mutations require confirmation:

```
Admin request
 -> AI plan
 -> confirmation
 -> write tool
 -> Data Core
 -> database
 -> audit/history
```

## 8. Potential partners

Potential partners are separate from registered partners.

```
Research source / Admin / AI discovery
 -> structured potential partner
 -> verification
 -> contact
 -> invitation
 -> registration
 -> application
 -> approval
 -> real partner
```

Finding a business never automatically creates an approved marketplace partner.

No fake partner is generated from AI text.

## 9. Catalog

The current catalog remains the source of classification IDs.

Hierarchical classification:

```
service text
 -> master direction
 -> only that direction's active subcategories
 -> validated existing ID
```

AI cannot invent a category ID.

## 10. Database policy

Current schema and data remain in place.

No "fresh database" is required for the AI architecture.

If a new table/column is eventually required:
1. identify the exact missing business capability;
2. create a versioned migration;
3. preserve existing IDs/data;
4. test against the current deployment;
5. deploy the migration separately from application code.

## 11. Provenance

Important facts should retain their source where the existing schema supports it.

Examples:
- price from application #36;
- status changed by admin;
- final negotiation price;
- document verification status.

AI answers factual "where did this come from?" questions from Data Core/history, not from model memory.

## 12. Implementation status in this branch

The first architecture migration introduces `data_core.py` as the canonical DB gateway and routes the shared AI Context, AI data tools and Client AI data access through it.

This is deliberately incremental: the existing database and business data are preserved while direct data access is moved behind the Data Core boundary.

Next migrations should move remaining domain modules behind named Data Core operations and add confirmation-aware write tools for negotiation/admin actions.

## 13. Golden rule

**AI thinks.  
AI Context gives AI the correct verified context.  
AI Tools define what AI is allowed to request.  
Data Core validates and performs the real operation.  
Supabase stores the fact.**

