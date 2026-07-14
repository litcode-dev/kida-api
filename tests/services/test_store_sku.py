import pytest

from app.services import store_sku


def test_generate_sku_loop():
    sku = store_sku.generate_sku("loop", "Amapiano Groove", "1a2b3c4d5e6f7890")
    assert sku == "kida.loop.amapiano_groove_1a2b3c4d"
    assert store_sku.is_valid_sku(sku)


def test_generate_sku_kit_and_drone_prefixes():
    kit = store_sku.generate_sku("kit", "Trap Essentials", "abcdef0123456789")
    drone = store_sku.generate_sku("drone", "Worship Pad", "abcdef0123456789")
    assert kit.startswith("kida.kit.")
    assert drone.startswith("kida.drone.")


def test_generate_sku_strips_and_lowercases_special_chars():
    sku = store_sku.generate_sku("loop", "Lo-Fi Beat!! (Deluxe)", "DEADBEEF00000000")
    # only lowercase letters, digits, ._ allowed; UUID prefix lowercased
    assert sku == "kida.loop.lo_fi_beat_deluxe_deadbeef"
    assert store_sku.is_valid_sku(sku)


def test_generate_sku_empty_title_falls_back_to_item():
    sku = store_sku.generate_sku("loop", "!!!", "12345678aaaaaaaa")
    assert sku == "kida.loop.item_12345678"
    assert store_sku.is_valid_sku(sku)


def test_generate_sku_unknown_kind_raises():
    with pytest.raises(ValueError):
        store_sku.generate_sku("bundle", "Whatever", "12345678aaaaaaaa")


def test_is_valid_sku_rejects_bad_values():
    assert not store_sku.is_valid_sku("com.litmusic.loop.foo")  # wrong prefix
    assert not store_sku.is_valid_sku("kida.loop.UPPER")  # uppercase not allowed
    assert not store_sku.is_valid_sku("kida.widget.foo")  # unknown kind
    assert not store_sku.is_valid_sku("kida.loop.has space")  # space not allowed
