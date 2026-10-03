from data.core import active_services
from data.partners import get as get_partner, companies
from db import run

def partner_context(uid):
    partner = get_partner(uid)
    if not partner: return {'partner':None,'companies':[]}
    return {'partner':partner,'companies':companies(partner['id'])}

def read_tool(uid, name, args=None):
    args=args or {}
    if name=='partner_context': return partner_context(uid)
    if name=='partner_services':
        partner=get_partner(uid)
        if not partner: return {'items':[]}
        rows=run('SELECT s.*,c.name AS company_name FROM aig_services s JOIN aig_companies c ON c.id=s.company_id WHERE c.partner_id=%s AND c.archived=false ORDER BY s.id DESC',(partner['id'],),True)
        return {'items':rows}
    if name=='client_search': return {'items':active_services(args.get('text'),args.get('city'))}
    raise ValueError('unknown_read_tool')
