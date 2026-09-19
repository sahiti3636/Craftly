from c2.fmt import inr


def test_indian_grouping():
    assert inr(0) == "₹0"
    assert inr(999) == "₹999"
    assert inr(1234) == "₹1,234"
    assert inr(123456) == "₹1,23,456"
    assert inr(1234567) == "₹12,34,567"


def test_missing_is_a_dash_not_zero():
    assert inr(None) == "-"
