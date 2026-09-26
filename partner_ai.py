"""AI-first partner onboarding and catalog structuring."""
from __future__ import annotations
import json
import logging
from platform_db import (
    active_session, create_session, update_session, add_ai_message,
    recent_ai_messages, catalog_tree, ensure_partner, update_partner,
    upsert_direction, create_or_update_proposal, proposal, latest_clarification, mark_clarification_answered,
)

logger=logging.getLogger(__name__)


def _catalog_text(tree):
    lines=[]
    for r in tree:
        lines.append(f"master_id={r['master_category_id']} | {r['master_name_am']} / {r['master_name_ru']} | category_id={r.get('category_id')} | {r.get('category_name_am')} / {r.get('category_name_ru')}")
    return '\n'.join(lines[:500])

class PartnerAI:
    def __init__(self, ai):
        self.ai=ai

    async def process(self, user_id:int, text:str, lang:str='hy') -> dict:
        partner=ensure_partner(user_id)
        session=active_session(user_id,'partner','onboarding') or create_session(user_id,'partner','onboarding',{
            'partner_id':partner['id'],'profile':{},'direction_id':None,'proposal_id':None,'confirmed':False,'awaiting_document':False
        })
        ctx=session.get('context_json') or {}
        if isinstance(ctx,str):
            try: ctx=json.loads(ctx)
            except Exception: ctx={}
        ctx.setdefault('partner_id',partner['id']); ctx.setdefault('profile',{})
        add_ai_message(session['id'],'user',text)
        history=recent_ai_messages(session['id'],14)
        clarification=latest_clarification(partner['id'])
        catalog=_catalog_text(catalog_tree())
        system=f'''Դու Armenia AI Guide-ի գործընկերոջ անձնական AI օգնականն ես։
Դու չես ստիպում գործընկերոջը լրացնել ձևեր։ Գործընկերը խոսում է բնական լեզվով, իսկ դու նրա խոսքը վերածում ես կառուցվածքային տվյալների։
Լեզուն՝ {lang}. Պատասխանիր նույն լեզվով, հնարավորինս բնական և կարճ։

Քո կանոնները.
1) Նախ օգտագործիր արդեն գոյություն ունեցող կատալոգը։
2) Եթե համապատասխան ուղղություն/կատեգորիա/ենթակատեգորիա չկա, երբեք մի ասա «մեզ մոտ չկա»։ Առաջարկիր նոր կատալոգային կառուցվածք և նշիր catalog_proposal. Ակտիվ կատալոգը փոխում է միայն ադմինը։
3) Եթե ադմինը վերադարձրել է ճշտման հարց, փոխանցիր այն գործընկերոջը բնական լեզվով և օգտագործիր նրա պատասխանը proposal-ը թարմացնելու համար։
4) Գործընկերոջ հաստատումից հետո ստեղծիր/թարմացրու draft/pending ուղղությունը։
5) Փաստաթուղթ պահանջելու ժամանակ հստակ ասա, որ պետք է սեղմել «Ներբեռնել փաստաթուղթ» և ուղարկել փաստաթուղթը։
6) Մի հայտարարիր հաստատված ուղղություն կամ հաստատված գործընկեր, եթե ադմինը դա չի արել։
7) Գործընկերոջ տվյալները կարող են գալ տեքստով, PDF/ֆոտոյով կամ գներով։

Կատալոգը.
{catalog}

Ընթացիկ կառուցված տվյալները.
{json.dumps(ctx.get('profile',{}),ensure_ascii=False)}

Վերջին հաղորդագրությունները.
{json.dumps(history,ensure_ascii=False)}

Պարտադիր JSON:
{{
  "reply":"...",
  "profile_patch":{{"business_name":"","business_description":"","location":"","working_hours":"","services":[],"prices":[],"staff":[],"packages":[]}},
  "catalog_match":{{"master_category_id":null,"category_ids":[]}},
  "catalog_proposal":{{"needed":false,"master_category":"","category":"","subcategory":"","service":"","reason":"","description":""}},
  "confirmed":false,
  "needs_document":false,
  "next_step":"profile|catalog|confirmation|document|waiting_admin|done"
}}'''
        try:
            data=await self.ai.chat_json(
                system, text, max_tokens=1200,
                chain="partner_registration", stage="dialogue",
                operation="partner_profile_extraction",
                purpose="Extract business profile, services, prices and catalog mapping",
                user_id=user_id, partner_id=int(partner['id']),
            )
        except Exception as exc:
            logger.exception('Partner AI failed')
            data={'reply':'Ես այստեղ եմ։ Խնդրում եմ պատմեք ձեր բիզնեսի մասին՝ ինչ ծառայություններ եք առաջարկում, որտեղ եք աշխատում և ինչ գներով։','profile_patch':{},'catalog_match':{},'catalog_proposal':{'needed':False},'confirmed':False,'needs_document':False,'next_step':'profile'}
        patch=data.get('profile_patch') or {}
        ctx['profile'].update({k:v for k,v in patch.items() if v not in ('',None,[])})
        match=data.get('catalog_match') or {}
        proposal_data=data.get('catalog_proposal') or {}
        proposal_id=ctx.get('proposal_id')
        if proposal_data.get('needed'):
            p=create_or_update_proposal(partner['id'],proposal_data,proposal_id)
            proposal_id=p['id'];ctx['proposal_id']=proposal_id;ctx['awaiting_document']=False
        elif proposal_id and data.get('confirmed'):
            # Admin still has to approve a catalog proposal; confirmation only means the partner agrees with the draft.
            pass
        master_id=match.get('master_category_id')
        category_ids=[int(x) for x in (match.get('category_ids') or []) if str(x).isdigit()]
        if master_id:
            try:
                direction=upsert_direction(partner['id'],int(master_id),category_ids,'pending' if data.get('confirmed') else 'draft')
                ctx['direction_id']=direction['id']
                update_partner(partner['id'],business_name=ctx['profile'].get('business_name') or partner.get('business_name') or f'Գործընկեր {partner["id"]}',business_description=ctx['profile'].get('business_description') or partner.get('business_description') or '',profile_json=ctx['profile'],status='pending',verification_status='not_submitted')
            except Exception:
                logger.exception('Failed to persist partner direction')
        if data.get('confirmed') and ctx.get('direction_id') and not proposal_data.get('needed'):
            ctx['awaiting_document']=True
        if clarification:
            ctx['last_admin_clarification_id']=clarification['id']
            mark_clarification_answered(clarification['id'])
        update_session(session['id'],ctx)
        add_ai_message(session['id'],'ai',str(data.get('reply') or ''),data)
        return {'reply':str(data.get('reply') or ''),'context':ctx,'raw':data}

    async def initial_message(self,user_id:int,lang:str='hy') -> str:
        p=ensure_partner(user_id)
        s=active_session(user_id,'partner','onboarding') or create_session(user_id,'partner','onboarding',{'partner_id':p['id'],'profile':{}})
        text={'hy':'Բարև 👋 Ես ձեր AI օգնականն եմ։ Ես կօգնեմ ձեր բիզնեսը միացնել Armenia AI Guide-ին՝ առանց բարդ ձևերի։ Պատմեք ձեր բիզնեսի մասին՝ ինչ եք անում, որտեղ եք աշխատում և ինչ ծառայություններ եք առաջարկում։ Կարող եք գրել ազատ ձևով կամ ուղարկել PDF/լուսանկար/գնացուցակ։','ru':'Здравствуйте 👋 Я ваш AI-помощник. Я помогу подключить бизнес к Armenia AI Guide без сложных анкет. Расскажите о бизнесе свободным текстом: чем занимаетесь, где работаете, какие услуги и цены. Можно прислать PDF, фото или прайс-лист.','en':'Hello 👋 I am your AI assistant. I will connect your business to Armenia AI Guide without complicated forms. Tell me about your business, location, services and prices. You can also send a PDF, photo or price list.'}[lang]
        add_ai_message(s['id'],'ai',text)
        return text
