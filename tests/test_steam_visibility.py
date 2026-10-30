"""Steam's public list of servers at an address (steam_visibility.py)."""
import steam_visibility


def test_steam_listing_matches_this_server_by_port(monkeypatch):
    data = {"response": {"success": True, "servers": [
        {"addr": "203.0.113.1:27015", "gameport": 7777, "appid": 440900},
        {"addr": "203.0.113.1:27020", "gameport": 7790, "appid": 440900}]}}
    monkeypatch.setattr(steam_visibility, "_fetch", lambda ip, timeout: data)
    assert steam_visibility.steam_listing("203.0.113.1", 7790, 27020).match["gameport"] == 7790
    assert steam_visibility.steam_listing("203.0.113.1", 7800, 27030).state == steam_visibility.NOT_LISTED
    monkeypatch.setattr(steam_visibility, "_fetch", lambda ip, timeout: (_ for _ in ()).throw(OSError("offline")))
    assert steam_visibility.steam_listing("203.0.113.1", 7777, 27015).state == steam_visibility.UNKNOWN
