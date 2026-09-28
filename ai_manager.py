"""Unified AI entry point for REGISTRATION/CLIENT/PARTNER/ADMIN."""
from __future__ import annotations
import asyncio, json, logging, os, time
from typing import Any
from groq import AsyncGroq
import ai_cost_center
from history_provider import HistoryProvider
from prompt_factory import ContextType, PromptFactory
from tool_registry import ToolRegistry

logger=logging.getLogger(__name__)

class AIManager:
    def __init__(self, *, history_provider: HistoryProvider|None=None, model: str|None=None):
        key=os.getenv("GROQ_API_KEY","").strip()
        if not key: raise RuntimeError("GROQ_API_KEY is not configured")
        self.client=AsyncGroq(api_key=key)
        self.model=model or os.getenv("GROQ_MODEL","openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
        self.history=history_provider or HistoryProvider()
        self.max_tool_rounds=4

    @staticmethod
    def _lang(text: str) -> str:
        import re
        if re.search(r"[Ա-Ֆա-ֆևօՕ]",text): return "hy"
        if re.search(r"[А-Яа-яЁё]",text): return "ru"
        return "en"

    def _trusted(self, context_type: ContextType, telegram_id: int, extra: dict[str,Any]|None):
        ctx=dict(extra or {})
        ctx["authenticated_telegram_id"]=int(telegram_id)
        # Identity is supplied by backend, never inferred from the message.
        if context_type == ContextType.PARTNER:
            p=__import__("data_core").get_partner_by_user(int(telegram_id))
            if p: ctx["partner_id"]=p.get("id")
        return ctx

    async def chat(self, telegram_id:int, context_type:ContextType|str, message:str, *, extra_context:dict[str,Any]|None=None, language:str|None=None)->dict[str,Any]:
        role=ContextType(str(context_type))
        language=language or self._lang(message)
        tools=ToolRegistry(telegram_id=telegram_id,context_type=role,trusted_context=extra_context)
        history=self.history.get(telegram_id,role.value.lower())
        prompt=PromptFactory.build(role,message=message,history=history,trusted_context=self._trusted(role,telegram_id,extra_context),language=language)
        messages=[{"role":"user","content":prompt}]
        started=time.monotonic()
        self.history.append(telegram_id,role.value.lower(),"user",message,{"context_type":role.value})
        pending=self.history.get_pending(telegram_id,role.value.lower())
        if pending and message.strip().casefold() in {"yes","да","այո","հա","հաստատել","հաստատում եմ","confirm","ok"}:
            result=await tools.execute_confirmed(pending["name"],pending["args"])
            self.history.set_pending(telegram_id,role.value.lower(),None)
            reply="Կատարված է։" if language=="hy" else ("Готово." if language=="ru" else "Done.")
            self.history.append(telegram_id,role.value.lower(),"ai",reply,{"confirmed_action":result})
            return {"reply":reply,"tool_result":result,"confirmed":True}

        tool_calls_log=[]
        for _ in range(self.max_tool_rounds):
            response=await self.client.chat.completions.create(
                model=self.model,messages=messages,tools=tools.definitions() or None,
                tool_choice="auto" if tools.definitions() else None,
                temperature=0.1,max_tokens=1600,
            )
            usage=getattr(response,"usage",None)
            ai_cost_center.record_usage(provider="groq",model=self.model,
                chain=role.value.lower(),stage="manager",operation="chat",
                purpose="Unified AIManager",user_id=telegram_id,
                input_tokens=int(getattr(usage,"prompt_tokens",getattr(usage,"input_tokens",0)) or 0),
                output_tokens=int(getattr(usage,"completion_tokens",getattr(usage,"output_tokens",0)) or 0),
                cached_tokens=0,reasoning_tokens=0)
            msg=response.choices[0].message
            calls=getattr(msg,"tool_calls",None) or []
            if not calls:
                reply=(msg.content or "").strip()
                self.history.append(telegram_id,role.value.lower(),"ai",reply,{"tool_calls":tool_calls_log,"latency_ms":round((time.monotonic()-started)*1000)})
                return {"reply":reply,"tool_calls":tool_calls_log}
            messages.append({"role":"assistant","content":msg.content or "", "tool_calls":[
                {"id":c.id,"type":"function","function":{"name":c.function.name,"arguments":c.function.arguments}} for c in calls]})
            for call in calls:
                try:
                    args=json.loads(call.function.arguments or "{}")
                    result=await tools.execute(call.function.name,args)
                except Exception as exc:
                    result={"ok":False,"error":str(exc)}
                tool_calls_log.append({"name":call.function.name,"arguments":args if 'args' in locals() else {}, "result":result})
                self.history.append(telegram_id,role.value.lower(),"tool",call.function.name,{"arguments":args if 'args' in locals() else {}, "result":result})
                if result.get("requires_confirmation"):
                    self.history.set_pending(telegram_id,role.value.lower(),{"name":call.function.name,"args":result["action"]["args"]})
                    reply=result["summary"]+"\n\nПодтвердить? / Confirm?"
                    self.history.append(telegram_id,role.value.lower(),"ai",reply,{"confirmation_required":True})
                    return {"reply":reply,"confirmation_required":True,"tool_calls":tool_calls_log}
                messages.append({"role":"tool","tool_call_id":call.id,"content":json.dumps(result,ensure_ascii=False,default=str)[:12000]})
        raise RuntimeError("AI tool loop limit reached")
