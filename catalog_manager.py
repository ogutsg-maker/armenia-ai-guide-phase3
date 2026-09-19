from __future__ import annotations
import re
from platform_db import one, execute, proposal, review_proposal, active_session, update_session

def slugify(text):
    s=re.sub(r'[^\w\u0530-\u058F]+','-',str(text).strip().lower()).strip('-')
    return s or 'catalog-item'


def _materialize_proposal_services(p, category_id):
    """Recreate the services a partner submitted with a catalogue proposal.

    When persist_ready_application() cannot map the partner's direction it
    stores the whole profile (including the services list) in
    ai_catalog_proposals.payload_json and creates no service rows. On approval
    we must materialise those services against the freshly created category,
    otherwise the partner's services are silently lost.
    """
    import json as _json
    partner_id=p.get('partner_id')
    if not partner_id or not category_id:
        return 0
    payload=p.get('payload_json') or {}
    if isinstance(payload,str):
        try: payload=_json.loads(payload)
        except Exception: payload={}
    if not isinstance(payload,dict):
        return 0
    services=payload.get('services') or []
    description=str(payload.get('description') or '')[:1000]
    count=0
    for item in services:
        if not isinstance(item,dict):
            continue
        name=str(item.get('name') or '').strip()[:300]
        if not name:
            continue
        try: price=float(item.get('price')) if item.get('price') not in (None,'') else None
        except (TypeError,ValueError): price=None
        data_json=_json.dumps({'ai_source':True,'price_type':item.get('price_type') or 'unknown','from_proposal':True},ensure_ascii=False)
        existing=one("SELECT id FROM services WHERE partner_id=%s AND name=%s AND status<>'deleted' ORDER BY id DESC LIMIT 1",(partner_id,name))
        if existing:
            execute("UPDATE services SET category_id=%s,subcategory_id=NULL,price=%s,status='pending',data_json=%s::jsonb,updated_at=NOW() WHERE id=%s",(category_id,price,data_json,existing['id']))
        else:
            execute("INSERT INTO services(partner_id,category_id,subcategory_id,name,description,price,status,data_json) VALUES(%s,%s,NULL,%s,%s,%s,'pending',%s::jsonb)",(partner_id,category_id,name,description,price,data_json))
        count+=1
    return count

def activate_proposal(proposal_id, admin_id, data=None):
    p=proposal(proposal_id)
    if not p: raise ValueError('proposal_not_found')
    master=p.get('proposed_master_category') or 'Նոր ուղղություն'
    category=p.get('proposed_category') or 'Նոր կատեգորիա'
    sub=p.get('proposed_subcategory') or None
    service=p.get('proposed_service') or None
    # Prefer an existing direction/category with the same normalized name.
    m=one('SELECT * FROM master_categories WHERE LOWER(name_am)=LOWER(%s) OR LOWER(name_ru)=LOWER(%s) LIMIT 1',(master,master))
    if not m:
        m=execute('INSERT INTO master_categories(name_am,name_ru,slug,is_active) VALUES(%s,%s,%s,TRUE) RETURNING *',(master,master,slugify(master)),True)
    c=one('SELECT * FROM categories WHERE master_category_id=%s AND (LOWER(name_am)=LOWER(%s) OR LOWER(name_ru)=LOWER(%s)) LIMIT 1',(m['id'],category,category))
    if not c:
        # Subcategory tariff stays NULL => inherits the direction default.
        c=execute('INSERT INTO categories(master_category_id,name_am,name_ru,slug,is_active) VALUES(%s,%s,%s,%s,TRUE) RETURNING *',(m['id'],category,category,slugify(master+'-'+category)),True)
    # 3-level catalog: master_categories -> categories -> services.
    # A finer "subcategory" proposed by the AI, if any, is kept on the
    # proposal payload (not a separate taxonomy table).
    subrow=None
    if p.get('partner_id'):
        pd=execute("INSERT INTO partner_directions(partner_id,master_category_id,status) VALUES(%s,%s,'pending') ON CONFLICT(partner_id,master_category_id) DO UPDATE SET updated_at=NOW() RETURNING id",(p['partner_id'],m['id']),True)
        if pd:
            execute("INSERT INTO partner_direction_categories(partner_direction_id,category_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(pd['id'],c['id']))
            # Attach a document uploaded before proposal activation to the new direction.
            execute("UPDATE partner_verification_documents SET partner_direction_id=%s WHERE id=(SELECT id FROM partner_verification_documents WHERE partner_id=%s AND partner_direction_id IS NULL AND status='pending' ORDER BY created_at DESC LIMIT 1)",(pd['id'],p['partner_id']))
            # Materialise the services the partner submitted with the proposal.
            # persist_ready_application() stored the FULL profile in
            # payload_json when it could not map the direction, so approving
            # the new direction here must recreate those services — otherwise
            # they would be lost in the "new direction (proposal)" branch.
            _materialize_proposal_services(p, c['id'])
            session=active_session(one('SELECT user_id FROM partners WHERE id=%s',(p['partner_id'],))['user_id'],'partner','onboarding')
            if session:
                ctx=session.get('context_json') or {}
                if isinstance(ctx,str):
                    try: ctx=__import__('json').loads(ctx)
                    except Exception: ctx={}
                ctx['direction_id']=pd['id'];ctx['proposal_id']=proposal_id;ctx['awaiting_document']=True;update_session(session['id'],ctx)
    review_proposal(proposal_id,'approved',admin_id,str((data or {}).get('comment') or ''))
    return {'proposal_id':proposal_id,'master_category':m,'category':c,'subcategory':subrow,'service':service}
