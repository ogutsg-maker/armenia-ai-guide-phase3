# Armenia AI Guide — Final AI/Data Architecture

## Principle

The real business database is the only source of truth.

There is no AI database, platform index, duplicated entity cache, or large prebuilt business context between AI and PostgreSQL.

## Runtime path

```
Client ────┐
Partner ───┼──> Groq AI
Admin ─────┘       ↓
                Python
                   ↓
                Data Core
                   ↓
          Supabase / PostgreSQL
                   ↓
                Data Core
                   ↓
                Python
                   ↓
                Groq AI
                   ↓
          Client / Partner / Admin
```

## Responsibilities

### Groq AI
- understands natural language;
- extracts intent, entities and user-provided values;
- chooses the appropriate Python operation when a planner is used;
- never invents database facts or IDs;
- never receives SQL credentials.

### Python
- authenticates the caller;
- determines role: client, partner or admin;
- validates arguments;
- resolves named entities against live database data;
- applies business rules and permissions;
- asks for confirmation before protected mutations;
- formats the verified result for AI/user.

### Data Core
Data Core is the single application-owned gateway to the real database.

It performs:
- reads and searches;
- counts;
- entity resolution;
- ownership/permission checks;
- writes and validation;
- persistence and history.

No second business-data representation is created for AI.

### Supabase / PostgreSQL
Stores the actual business facts:
- users;
- partners;
- companies;
- addresses;
- services;
- applications;
- catalog;
- orders;
- negotiations;
- documents;
- AI usage ledger.

Existing schema and data are preserved. This architecture does not reset or recreate the database.

## AI usage statistics

AI accounting remains independent from business-data context:

```
Groq → Python → ai_cost_center → ai_usage_ledger → Admin statistics
```

Every model call may be recorded with provider, model, operation, token usage and cost. Accounting failures must not break the user request.

## Client

Client requests use live catalog/service data.

Only the smallest verified records required for the current request may be sent to Groq. The complete catalog is never copied into a persistent AI context.

## Partner

Partner onboarding collects the partner's own business information.

During registration the partner does not select direction/subcategory/catalog IDs. Catalog classification is an admin/application concern after submission.

Company, address and service changes use Python/Data Core operations and are protected by ownership and confirmation rules.

## Admin

Admin AI is a natural-language database assistant.

Examples:
- "քանի հայտ ունենք" → live COUNT;
- "ինչ ուղղություններով կան հայտեր" → live application-direction query;
- "ինչ ծառայություններ ունի BYUTI-ն" → resolve BYUTI → live company/service query;
- "ամբողջական հայտը ցույց տուր" → use the current application/entity reference → live application + documents query.

A read operation executes immediately. A protected mutation requires confirmation.

## Removed architecture

The following are not part of the runtime data path:
- AI Context database/index;
- duplicated platform index;
- prebuilt entity snapshots used as a substitute for database reads;
- large persistent AI business-data context;
- LangChain/CrewAI/AutoGen orchestration.

Legacy AI Context / AI Tools modules have been removed from the runtime. There is no compatibility data layer between AI and Data Core.

## Golden rule

**AI understands. Python validates. Data Core reads/writes. PostgreSQL stores the fact.**
