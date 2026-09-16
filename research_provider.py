"""Optional real web-research adapter.
No fabricated candidates: if no search provider is configured, the system explicitly says so.
Supports Serper when SERPER_API_KEY is configured; the interface can later accept another provider.
"""
from __future__ import annotations
import os
import aiohttp

async def search_web(query: str, location: str = 'Armenia', limit: int = 10) -> list[dict]:
    provider=os.getenv('SEARCH_PROVIDER','').strip().lower()
    key=os.getenv('SERPER_API_KEY','').strip()
    if provider in ('serper','') and key:
        payload={'q':query,'gl':'am','hl':'en','num':max(1,min(limit,20))}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.post('https://google.serper.dev/search',json=payload,headers={'X-API-KEY':key,'Content-Type':'application/json'}) as r:
                if r.status!=200: raise RuntimeError(f'search_provider_http_{r.status}')
                data=await r.json()
        return [{'title':x.get('title',''),'url':x.get('link',''),'snippet':x.get('snippet','')} for x in data.get('organic',[])[:limit]]
    raise RuntimeError('search_provider_not_configured')
