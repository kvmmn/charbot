"""Chase-via identity: Ghazal is followed up through Hamed."""

from __future__ import annotations

from charbot.members import chase_via, followup_addressee_fa


def test_chase_via_ghazal_goes_to_hamed():
    assert chase_via("ghazal") == "hamed"
    assert chase_via("saman") == "saman"


def test_followup_addressee_fa_ghazal_names_hamed_and_ghazal():
    text = followup_addressee_fa("ghazal")
    assert text is not None
    assert "حامد" in text
    assert "غزل" in text


def test_followup_addressee_fa_saman_is_just_saman():
    assert followup_addressee_fa("saman") == "سامان"
