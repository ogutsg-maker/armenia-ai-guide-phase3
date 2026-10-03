import json

def prompt(context, text, context_data=None):
    data = json.dumps(context_data or {}, ensure_ascii=False, default=str)
    rules = (
        "Armenia AI Guide. AI is an interface, never the source of truth. "
        "Use only supplied context. Never invent IDs, partners, prices, availability or state. "
        "Reads are immediate. Writes must produce a preview and wait for explicit confirmation."
    )
    if context == "PARTNER":
        rules += (
            " Return ONLY valid JSON. For create-service requests return action=create_service "
            "with name, price_type=fixed/from, price_amd, hours, at_client, territory, internal_phone. "
            "Do not select catalog IDs."
        )
    else:
        rules += " Return ONLY valid JSON with action and answer."
    return [{"role": "user", "content": rules + "\nContext: " + data + "\nRequest: " + str(text)}]
