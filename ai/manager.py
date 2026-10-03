from .core import chat
from .prompts import prompt
from db import exec
async def turn(uid,context,text):
 r=await chat(prompt(context,text))
 exec("INSERT INTO aig_ai_costs(telegram_id,provider,model,operation,purpose,input_tokens,output_tokens) VALUES(%s,%s,%s,%s,%s,%s,%s)",(uid,r["provider"],r["model"],context,"natural_language",r["input_tokens"],r["output_tokens"]))
 return r