    # the values. The partner form consumes this exact shape.
    if not merged.get("city"):
        merged["city"] = merged.get("location_city") or merged.get("settlement") or ""
    if not merged.get("marz"):
        merged["marz"] = merged.get("location_marz") or merged.get("region") or ""
    if not merged.get("phone"):
        merged["phone"] = merged.get("phone_number") or ""
    normalized_services=[]
    for svc in (merged.get("services") or []):
        if not isinstance(svc,dict):
            continue
        name=str(svc.get("name") or svc.get("service_name") or svc.get("service") or "").strip()
        if not name:
            continue
        item=dict(svc)
        item["name"]=name
        if item.get("price") in ("",None):
            item["price"]=None
        try:
            if item.get("price") is not None:
                item["price"]=float(item["price"])
        except (TypeError,ValueError):
            item["price"]=None
        item["price_type"]=str(item.get("price_type") or "fixed").strip().lower()
        if item["price_type"] in {"starting","starting_from","from_price"}:
            item["price_type"]="from"
        normalized_services.append(item)
    if normalized_services:
        merged["services"]=normalized_services

    # The partner never needs to provide an internal catalogue direction.
    # AI matching/proposal handles that automatically.
    # Business name is a required partner-facing registration field.
    # Direction/subcategory remain admin-side classification fields.
    missing = [key for key in ("business_name", "marz", "city", "phone", "services") if not merged.get(key)]
    merged["missing"] = missing
    merged["ready"] = not missing

    # Classification is internal and invisible as a choice to the partner,
    # but it IS calculated during registration so the preview already contains
    # the direction/subcategory determined by the current live catalogue.
    matched_services = [
        svc for svc in normalized_services
        if str(svc.get("matched_subcategory_id") or "").strip().isdigit()
    ]
    if matched_services:
        merged["master_category_id"] = matched_services[0].get("direction_id")
        merged["direction"] = (
            matched_services[0].get("direction_name")
            or merged.get("direction")
            or None
        )
        merged["classification_confidence"] = max(
            float(svc.get("match_confidence") or 0)
            for svc in matched_services
        )
        merged["classification_ambiguities"] = []
        merged["classification_needs_review"] = False
    else:
        merged["master_category_id"] = None
        merged["classification_confidence"] = 0
        merged["classification_ambiguities"] = ["subcategory_not_matched"] if normalized_services else []
        merged["classification_needs_review"] = bool(normalized_services)

    await state.update_data(partner_onboarding_history=history, partner_profile=merged)
    if missing:
        # IMPORTANT: the AI result is shown immediately in the universal form.
        # Missing fields remain editable/empty; the partner does not have to
        # answer a questionnaire before seeing what AI understood.
        draft = data_core.save_partner_application_draft(user_id=uid, profile=merged)
        question = missing_question(merged, lang)
        history.append({"role": "assistant", "content": question})
        await state.update_data(
            partner_onboarding_pending_field=missing[0],
            partner_onboarding_history=history,
            partner_profile=merged,
            partner_application_id=draft.get("application_id"),