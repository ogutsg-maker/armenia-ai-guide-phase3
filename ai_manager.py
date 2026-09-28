"""Unified AI entry point for REGISTRATION/CLIENT/PARTNER/ADMIN."""
from __future__ import annotations
import asyncio, json, logging, os, time
from typing import Any
from groq import AsyncGroq
import platform_db
import ai_cost_center
from history_provider import HistoryProvider
from prompt_factory import ContextType, as_context_type, PromptFactory
from tool_registry import ToolRegistry

logger=logging.getLogger(__name__)

class AIContext(Enum):
    REGISTRATION = "registration"
    CLIENT = "client"
    PARTNER = "partner"
    ADMIN = "admin"


class AIManager:
    def __init__(self, db_pool=None, *, history_provider: HistoryProvider|None=None, model: str|None=None, max_history: int = 10, max_tool_rounds: int = 4):
        key=os.getenv("GROQ_API_KEY","").strip()
        if not key: raise RuntimeError("GROQ_API_KEY is not configured")
        self.client=AsyncGroq(api_key=key)
        self.db=db_pool
        self.model=model or os.getenv("GROQ_MODEL","openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
        self.history=history_provider or HistoryProvider()
        self.max_history=max(2,min(int(max_history),50))
        self.max_tool_rounds=max(1,min(int(max_tool_rounds),8))

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

    async def _supabase_history(self, telegram_id:int, context:ContextType, limit:int|None=None):
        role=context.value.lower()
        size=max(2,min(int(limit or self.max_history),50))
        def _read():
            session=platform_db.active_session(int(telegram_id),role,"ai_manager") or platform_db.create_session(int(telegram_id),role,"ai_manager",{"history_version":1})
            rows=platform_db.recent_ai_messages(int(session["id"]),size)
            return [{"role":"assistant" if str(x.get("sender_role") or "").lower() in {"ai","assistant"} else "user","content":str(x.get("message_text") or x.get("text") or "")} for x in rows if str(x.get("sender_role") or "").lower()!="tool"]
        return await asyncio.to_thread(_read)

    async def _save_supabase_history(self, telegram_id:int, context:ContextType, sender:str, content:str, metadata:dict|None=None):
        def _write():
            session=platform_db.active_session(int(telegram_id),context.value.lower(),"ai_manager") or platform_db.create_session(int(telegram_id),context.value.lower(),"ai_manager",{"history_version":1})
            return platform_db.add_ai_message(int(session["id"]),sender,str(content or "")[:12000],metadata or {})
        return await asyncio.to_thread(_write)

    async def _cost_log(self, telegram_id:int, context:ContextType, usage, elapsed:float, *, status="success", error="", extra_context=None):
        data=usage.model_dump() if hasattr(usage,"model_dump") else (usage if isinstance(usage,dict) else {})
        partner_id=(extra_context or {}).get("partner_id")
        try: partner_id=int(partner_id) if partner_id is not None else None
        except (TypeError,ValueError): partner_id=None
        return await asyncio.to_thread(ai_cost_center.record_usage,
            provider="groq",model=self.model,chain=context.value.lower(),stage="manager",
            operation="chat",purpose="Unified AIManager",user_id=int(telegram_id),
            partner_id=partner_id,input_tokens=int(data.get("prompt_tokens") or data.get("input_tokens") or 0),
            output_tokens=int(data.get("completion_tokens") or data.get("output_tokens") or 0),
            cached_tokens=int(data.get("prompt_cached_tokens") or data.get("cached_tokens") or 0),
            reasoning_tokens=int(data.get("reasoning_tokens") or 0),status=status,error=error)

    async def chat_json(self, telegram_id:int, context_type:ContextType|str, message:str, *, task_instructions:str, extra_context:dict[str,Any]|None=None, language:str|None=None, max_tokens:int=1800)->dict[str,Any]:
        """Structured JSON completion through the same unified manager."""
        role=as_context_type(context_type)
        language=language or self._lang(message)
        history=await self._supabase_history(telegram_id,role,self.max_history)
        prompt=PromptFactory.build(role,message=message,history=history,
            trusted_context=self._trusted(role,telegram_id,extra_context),
            language=language,task_instructions=task_instructions)
        started=time.monotonic()
        await self._save_supabase_history(telegram_id,role,"user",message,{"context_type":role.value,"structured":True})
        kwargs={
            "model":self.model,
            "messages":[{"role":"user","content":prompt}],
            "temperature":0,
            "max_tokens":max_tokens,
        }
        # gpt-oss on Groq is more reliable with prompt-constrained JSON than
        # response_format=json_object.
        if "gpt-oss" not in self.model.lower():
            kwargs["response_format"]={"type":"json_object"}
        response=await self.client.chat.completions.create(**kwargs)
        usage=getattr(response,"usage",None)
        ai_cost_center.record_usage(provider="groq",model=self.model,
            chain=role.value.lower(),stage="manager",operation="json",
            purpose="Unified AIManager structured extraction",user_id=telegram_id,
            input_tokens=int(getattr(usage,"prompt_tokens",getattr(usage,"input_tokens",0)) or 0),
            output_tokens=int(getattr(usage,"completion_tokens",getattr(usage,"output_tokens",0)) or 0),
            cached_tokens=0,reasoning_tokens=0)
        raw=(response.choices[0].message.content or "{}").strip()
        try:
            parsed=json.loads(raw)
        except Exception as exc:
            raise RuntimeError("AIManager returned invalid JSON") from exc
        await self._save_supabase_history(telegram_id,role,"ai",raw,{"structured":True,"latency_ms":round((time.monotonic()-started)*1000)})
        return parsed

    async def chat(self, telegram_id:int, context_type:ContextType|str, message:str, *, extra_context:dict[str,Any]|None=None, language:str|None=None)->dict[str,Any]:
        role=as_context_type(context_type)
        language=language or self._lang(message)
        tools=ToolRegistry(telegram_id=telegram_id,context_type=role,trusted_context=extra_context)
        history=await self._supabase_history(telegram_id,role,self.max_history)
        prompt=PromptFactory.build(role,message=message,history=history,trusted_context=self._trusted(role,telegram_id,extra_context),language=language)
        messages=[{"role":"user","content":prompt}]
        started=time.monotonic()
        await self._save_supabase_history(telegram_id,role,"user",message,{"context_type":role.value})
        pending=await self._get_pending(telegram_id,role)
        if pending and message.strip().casefold() in {"yes","да","այո","հա","հաստատել","հաստատում եմ","confirm","ok"}:
            result=await tools.execute_confirmed(pending["name"],pending["args"])
            await self._clear_pending(telegram_id,role)
            reply="Կատարված է։" if language=="hy" else ("Готово." if language=="ru" else "Done.")
            await self._save_supabase_history(telegram_id,role,"ai",reply,{"confirmed_action":result})
            return {"reply":reply,"tool_result":result,"confirmed":True}

        tool_calls_log=[]
        for _ in range(self.max_tool_rounds):
            response=await self.client.chat.completions.create(
                model=self.model,messages=messages,tools=tools.definitions() or None,
                tool_choice="auto" if tools.definitions() else None,
                temperature=0.1,max_tokens=1600,
            )
            usage=getattr(response,"usage",None)
            await self._cost_log(telegram_id,role,usage,time.monotonic()-started,extra_context=extra_context)
            msg=response.choices[0].message
            calls=getattr(msg,"tool_calls",None) or []
            if not calls:
                reply=(msg.content or "").strip()
                await self._save_supabase_history(telegram_id,role,"ai",reply,{"tool_calls":tool_calls_log,"latency_ms":round((time.monotonic()-started)*1000)})
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
                await self._save_supabase_history(telegram_id,role,"tool",call.function.name,{"arguments":args if 'args' in locals() else {}, "result":result})
                if result.get("requires_confirmation"):
                    await self._set_pending(telegram_id,role,{"name":call.function.name,"args":result["action"]["args"]})
                    reply=result["summary"]+"\n\nПодтвердить? / Confirm?"
                    await self._save_supabase_history(telegram_id,role,"ai",reply,{"confirmation_required":True})
                    return {"reply":reply,"confirmation_required":True,"tool_calls":tool_calls_log}
                messages.append({"role":"tool","tool_call_id":call.id,"content":json.dumps(result,ensure_ascii=False,default=str)[:12000]})
        raise RuntimeError("AI tool loop limit reached

    async def _set_pending(self, telegram_id:int, context:ContextType, pending:dict):
        def _write():
            session=platform_db.active_session(int(telegram_id),context.value.lower(),"ai_manager")
            if not session:
                session=platform_db.create_session(int(telegram_id),context.value.lower(),"ai_manager",{"history_version":1})
            value=session.get("context_json") or {}
            if isinstance(value,str): value=json.loads(value)
            value["ai_manager_pending_action"]=pending
            return platform_db.update_session(int(session["id"]),value)
        return await asyncio.to_thread(_write)

    async def _clear_pending(self, telegram_id:int, context:ContextType):
        return await self._set_pending(telegram_id,context,{}) if False else await asyncio.to_thread(self._clear_pending_sync,telegram_id,context)

    def _clear_pending_sync(self, telegram_id:int, context:ContextType):
        session=platform_db.active_session(int(telegram_id),context.value.lower(),"ai_manager")
        if not session: return None
        value=session.get("context_json") or {}
        if isinstance(value,str): value=json.loads(value)
        value.pop("ai_manager_pending_action",None)
        return platform_db.update_session(int(session["id"]),value)

    async def _get_pending(self, telegram_id:int, context:ContextType):
        def _read():
            session=platform_db.active_session(int(telegram_id),context.value.lower(),"ai_manager")
            if not session: return None
            value=session.get("context_json") or {}
            if isinstance(value,str): value=json.loads(value)
            return value.get("ai_manager_pending_action")
        return await asyncio.to_thread(_read)

    @staticmethod
    def _is_confirmation(message:str)->bool:
        return " ".join((message or "").strip().casefold().split()) in {"yes","да","подтверждаю","подтвердить","confirm","ok","այո","հա","հաստատում եմ","հաստատել"}

")
