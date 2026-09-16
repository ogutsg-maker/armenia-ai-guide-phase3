"""AI structuring layer for potential-partner research.
The actual web-search provider is intentionally an adapter: no fake partners are generated.
"""
from __future__ import annotations
import json
import logging
from platform_db import create_potential

logger=logging.getLogger(__name__)

class PotentialPartnerAI:
    def __init__(self, ai): self.ai=ai

    async def structure_candidate(self, raw_text:str, source='manual_research') -> dict:
        system='''Դու Armenia AI Guide-ի AI Research Manager-ն ես։
Քեզ տրված իրական աղբյուրներից ստացված տեքստից կազմիր պոտենցիալ գործընկերոջ կառուցվածքային քարտ։ Երբեք մի հորինիր անուն, հեռախոս, հասցե, կայք կամ ծառայություն։ Եթե տվյալ չկա՝ թող դատարկ։
JSON: {"business_name":"","description":"","direction":"","category":"","subcategory":"","country":"Armenia","marz":"","city":"","village":"","phone":"","website":"","email":"","social":{},"services":[],"prices":[],"source_urls":[],"ai_reason":"","ai_confidence":0}
'''
        try:
            raw=await self.ai._call_groq(system,raw_text,True)
            data=json.loads(raw)
        except Exception:
            logger.exception('Potential partner structuring failed')
            data={'business_name':'','description':raw_text[:1000],'source_urls':[],'ai_reason':'AI structuring failed','ai_confidence':0}
        data['source']=source
        if not data.get('business_name'):
            raise ValueError('business_name_required')
        return create_potential(data)
