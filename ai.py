from __future__ import annotations
import json
from openai import AsyncOpenAI
from config import GROQ_API_KEY,OPENAI_API_KEY,OPENROUTER_API_KEY,AI_MODEL,AI_TIMEOUT
PROVIDERS=[
 ("groq",GROQ_API_KEY,"https://api.groq.com/openai/v1",AI_MODEL),
 ("openai",OPENAI_API_KEY,"https://api.openai.com/v1",os.getenv("OPENAI_MODEL","gpt-4o-mini") if False else "gpt-4o-mini"),
 ("openrouter",OPENROUTER_API_KEY,"https://openrouter.ai/api/v1",os.getenv("OPENROUTER_MODEL","openai/gpt-4o-mini") if False else "openai/gpt-4o-mini")
]
async def ask(messages,tools=None):
    last=None
    for provider,key,base,model in PROVIDERS:
        if not key: continue
        try:
            client=AsyncOpenAI(api_key=key,base_url=base,timeout=AI_TIMEOUT)
            r=await client.chat.completions.create(model=model,messages=messages,tools=tools,tool_choice="auto" if tools else None)
            u=r.usage
            return {"text":r.choices[0].message.content or "","tool_calls":r.choices[0].message.tool_calls or [],"provider":provider,"model":model,"input_tokens":getattr(u,"prompt_tokens",0),"output_tokens":getattr(u,"completion_tokens",0)}
        except Exception as e:
            last=e
    raise RuntimeError(f"ai_provider_failed:{last}")
async def extract(text,kind):
    prompt=f"""You are the Armenia AI Guide extraction layer. Return JSON only.
Task: {kind}. Never invent database IDs. Never perform writes.
User text: {text}
For service creation return name,price_type(fixed/from),price_amd,hours,at_client,territory.
For client search return service,city,district.
For negotiation return agreed_min,agreed_max,agreed_price,service,date,time.
"""
    r=await ask([{"role":"user","content":prompt}])
    try:return json.loads(r["text"]),r
    except Exception: return {},r
