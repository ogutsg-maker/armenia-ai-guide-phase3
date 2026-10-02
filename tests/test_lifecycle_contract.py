"""Deterministic tests for the final six lifecycle blocks."""
from lifecycle_contract import (
    LifecycleViolation,
    can_transition,
    commission_amount,
    contacts_may_disclose,
    payment_may_confirm,
    potential_partner_may_invite,
    qr_may_activate,
    qr_may_checkin,
    require_transition,
    service_activation_allowed,
    validate_booking_price,
)


def test_booking_state_machine():
    assert can_transition("pending_partner_confirmation", "pending_payment")
    assert can_transition("pending_payment", "paid")
    assert can_transition("paid", "in_progress")
    assert can_transition("in_progress", "completed")
    assert not can_transition("pending_payment", "completed")
    assert not can_transition("completed", "paid")
    require_transition("paid", "in_progress")
    try:
        require_transition("pending_payment", "completed")
    except LifecycleViolation:
        pass
    else:
        raise AssertionError("invalid transition accepted")


def test_fixed_and_from_price_contract():
    assert validate_booking_price(exact=15500, agreed_min=None, agreed_max=None) == (15500.0, 15500.0, 15500.0)
    assert validate_booking_price(exact=None, agreed_min=12000, agreed_max=18000) == (15000.0, 12000.0, 18000.0)


def test_commission_contract():
    assert commission_amount(10000, "inside", 10) == (1000.0, 9000.0)
    assert commission_amount(10000, "on_top", 10) == (1000.0, 10000.0)
    assert commission_amount(10000, "fixed", 2000) == (2000.0, 8000.0)


def test_payment_qr_and_contact_gates():
    assert payment_may_confirm("pending_payment")
    assert not payment_may_confirm("paid")
    assert qr_may_activate("paid", "paid")
    assert not qr_may_activate("pending_payment", "paid")
    assert qr_may_checkin("active", "paid", False)
    assert not qr_may_checkin("active", "paid", True)
    assert contacts_may_disclose("paid", "paid")
    assert not contacts_may_disclose("pending", "paid")


def test_service_activation_contract():
    assert service_activation_allowed("approved", "classified", True)
    assert not service_activation_allowed("approved", "classified", False)
    assert not service_activation_allowed("pending_admin", "classified", True)


def test_potential_partner_contract():
    assert potential_partner_may_invite("new")
    assert potential_partner_may_invite("reviewed")
    assert not potential_partner_may_invite("invited")
