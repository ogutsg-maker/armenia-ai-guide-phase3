# Armenia AI Guide — Final Architecture & Lifecycle Contract

## 1. Source of truth

The real PostgreSQL/Supabase database and deterministic backend rules are the only source of truth.

Runtime path:

```
TELEGRAM
   │
   ├── Client
   ├── Partner
   └── Admin
          │
       Direct UI ─────┐
          │           │
          └──────► AI Core
                     │
                 AI Context
                     │
                  Data Core
                     │
                 Supabase
```

Golden rule:

**AI understands. Python validates. Data Core reads/writes. PostgreSQL stores the fact.**

AI never receives SQL credentials, invents database IDs, or declares a business state that the backend has not confirmed.

---

## 2. Partner registration lifecycle

Registration is intentionally minimal.

```
Я партнёр
   ↓
🏢 Register your business
   ↓
Business name *
Phone *
   ↓
✓ Register
   ↓
partner
   ↓
company
   ↓
partner cabinet
```

Registration does **not** collect or require:

- directions/categories;
- services;
- prices;
- addresses;
- working hours;
- documents;
- classification;
- service applications.

The registration write is deterministic and atomic. It creates the partner and first company only.

All operational data is added later from the partner cabinet.

---

## 3. Partner cabinet

The cabinet contains:

- AI Assistant;
- My Companies;
- Orders;
- Negotiations;
- Applications;
- Notifications;
- Settings.

AI is the universal operator for business changes, but ordinary reads/buttons may remain deterministic.

Example:

> Создай ремонт холодильников от 5000.

AI extracts the requested structure, resolves the service against the live catalogue, prepares a preview, and asks for confirmation.

Before confirmation there is no active service mutation.

---

## 4. Service lifecycle

```
Partner request
    ↓
AI semantic understanding
    ↓
structured service proposal
    ↓
preview
    ↓
partner confirmation
    ↓
service application
    ↓
admin review
    ↓
live-catalog classification
    ↓
admin approval / direction verification
    ↓
activation
    ↓
ACTIVE marketplace service
```

The service model is intentionally compact:

- name;
- price;
- fixed/from;
- working hours;
- service location;
- customer-visit coverage;
- address when applicable;
- internal phone when applicable;
- document only when a direction requires verification.

AI never chooses a category ID from memory.

Classification uses the live catalogue and deterministic backend matching:

- normalization;
- token/root matching;
- SequenceMatcher / lexical scoring;
- dynamic majority evidence;
- confidence gate;
- margin gate.

Uncertain classification becomes **Չդասակարգված** and produces an admin classification alert instead of a false category.

---

## 5. Potential partner lifecycle

Potential partners are separate from registered partners.

```
Client demand
   ↓
no/insufficient active marketplace supply
   ↓
AI research
   ↓
potential_partners
   ↓
admin review/contact/invite
   ↓
real registration
   ↓
partners
```

`potential_partners` and `potential_partner_sources` are not substitutes for the real partner tables.

A potential partner becomes a real partner only through actual registration.

---

## 6. Client lifecycle

Client speaks naturally:

> Хочу мастера для ремонта холодильника в Раздане.

AI extracts the request. Data Core searches only real active services, partners, locations and availability.

The client may receive a small set of verified candidates.

No fictional partner/service may be generated as a marketplace result.

---

## 7. Fixed vs From pricing

### FIXED

A fixed service price is a concrete price.

Example:

`10,000 AMD`

When all conditions permit, the system may proceed toward booking without price negotiation.

### FROM

A from-price is only a starting price.

Example:

`от 5,000 AMD`

It is not the order price.

The system may notify suitable partners and open separate client↔partner negotiations.

---

## 8. Partner interest and negotiations

For FROM requests, suitable partners can receive a client-demand notification.

A non-response timeout is deterministic backend logic (currently the agreed three-minute window). A timed-out candidate remains in history; it is not deleted.

If multiple partners respond, negotiations are separate:

```
Client ↔ Partner 1
Client ↔ Partner 2
Client ↔ Partner 3
```

There is no group negotiation.

---

## 9. AI during negotiation

AI is a background extractor, not a third participant.

Client and partner communicate directly.

AI may extract:

- service;
- place;
- city/district/marz;
- date/time;
- proposed price;
- price range;
- other proposed conditions.

A proposal is not an agreement.

```
PROPOSAL
   ↓
client accepts the relevant conditions
   ↓
AGREED
```

The backend stores the agreed state.

A range such as 12,000–18,000 remains a range:

- agreed_min = 12000;
- agreed_max = 18000;
- commission base = deterministic backend value (e.g. midpoint when the agreed range remains a range).

If the parties later agree on 15,500, the backend stores 15,500 as the agreed price and uses that value for commission calculation.

AI does not calculate or invent the final commission.

---

## 10. Booking lifecycle

Booking is a backend state transition, not an AI declaration.

```
AGREED TERMS
   ↓
Booking
   ↓
PARTNER_CONFIRMED
   ↓
Payment
   ↓
PAYMENT_CONFIRMED
   ↓
Contact disclosure
   ↓
QR
   ↓
Check-in
   ↓
SERVICE_COMPLETED
   ↓
Review
```

Buttons and provider webhooks perform deterministic transitions.

AI cannot claim that payment succeeded unless the payment provider/backend confirms it.

---

## 11. Contact disclosure

Before payment, protected contact data remains hidden.

Examples:

- phone;
- Telegram;
- WhatsApp;
- email;
- unnecessary personal data;
- exact client address.

After `PAYMENT_CONFIRMED`, backend policy decides what necessary contact data may be disclosed.

---

## 12. QR and service execution

A QR is created only for an eligible paid booking.

QR validity is deterministic and expires after the agreed one-hour window from service start.

If arbitration is open, automatic expiry must not silently close the arbitration-controlled case.

Partner QR scan creates the backend check-in record.

Service completion is a backend state:

`SERVICE_COMPLETED`

It is not an AI-generated status.

---

## 13. Payment and commission

Payment provider/backend state is authoritative.

Commission is calculated by Data Core using the configured existing commission modes:

- `inside`;
- `on_top`;
- `fixed`.

AI only extracts user-provided commercial terms.

Test/fake settlement paths are not part of production.

---

## 14. Arbitration

```
Order
  ↓
Problem
  ↓
Arbitration
  ↓
Admin
  ↓
Resolution
```

AI may summarize context and conversation history.

AI does not make the arbitration decision.

Backend/admin rules remain authoritative.

---

## 15. Notifications

Notifications are deterministic events where possible:

- registration;
- service approved/rejected;
- new client demand;
- partner interest;
- negotiation;
- booking;
- partner confirmation;
- payment;
- QR;
- service completion;
- review;
- arbitration.

AI may explain a complex notification, but simple system events do not need AI.

---

## 16. Admin

Admin AI is a natural-language database assistant.

Examples:

- how many applications are pending;
- which services are unclassified;
- what services a company has;
- what happened to an order;
- which potential partners exist in a city;
- how many AI operations ran today;
- what AI cost was generated.

Reads execute immediately.

Protected mutations require confirmation and backend authorization.

---

## 17. AI accounting

```
AI provider
   ↓
Python
   ↓
ai_cost_center
   ↓
ai_usage_ledger
   ↓
Admin statistics
```

Accounting failures must never break the user's business request.

---

## 18. Architecture boundaries

### AI

Responsible for:

- natural-language understanding;
- intent/entity extraction;
- proposal/meaning extraction;
- background negotiation understanding;
- natural-language admin queries.

### Python/backend

Responsible for:

- authentication;
- role resolution;
- validation;
- permissions;
- state transitions;
- timers;
- provider callbacks;
- contact disclosure;
- QR lifecycle.

### Data Core

Responsible for:

- live reads/searches;
- entity resolution;
- ownership checks;
- writes;
- transaction boundaries;
- business rules;
- persistence;
- history.

### PostgreSQL/Supabase

Stores the real facts.

No duplicate AI business database is allowed.

---

## 19. Removed architecture

The following are not part of the final runtime path:

- AI database/index;
- duplicated business-data cache used as truth;
- hardcoded catalogue IDs in AI prompts;
- direct SQL from AI;
- direct partner/admin mutation UI bypasses where the final contract requires AI;
- legacy storefront paths;
- fake payment settlement;
- old partner registration questionnaire/application flow;
- command-only AI interfaces.

The final system is one connected set of lifecycles, not a collection of unrelated features.
