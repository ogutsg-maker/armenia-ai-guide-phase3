import json
from .core import chat
from .prompts import prompt
from .session import save
from .tools import partner_context
from db import exec

def parse_json(value):
    text = (value or '').strip()
    if text.startswith('```'):
        text = text.replace('```json', '', 1).replace('```', '').strip()
    return json.loads(text)

async def turn(uid, context, text):
    context_data = partner_context(uid) if context == 'PARTNER' else None
    result = await chat(prompt(context, text, context_data))
    exec('INSERT INTO aig_ai_costs(telegram_id,provider,model,operation,input_tokens,output_tokens) VALUES(%s,%s,%s,%s,%s,%s)', (uid,result['provider'],result['model'],context,result['input_tokens'],result['output_tokens']))
    return result

async def partner_service_preview(uid, text):
    state = partner_context(uid)
    if not state['partner']: raise ValueError('partner_registration_required')
    if len(state['companies']) != 1: return {'kind':'select_company','companies':state['companies']}
    result = await turn(uid, 'PARTNER', text)
    data = parse_json(result['text'])
    if data.get('action') != 'create_service': return {'kind':'message','answer':data.get('answer','')}
    raw_type = str(data.get('price_type','')).strip().lower()
    if raw_type in ('from','starting','starting_from','от','от_цены','սկսած','դրամից'):
        price_type = 'from'
    elif raw_type in ('fixed','exact','фиксированная','фиксированный','ֆիքսված'):
        price_type = 'fixed'
    else:
        lower_text = str(text).lower()
        price_type = 'from' if any(x in lower_text for x in (' от ', 'от ', 'սկսած', 'դրամից', 'from ')) else ''
    raw_price = data.get('price_amd')
    if isinstance(raw_price, str):
        import re as _re
        m = _re.search(r'\d+(?:[.,]\d+)?', raw_price.replace(' ', ''))
        raw_price = m.group(0).replace(',', '.') if m else None
    try:
        price_amd = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        price_amd = None
    service = {'company_id':state['companies'][0]['id'],'name':str(data.get('name','')).strip(),'price_type':price_type,'price_amd':price_amd,'hours':data.get('hours'),'at_client':bool(data.get('at_client',False)),'territory':data.get('territory'),'address_id':None,'internal_phone':data.get('internal_phone')}
    if not service['name'] or service['price_type'] not in ('fixed','from') or service['price_amd'] is None: raise ValueError('service_data_incomplete')
    save(uid,'PARTNER',{'action':'create_service','service':service})
    return {'kind':'preview','preview':service}
