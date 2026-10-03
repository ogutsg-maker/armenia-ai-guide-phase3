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


TOOL_HANDLERS = {}
def register_tool(name, fn):
    TOOL_HANDLERS[name]=fn
async def execute_tool(name,args,context):
    fn=TOOL_HANDLERS.get(name)
    if not fn: raise RuntimeError("unknown_tool:"+name)
    return await fn(args,context)
async def admin_tool(name,args,uid):
    from core import DataCore
    if name=="admin_list_service_applications":
        q="SELECT a.*,s.name service_name,s.status service_status,c.name company_name FROM aig_service_applications a JOIN aig_services s ON s.id=a.service_id JOIN aig_companies c ON c.id=s.company_id"
        vals=[]
        if args.get("status"): q+=" WHERE a.status=%s OR s.status=%s";vals=[args["status"],args["status"]]
        return db.all(q+" ORDER BY a.id DESC",vals)
    if name=="admin_list_uncategorized_services":
        return db.all("SELECT s.id,s.name,s.status,s.classification_confidence,s.classification_margin,c.name company_name FROM aig_services s JOIN aig_companies c ON c.id=s.company_id WHERE s.catalog_category_id IS NULL OR s.status='CLASSIFICATION_PENDING' ORDER BY s.id DESC")
    if name=="admin_ai_costs_today":
        return db.one("SELECT count(*) operations,coalesce(sum(input_tokens+output_tokens),0) tokens,coalesce(sum(usd),0) usd FROM aig_ai_costs WHERE created_at::date=current_date")
    if name=="admin_list_potential_partners":
        q="SELECT * FROM aig_potential_partners WHERE 1=1";v=[]
        if args.get("city"):q+=" AND city ILIKE %s";v.append("%"+args["city"]+"%")
        if args.get("status"):q+=" AND status=%s";v.append(args["status"])
        return db.all(q+" ORDER BY id DESC",v)
    if name=="admin_get_order":
        return db.one("SELECT b.*,n.status negotiation_status,s.name service_name,c.name company_name FROM aig_bookings b JOIN aig_negotiations n ON n.id=b.negotiation_id JOIN aig_services s ON s.id=n.service_id JOIN aig_companies c ON c.id=s.company_id WHERE b.id=%s",(int(args["booking_id"]),))
    if name=="admin_list_arbitrations":
        return db.all("SELECT a.*,b.status booking_status FROM aig_arbitrations a JOIN aig_bookings b ON b.id=a.booking_id WHERE a.status='OPEN' ORDER BY a.id DESC")
    raise RuntimeError("unsupported_tool:"+name)
async def admin_ai_turn(text,uid):
    messages=admin_prompt(text)
    meta=await ask(messages,ADMIN_TOOLS)
    calls=meta["tool_calls"]
    if not calls:return meta
    results=[]
    for call in calls:
        name=call.function.name
        args=json.loads(call.function.arguments or "{}")
        result=await admin_tool(name,args,uid)
        results.append({"name":name,"result":result})
    follow=messages+[{"role":"assistant","content":meta["text"],"tool_calls":calls}]
    for item,call in zip(results,calls):
        follow.append({"role":"tool","tool_call_id":call.id,"content":json.dumps(item["result"],ensure_ascii=False,default=str)})
    final=await ask(follow,ADMIN_TOOLS)
    final["tool_results"]=results
    final["input_tokens"]+=meta["input_tokens"];final["output_tokens"]+=meta["output_tokens"]
    final["provider"]=meta["provider"];final["model"]=meta["model"]
    return final
