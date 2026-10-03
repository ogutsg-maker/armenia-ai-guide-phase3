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
    service = {'company_id':state['companies'][0]['id'],'name':str(data.get('name','')).strip(),'price_type':str(data.get('price_type','')).lower(),'price_amd':data.get('price_amd'),'hours':data.get('hours'),'at_client':bool(data.get('at_client',False)),'territory':data.get('territory'),'address_id':None,'internal_phone':data.get('internal_phone')}
    if not service['name'] or service['price_type'] not in ('fixed','from') or service['price_amd'] is None: raise ValueError('service_data_incomplete')
    save(uid,'PARTNER',{'action':'create_service','service':service})
    return {'kind':'preview','preview':service}
