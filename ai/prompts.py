import json


def prompt(context, text, context_data=None):
    data = json.dumps(context_data or {}, ensure_ascii=False, default=str)
    rules = (
        "Armenia AI Guide. AI is an interface, never the source of truth. "
        "Use only supplied context. Never invent IDs, partners, prices, documents or state. "
        "Reads are immediate. Writes must produce a preview and wait for explicit confirmation."
    )
    if context == "PARTNER":
        rules += (
            " Return ONLY valid JSON. For a create-service request return action=create_service and "
            "services as an array. For every service return name, price_type=fixed/from, price_amd, "
            "description, hours, at_client, internal_phone, and location. "
            "location may contain address, marzes, cities, districts. "
            "If the user gives one location for all services, put it in the top-level location too. "
            "Never select catalog IDs; backend classification does that."
        )
    elif context == "REGISTRATION":
        rules += (
            " Return ONLY valid JSON. Extract only information explicitly present in the request. "
            "Return name, phone, marz, city, village, address, hours, description, and services. "
            "services is an array of objects with name, price_type=fixed/from, price_amd. "
            "Do not invent missing values. The backend keeps the draft and asks only for missing fields."
        )
    else:
        rules += " Return ONLY valid JSON with action and answer."
    return [{"role": "user", "content": rules + "\nContext: " + data + "\nRequest: " + str(text)}]
