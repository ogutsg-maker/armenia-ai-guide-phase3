"""Provider/model-agnostic AI usage and cost ledger.

The AI layer records every model call without owning business data. Pricing is
configuration-driven so changing provider/model never requires code changes.
"""
from __future__ import annotations
import json, os
from typing import Any
import platform_db


def _pricing() -> dict:
    raw = os.getenv("AI_PRICING_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _price_for(provider: str, model: str) -> tuple[float, float, float]:
    p = _pricing()
    key = f"{provider}:{model}"
    row = p.get(key) or p.get(provider) or {}
    if not isinstance(row, dict):
        return 0.0, 0.0, 0.0
    try:
        inp = float(row.get("input_per_1m_usd", 0) or 0)
        out = float(row.get("output_per_1m_usd", 0) or 0)
        cached = float(row.get("cached_input_per_1m_usd", 0) or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0
    return max(0, inp), max(0, out), max(0, cached)


def _usd_amd_rate() -> float:
    try:
        return max(0.0, float(os.getenv("AI_USD_AMD_RATE", "0") or 0))
    except (TypeError, ValueError):
        return 0.0


def calculate_cost(provider: str, model: str, input_tokens: int = 0,
                   output_tokens: int = 0, cached_tokens: int = 0) -> dict:
    inp, out, cached = _price_for(provider, model)
    normal_input = max(0, int(input_tokens or 0) - int(cached_tokens or 0))
    cost = normal_input / 1_000_000 * inp
    cost += max(0, int(cached_tokens or 0)) / 1_000_000 * cached
    cost += max(0, int(output_tokens or 0)) / 1_000_000 * out
    return {
        "input_cost_usd": round(normal_input / 1_000_000 * inp, 10),
        "cached_input_cost_usd": round(max(0, int(cached_tokens or 0)) / 1_000_000 * cached, 10),
        "output_cost_usd": round(max(0, int(output_tokens or 0)) / 1_000_000 * out, 10),
        "total_cost_usd": round(cost, 10),
        "pricing_configured": bool(inp or out or cached),
        "usd_amd_rate": _usd_amd_rate(),
        "total_cost_amd": round(cost * _usd_amd_rate(), 4),
    }


def record_usage(*, provider: str, model: str, chain: str = "unknown",
                 stage: str = "unknown", operation: str = "chat",
                 purpose: str = "", user_id: int | None = None,
                 partner_id: int | None = None, company_id: int | None = None,
                 order_id: int | None = None, negotiation_id: int | None = None,
                 input_tokens: int = 0, output_tokens: int = 0,
                 cached_tokens: int = 0, reasoning_tokens: int = 0,
                 status: str = "success", error: str = "") -> dict:
    costs = calculate_cost(provider, model, input_tokens, output_tokens, cached_tokens)
    try:
        return platform_db.execute(
            """INSERT INTO ai_usage_ledger
               (provider,model,chain,stage,operation,purpose,user_id,partner_id,company_id,
                order_id,negotiation_id,input_tokens,output_tokens,cached_input_tokens,
                reasoning_tokens,total_tokens,input_cost_usd,cached_input_cost_usd,
                output_cost_usd,total_cost_usd,total_cost_amd,exchange_rate_amd,status,error)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (provider,model,chain,stage,operation,purpose,user_id,partner_id,company_id,
             order_id,negotiation_id,int(input_tokens or 0),int(output_tokens or 0),
             int(cached_tokens or 0),int(reasoning_tokens or 0),
             int(input_tokens or 0)+int(output_tokens or 0),costs["input_cost_usd"],
             costs["cached_input_cost_usd"],costs["output_cost_usd"],costs["total_cost_usd"],
             costs["total_cost_amd"],costs["usd_amd_rate"],status,error[:1000]),
            True,
        ) or {}
    except Exception:
        # AI usage accounting must never break a successful user request.
        return {}


def usage_summary(*, partner_id: int | None = None, days: int = 30) -> dict:
    where=["created_at >= NOW() - (%s || ' days')::interval"]
    vals=[max(1,min(int(days or 30),3650))]
    if partner_id is not None:
        where.append("partner_id=%s"); vals.append(int(partner_id))
    sql=" AND ".join(where)
    row=platform_db.one(
        f"""SELECT COUNT(*) operations,
                   COALESCE(SUM(input_tokens),0) input_tokens,
                   COALESCE(SUM(output_tokens),0) output_tokens,
                   COALESCE(SUM(cached_input_tokens),0) cached_input_tokens,
                   COALESCE(SUM(reasoning_tokens),0) reasoning_tokens,
                   COALESCE(SUM(total_tokens),0) total_tokens,
                   COALESCE(SUM(total_cost_usd),0) total_cost_usd,
                   COALESCE(SUM(total_cost_amd),0) total_cost_amd
            FROM ai_usage_ledger WHERE {sql}""", vals)
    rows=platform_db.rows(
        f"""SELECT provider,model,chain,stage,operation,purpose,input_tokens,output_tokens,
                   cached_input_tokens,reasoning_tokens,total_tokens,total_cost_usd,total_cost_amd,exchange_rate_amd,created_at
            FROM ai_usage_ledger WHERE {sql}
            ORDER BY created_at DESC LIMIT 500""", vals)
    return {"summary":row or {}, "items":rows}


def partner_breakdown(*, days: int = 30) -> list[dict]:
    d=max(1,min(int(days or 30),3650))
    return platform_db.rows(
        """SELECT partner_id, COUNT(*) operations,
                  COALESCE(SUM(input_tokens),0) input_tokens,
                  COALESCE(SUM(output_tokens),0) output_tokens,
                  COALESCE(SUM(total_tokens),0) total_tokens,
                  COALESCE(SUM(total_cost_usd),0) total_cost_usd
           FROM ai_usage_ledger
           WHERE created_at >= NOW() - (%s || ' days')::interval
           GROUP BY partner_id ORDER BY total_cost_usd DESC""",(d,))


def provider_breakdown(*, days: int = 30) -> list[dict]:
    d=max(1,min(int(days or 30),3650))
    return platform_db.rows(
        """SELECT provider,model,COUNT(*) operations,
                  COALESCE(SUM(input_tokens),0) input_tokens,
                  COALESCE(SUM(output_tokens),0) output_tokens,
                  COALESCE(SUM(total_tokens),0) total_tokens,
                  COALESCE(SUM(total_cost_usd),0) total_cost_usd
           FROM ai_usage_ledger
           WHERE created_at >= NOW() - (%s || ' days')::interval
           GROUP BY provider,model ORDER BY total_cost_usd DESC""",(d,))


def admin_overview(days: int = 30) -> dict:
    base=usage_summary(days=days)
    base["partners"]=partner_breakdown(days=days)
    base["providers"]=provider_breakdown(days=days)
    return base
