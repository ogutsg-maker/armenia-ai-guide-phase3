from __future__ import annotations
import re
from platform_db import one, execute, proposal, review_proposal, active_session, update_session

def slugify(text):
    s=re.sub(r'[^\w\u0530-\u058F]+','-',str(text).strip().lower()).strip('-')
    return s or 'catalog-item'

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
        c=execute('INSERT INTO categories(master_category_id,name_am,name_ru,slug,is_active,commission_type,commission_value) VALUES(%s,%s,%s,%s,TRUE,\'on_top\',10) RETURNING *',(m['id'],category,category,slugify(master+'-'+category)),True)
    subrow=None
    if sub:
        subrow=one('SELECT * FROM catalog_subcategories WHERE category_id=%s AND LOWER(name_ru)=LOWER(%s) LIMIT 1',(c['id'],sub))
        if not subrow:
            subrow=execute('INSERT INTO catalog_subcategories(category_id,name_am,name_ru,name_en,slug,is_active) VALUES(%s,%s,%s,%s,%s,TRUE) RETURNING *',(c['id'],sub,sub,sub,slugify(category+'-'+sub)),True)
    if p.get('partner_id'):
        pd=execute("INSERT INTO partner_directions(partner_id,master_category_id,status) VALUES(%s,%s,'pending') ON CONFLICT(partner_id,master_category_id) DO UPDATE SET updated_at=NOW() RETURNING id",(p['partner_id'],m['id']),True)
        if pd:
            execute("INSERT INTO partner_direction_categories(partner_direction_id,category_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(pd['id'],c['id']))
            session=active_session(one('SELECT user_id FROM partners WHERE id=%s',(p['partner_id'],))['user_id'],'partner','onboarding')
            if session:
                ctx=session.get('context_json') or {}
                if isinstance(ctx,str):
                    try: ctx=__import__('json').loads(ctx)
                    except Exception: ctx={}
                ctx['direction_id']=pd['id'];ctx['proposal_id']=proposal_id;ctx['awaiting_document']=True;update_session(session['id'],ctx)
    review_proposal(proposal_id,'approved',admin_id,str((data or {}).get('comment') or ''))
    return {'proposal_id':proposal_id,'master_category':m,'category':c,'subcategory':subrow,'service':service}
