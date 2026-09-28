from __future__ import annotations
import json
from data_core import create_session, active_session, add_ai_message, update_session, search_catalog, create_service_request, update_service_request, search_services, replace_request_candidates

class ClientAI:
    def __init__(self, ai): self.ai = ai

    async def process(self, user_id, text, lang='hy'):
        session = active_session(user_id, 'client', 'sales') or create_session(user_id, 'client', 'sales', {'request_id': None, 'stage': 'discovery', 'asked_questions': []})
        ctx = session.get('context_json') or {}
        if isinstance(ctx, str):
            try: ctx = json.loads(ctx)
            except Exception: ctx = {}
        add_ai_message(session['id'], 'user', text)
        # Unified AIManager extracts only the customer's natural-language
        # request. It does not receive catalog IDs. Category resolution is
        # performed afterwards by the backend live-catalog classifier.
        from ai_manager import AIManager
        from prompt_factory import ContextType
        from ai_service import RequestAnalysis
        from catalog_classifier import classify_services_batch

        manager = AIManager()
        task = """Return ONLY JSON with these keys:
language,intent,service,location,marz,city,village,budget_min,budget_max,date,time,
requirements,missing_fields,checklist,summary,confidence.
Do not invent partners, prices, availability or catalog IDs.
Extract the requested service phrase exactly enough for backend catalog matching.
Prices are AMD. checklist maximum 3 short useful questions."""
        try:
            data = await manager.chat_json(
                int(user_id), ContextType.CLIENT, text,
                task_instructions=task,
                extra_context={"request_id": ctx.get("request_id")},
                language=lang, max_tokens=700,
            )
        except Exception:
            data = {}

        def _int(v):
            try: return int(v) if v is not None else None
            except Exception: return None
        def _float(v):
            try: return float(v) if v is not None else None
            except Exception: return None

        service_phrase = str(data.get("service") or "").strip()
        classified = await classify_services_batch(
            data_core, [service_phrase], telegram_id=int(user_id)
        ) if service_phrase else []
        mapped = classified[0] if classified else {}
        analysis = RequestAnalysis(
            language=data.get("language") if data.get("language") in {"hy","ru","en"} else lang,
            intent=str(data.get("intent") or "service_search"),
            master_category_id=_int(mapped.get("direction_id")),
            category_id=_int(mapped.get("subcategory_id")),
            category_name=mapped.get("subcategory_name_am") or mapped.get("subcategory_name_ru"),
            service=service_phrase or None,
            location=data.get("location"), marz=data.get("marz"), city=data.get("city"),
            village=data.get("village"), budget_min=_float(data.get("budget_min")),
            budget_max=_float(data.get("budget_max")), date=data.get("date"),
            time=data.get("time"),
            requirements=[str(x) for x in (data.get("requirements") or []) if x],
            missing_fields=[str(x) for x in (data.get("missing_fields") or []) if x],
            checklist=[str(x) for x in (data.get("checklist") or []) if x][:3],
            summary=str(data.get("summary") or text[:500]),
            confidence=max(0.0,min(1.0,_float(data.get("confidence")) or 0.0)),
        )
        location = analysis.city or analysis.village or analysis.marz or analysis.location
        if not ctx.get('request_id'):
            req = create_service_request(user_id, analysis.category_id, 'searching', analysis.language, location, analysis.summary, analysis.model_dump())
            ctx['request_id'] = req['id'] if req else None
        else:
            update_service_request(ctx['request_id'], analysis.category_id, 'searching', analysis.summary, location, analysis.model_dump())
        candidates = self._find_candidates(analysis)
        if ctx.get('request_id'):
            replace_request_candidates(ctx['request_id'], candidates, self._match_reason(analysis,lang))
        ctx['last_analysis'] = analysis.model_dump(); ctx['stage'] = 'options_found' if candidates else 'clarifying'
        reply = self._options_reply(candidates, lang) if candidates else self._clarify(ctx, analysis, lang)
        update_session(session['id'], ctx)
        add_ai_message(session['id'], 'ai', reply, {'analysis': analysis.model_dump(), 'candidates': candidates})
        return reply

    def _find_candidates(self, analysis):
        location = analysis.city or analysis.village or analysis.marz or analysis.location
        try:
            return search_services(
                category_id=analysis.category_id,
                city=location or "",
                max_price=analysis.budget_max,
                limit=3,
            )
        except Exception:
            return []

    def _match_reason(self,analysis,lang):
        if analysis.budget_max is not None: return {'hy':'Համապատասխանում է ծառայության և բյուջեի պահանջներին։','ru':'Соответствует услуге и указанному бюджету.','en':'Matches the requested service and budget.'}.get(lang,'Matches your request.')
        return {'hy':'Համապատասխանում է պահանջվող ծառայությանը։','ru':'Соответствует запрошенной услуге.','en':'Matches the requested service.'}.get(lang,'Matches your request.')

    def _options_reply(self,candidates,lang):
        head={'hy':'Գտա մինչև 3 իրական, հաստատված գործընկերային տարբերակ։ Կոնտակտները դեռ փակ են։','ru':'Нашёл до 3 реальных одобренных партнёров. Контакты пока скрыты.','en':'I found up to 3 real approved partners. Contacts stay hidden for now.'}.get(lang,'I found real approved partners.')
        lines=[]
        for i,c in enumerate(candidates,1):
            price=f" — {float(c['price']):,.0f} ֏" if c.get('price') is not None else ''
            lines.append(f"{i}. {c.get('business_name') or 'Partner'} — {c.get('service_name') or ''}{price}")
        tail={'hy':'Ընտրեք տարբերակը կամ գրեք՝ ինչ եք ուզում փոխել։','ru':'Выберите вариант или напишите, что изменить.','en':'Choose an option or tell me what to change.'}.get(lang,'Choose an option or tell me what to change.')
        return head+'\n\n'+'\n'.join(lines)+'\n\n'+tail

    def _clarify(self,ctx,analysis,lang):
        asked=ctx.get('asked_questions') or []; fresh=[q for q in (analysis.checklist or []) if q and q not in asked][:2]
        ctx['asked_questions']=(asked+fresh)[:12]
        if fresh:
            intro={'hy':'Հասկացա։ Եվս մի քանի մանրուք ճշտեմ, որպեսզի ճիշտ գործընկերներ գտնեմ։','ru':'Понял. Уточню ещё несколько деталей, чтобы найти подходящих партнёров.','en':'Understood. I need a couple more details to find the right partners.'}.get(lang,'I need a couple more details.')
            return intro+'\n\n'+'\n'.join('• '+q for q in fresh)
        return {'hy':'Այս պահին հաստատված համապատասխան գործընկեր չգտա։ Գրեք քաղաքը, բյուջեն կամ ծառայության ավելի կոնկրետ անվանումը։','ru':'Пока не нашёл одобренного подходящего партнёра. Укажите город, бюджет или более точное название услуги.','en':'I did not find an approved matching partner yet. Add a city, budget, or a more specific service.'}.get(lang,'I did not find an approved match yet.')