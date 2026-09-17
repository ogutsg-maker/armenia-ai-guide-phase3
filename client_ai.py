from __future__ import annotations
import json
from platform_db import create_session, active_session, add_ai_message, update_session, execute, rows

class ClientAI:
    def __init__(self, ai): self.ai = ai

    async def process(self, user_id, text, lang='hy'):
        session = active_session(user_id, 'client', 'sales') or create_session(user_id, 'client', 'sales', {'request_id': None, 'stage': 'discovery', 'asked_questions': []})
        ctx = session.get('context_json') or {}
        if isinstance(ctx, str):
            try: ctx = json.loads(ctx)
            except Exception: ctx = {}
        add_ai_message(session['id'], 'user', text)
        cats = rows("""SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug
                      FROM categories c JOIN master_categories m ON m.id=c.master_category_id
                      WHERE c.is_active=TRUE AND COALESCE(m.is_active,TRUE)=TRUE ORDER BY c.id""")
        analysis = await self.ai.analyze_request(text, cats)
        location = analysis.city or analysis.village or analysis.marz or analysis.location
        if not ctx.get('request_id'):
            req = execute('INSERT INTO service_requests(client_id,category_id,status,language,city,summary,preferences_json) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *', (user_id, analysis.category_id, 'searching', analysis.language, location, analysis.summary, json.dumps(analysis.model_dump(), ensure_ascii=False)), True)
            ctx['request_id'] = req['id'] if req else None
        else:
            execute("UPDATE service_requests SET category_id=%s,status='searching',summary=%s,city=%s,preferences_json=%s::jsonb,updated_at=NOW() WHERE id=%s", (analysis.category_id, analysis.summary, location, json.dumps(analysis.model_dump(), ensure_ascii=False), ctx['request_id']))
        candidates = self._find_candidates(analysis)
        if ctx.get('request_id'):
            execute('DELETE FROM request_candidates WHERE request_id=%s', (ctx['request_id'],))
            for rank,c in enumerate(candidates,1):
                execute('INSERT INTO request_candidates(request_id,partner_id,service_id,rank_score,match_reason) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING', (ctx['request_id'],c['partner_id'],c['service_id'],100-rank*5,self._match_reason(analysis,lang)))
        ctx['last_analysis'] = analysis.model_dump(); ctx['stage'] = 'options_found' if candidates else 'clarifying'
        reply = self._options_reply(candidates, lang) if candidates else self._clarify(ctx, analysis, lang)
        update_session(session['id'], ctx)
        add_ai_message(session['id'], 'ai', reply, {'analysis': analysis.model_dump(), 'candidates': candidates})
        return reply

    def _find_candidates(self, analysis):
        clauses=["s.status='approved'","c.is_active=TRUE","pd.status='approved'","p.status='approved'"]; params=[]
        if analysis.category_id: clauses.append('s.category_id=%s'); params.append(analysis.category_id)
        if analysis.budget_max is not None: clauses.append('(s.price IS NULL OR s.price<=%s)'); params.append(analysis.budget_max)
        if analysis.budget_min is not None: clauses.append('(s.price IS NULL OR s.price>=%s)'); params.append(analysis.budget_min)
        location=analysis.city or analysis.village or analysis.marz or analysis.location
        if location:
            clauses.append("EXISTS (SELECT 1 FROM partner_locations pl WHERE pl.partner_id=p.id AND pl.is_active=TRUE AND (LOWER(COALESCE(pl.city,''))=LOWER(%s) OR LOWER(COALESCE(pl.village,''))=LOWER(%s) OR LOWER(COALESCE(pl.marz,''))=LOWER(%s) OR COALESCE(pl.is_all_armenia,FALSE)=TRUE))")
            params += [location,location,location]
        sql=f'''SELECT DISTINCT s.id service_id,s.partner_id,s.name service_name,s.price,s.duration_minutes,c.id category_id,p.business_name
                FROM services s JOIN categories c ON c.id=s.category_id JOIN partners p ON p.id=s.partner_id
                JOIN partner_directions pd ON pd.partner_id=p.id AND pd.status='approved'
                WHERE {' AND '.join(clauses)} ORDER BY CASE WHEN s.price IS NULL THEN 1 ELSE 0 END,s.created_at DESC LIMIT 3'''
        try: return rows(sql,tuple(params))
        except Exception: return []

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
