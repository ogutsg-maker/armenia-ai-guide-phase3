from __future__ import annotations
import json,os
from openai import AsyncOpenAI
from config import GROQ_API_KEY,OPENAI_API_KEY,OPENROUTER_API_KEY,AI_MODEL,AI_TIMEOUT
PROVIDERS=[("groq",GROQ_API_KEY,"https://api.groq.com/openai/v1",AI_MODEL),("openai",OPENAI_API_KEY,"https://api.openai.com/v1",os.getenv("OPENAI_MODEL","gpt-4o-mini")),("openrouter",OPENROUTER_API_KEY,"https://openrouter.ai/api/v1",os.getenv("OPENROUTER_MODEL","openai/gpt-4o-mini"))]
async def ask(messages,tools=None):
    last=None
    for provider,key,base,model in PROVIDERS:
        if not key: continue
        try:
            r=await AsyncOpenAI(api_key=key,base_url=base,timeout=AI_TIMEOUT).chat.completions.create(model=model,messages=messages,tools=tools,tool_choice="auto" if tools else None)
            u=r.usage
            return {"text":r.choices[0].message.content or "","tool_calls":r.choices[0].message.tool_calls or [],"provider":provider,"model":model,"input_tokens":getattr(u,"prompt_tokens",0),"output_tokens":getattr(u,"completion_tokens",0)}
        except Exception as e:last=e
    raise RuntimeError(f"ai_provider_failed:{last}")
async def extract(text,kind):
    prompt=f"""Armenia AI Guide extraction layer. Return JSON only.
Task: {kind}. Never invent IDs, prices, locations, states, or facts.
User text: {text}
For service_creation use keys name,price_type (fixed/from),price_amd,hours,at_client,territory.
For client_search use service,city,district.
For negotiation_terms use agreed_min,agreed_max,agreed_price,service,date,time.
Unknown = null."""
    r=await ask([{"role":"user","content":prompt}])
    try:return json.loads(r["text"]),r
    except Exception:return {},r
ADMIN_TOOLS=[
{"type":"function","function":{"name":"admin_list_service_applications","description":"List service applications requiring classification or admin review.","parameters":{"type":"object","properties":{"status":{"type":"string","enum":["CLASSIFICATION_PENDING","PENDING_ADMIN","REJECTED","ACTIVE"]}}}}},
{"type":"function","function":{"name":"admin_list_uncategorized_services","description":"List services without a catalog category or with uncertain classification.","parameters":{"type":"object","properties":{}}}},
{"type":"function","function":{"name":"admin_ai_costs_today","description":"Return AI operation count and token/cost totals for today.","parameters":{"type":"object","properties":{}}}},
{"type":"function","function":{"name":"admin_list_potential_partners","description":"List potential partners, optionally filtered by city or status.","parameters":{"type":"object","properties":{"city":{"type":"string"},"status":{"type":"string"}}}}},
{"type":"function","function":{"name":"admin_get_order","description":"Return complete backend state for one booking/order.","parameters":{"type":"object","properties":{"booking_id":{"type":"integer"}},"required":["booking_id"]}}},
{"type":"function","function":{"name":"admin_list_arbitrations","description":"List open arbitrations.","parameters":{"type":"object","properties":{}}}}
]
def admin_prompt(text):
    return [{"role":"user","content":"""You are the Armenia AI Guide admin operator.
Understand the administrator's natural-language request and use only the available Data Core tools.
Never invent database facts, IDs, statuses, costs, or people.
Reads are immediate. Writes must require explicit confirmation.
Answer in the administrator's language.
Request: """+text}]
