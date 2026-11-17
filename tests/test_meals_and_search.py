"""Meal-name resolution and v2 search parsing - pure logic, no network."""

import pytest

from mfp_mcp import server

NAMES = ["Grap-and-go", "Lunch", "Dinner", "Snacks", "Tilskudd"]


@pytest.mark.parametrize("meal,expected", [
    ("Breakfast", ("Grap-and-go", 0)),
    ("breakfast", ("Grap-and-go", 0)),
    ("frokost", ("Grap-and-go", 0)),
    ("grap-and-go", ("Grap-and-go", 0)),
    ("Lunch", ("Lunch", 1)),
    ("middag", ("Dinner", 2)),
    ("snack", ("Snacks", 3)),
    ("Tilskudd", ("Tilskudd", 4)),
])
def test_resolve_meal_custom_names(meal, expected):
    assert server.resolve_meal(meal, NAMES) == expected


def test_resolve_meal_defaults_when_names_unknown():
    assert server.resolve_meal("Breakfast", []) == ("Breakfast", 0)


def test_resolve_meal_rejects_unknown():
    with pytest.raises(ValueError, match="Grap-and-go"):
        server.resolve_meal("Brunch", NAMES)


def test_parse_v2_search():
    payload = {"items": [
        {"item": {"id": "1", "description": "Pils", "brand_name": "Schous",
                  "verified": False,
                  "nutritional_contents": {"energy": {"value": 34.0},
                                           "protein": 0.4, "carbohydrates": 2.4,
                                           "fat": 0.0},
                  "serving_sizes": [{"value": 100.0, "unit": "ml",
                                     "nutrition_multiplier": 1.0}]}},
        {"item": {"id": "2", "description": "Egg", "deleted": True}},
        {"item": {"id": "3", "description": "Kanel", "brand_name": "Wasa",
                  "nutritional_contents": {"energy": {"value": 60.0}},
                  "serving_sizes": [{"value": 1.0, "unit": "skive",
                                     "nutrition_multiplier": 2.0}]}},
    ]}
    rows = server.parse_v2_search(payload)
    assert [r["mfp_id"] for r in rows] == ["1", "3"]
    assert rows[0]["serving"] == "100 ml" and rows[0]["calories"] == 34.0
    assert rows[1]["calories"] == 120.0 and rows[1]["protein"] is None
