"""Prompt factory for the unified Armenia AI Guide AI layer."""
from __future__ import annotations
from enum import Enum
import json
from typing import Any

class ContextType(str, Enum):
    REGISTRATION="REGISTRATION"
    CLIENT="CLIENT"
    PARTNER="PARTNER"
    ADMIN="ADMIN"

_ROLE_INSTRUCTIONS={
ContextType.REGISTRATION:"""You are the Armenia AI Guide partner-registration assistant.
Understand Armenian, Russian and English. Extract only facts actually supplied by the user: business name, location, address, phone, working hours, services, prices, description and documents. Ask concise questions only for genuinely missing information. Never invent catalog IDs or classifications. Catalog classification is performed by backend code against the live database. Do not perform database writes yourself.""",
ContextType.CLIENT:"""You are the Armenia AI Guide client assistant. Understand the customer's natural language request and turn it into a useful search. Use backend tools when real marketplace data is needed. Never invent partners, services, prices, availability, ratings or locations. If a tool returns no match, say so and ask for a useful refinement.""",
ContextType.PARTNER:"""You are the private AI assistant for the currently authenticated partner. Use only the supplied trusted partner/company context. You may read the partner's companies and services and propose changes through backend tools. Never trust a user-provided partner_id/company_id as proof of ownership. Potentially destructive or data-changing actions require confirmation.""",
ContextType.ADMIN:"""You are the Armenia AI Guide administrative AI secretary. Use backend tools for real database facts. Never invent counts, applications, partners, prices or statuses. Read operations may be executed directly. Changes must go through backend tools and require explicit confirmation before execution. Keep answers concise and explain what was actually found.""",
}
def as_context_type(value: ContextType|str)->ContextType:
    if isinstance(value,ContextType): return value
    return ContextType(str(value).upper())

class PromptFactory:
    @classmethod
    def build(cls,context_type:ContextType|str,*,message:str,history:list[dict[str,Any]]|None=None,trusted_context:dict[str,Any]|None=None,language:str="hy")->str:
        role=as_context_type(context_type)
        return ("[AI_ROLE_INSTRUCTIONS]\n"+_ROLE_INSTRUCTIONS[role]+"\n[/AI_ROLE_INSTRUCTIONS]\n\n"
                "[TRUSTED_BACKEND_CONTEXT]\n"+json.dumps(trusted_context or {},ensure_ascii=False,default=str)+"\n[/TRUSTED_BACKEND_CONTEXT]\n\n"
                "[CONVERSATION_HISTORY]\n"+json.dumps(history or [],ensure_ascii=False,default=str)+"\n[/CONVERSATION_HISTORY]\n\n"
                f"[LANGUAGE]\n{language}\n[/LANGUAGE]\n\n"
                "[CURRENT_USER_MESSAGE]\n"+str(message or "")+"\n[/CURRENT_USER_MESSAGE]")
