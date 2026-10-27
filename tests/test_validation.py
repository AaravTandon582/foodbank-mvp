"""Bad input should be a 400 with a message, never a 500, never a stored value."""
import pytest

import db
from validation import Invalid, number, text


def test_number_rejects_junk():
    for bad in ("abc", "nan", "inf", "-inf", "1e400"):
        with pytest.raises(Invalid):
            number({"q": bad}, "q", low=0, high=1000, required=True)


def test_number_enforces_bounds():
    with pytest.raises(Invalid):
        number({"q": "1e308"}, "q", low=0, high=1_000_000)
    with pytest.raises(Invalid):
        number({"q": "-5"}, "q", low=0, high=100)
    assert number({"q": "42.5"}, "q", low=0, high=100) == 42.5


def test_number_optional_vs_required():
    assert number({}, "q", default=7.0) == 7.0
    with pytest.raises(Invalid):
        number({}, "q", required=True)


def test_text_trims_caps_and_strips_control_chars():
    assert text({"n": "  Canned  Peas  "}, "n") == "Canned Peas"
    assert text({"n": "a\x00\x07b"}, "n") == "ab"
    assert len(text({"n": "x" * 500}, "n", limit=80)) == 80
    with pytest.raises(Invalid):
        text({"n": "   "}, "n", required=True)


def test_bad_qty_is_a_400_not_a_500(client, token):
    r = client.post("/items/1/qty", data={"qty": "abc", "par_level": "5", "_csrf": token})
    assert r.status_code == 400


def test_absurd_values_are_not_stored(client, token):
    client.post("/items", data={"name": "Bounds Test", "qty": "1e308",
                                "par_level": "-999", "_csrf": token})
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM items WHERE name = 'Bounds Test'").fetchone()
    assert row is None, "out-of-range values were accepted"


def test_settings_are_clamped_even_if_the_db_is_edited_by_hand(fresh_db):
    with db.session() as conn:
        conn.execute("UPDATE settings SET value = '9999' WHERE key = 'price_weight'")
    assert db.get_settings()["price_weight"] == 1.0

    with db.session() as conn:
        conn.execute("UPDATE settings SET value = 'garbage' WHERE key = 'max_distance'")
    assert db.get_settings()["max_distance"] == 15.0


def test_money_filter_survives_bad_values():
    from app import money
    assert money(3) == "$3.00"
    assert money(None) == "—"
    assert money("x") == "—"
