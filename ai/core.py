from openai import AsyncOpenAI
from config import GROQ_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, AI_MODEL, OPENAI_MODEL, OPENROUTER_MODEL

PROVIDERS = (
    ("groq", GROQ_API_KEY, "https://api.groq.com/openai/v1", AI_MODEL),
    ("openai", OPENAI_API_KEY, "https://api.openai.com/v1", OPENAI_MODEL),
    ("openrouter", OPENROUTER_API_KEY, "https://openrouter.ai/api/v1", OPENROUTER_MODEL),
)

async def chat(messages):
    last = None
    for name, key, base, model in PROVIDERS:
        if not key:
            continue
        try:
            client = AsyncOpenAI(api_key=key, base_url=base)
            result = await client.chat.completions.create(model=model, messages=messages)
            return {
                "text": result.choices[0].message.content or "",
                "provider": name,
                "model": model,
                "input_tokens": getattr(result.usage, "prompt_tokens", 0),
                "output_tokens": getattr(result.usage, "completion_tokens", 0),
            }
        except Exception as exc:
            last = exc
    raise RuntimeError(str(last or "No AI provider configured"))
