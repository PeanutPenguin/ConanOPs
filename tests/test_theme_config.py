from __future__ import annotations

import json

import theme_config as tc


def test_missing_file_returns_defaults(tmp_path):
    path = str(tmp_path / "theme.json")
    palette = tc.load_theme(path)
    assert palette == tc.ThemePalette()


def test_save_then_load_round_trip(tmp_path):
    path = str(tmp_path / "theme.json")
    original = tc.ThemePalette(accent="#3f7fd6", bg="#000000")
    tc.save_theme(original, path)

    loaded = tc.load_theme(path)
    assert loaded.accent == "#3f7fd6"
    assert loaded.bg == "#000000"


def test_partial_file_only_overrides_given_fields(tmp_path):
    path = str(tmp_path / "theme.json")
    with open(path, "w") as f:
        json.dump({"accent": "#00ff00"}, f)

    palette = tc.load_theme(path)
    assert palette.accent == "#00ff00"
    assert palette.bg == tc.DEFAULT_PALETTE.bg  # untouched fields stay default


def test_invalid_color_falls_back_to_default_for_that_field_only(tmp_path):
    path = str(tmp_path / "theme.json")
    with open(path, "w") as f:
        json.dump({"accent": "not-a-color", "bg": "#111111"}, f)

    palette = tc.load_theme(path)
    assert palette.accent == tc.DEFAULT_PALETTE.accent  # rejected, fell back
    assert palette.bg == "#111111"  # this one was valid


def test_unknown_keys_are_ignored(tmp_path):
    path = str(tmp_path / "theme.json")
    with open(path, "w") as f:
        json.dump({"accent": "#00ff00", "some_future_field": "whatever"}, f)

    palette = tc.load_theme(path)  # should not raise
    assert palette.accent == "#00ff00"


def test_malformed_json_falls_back_to_defaults(tmp_path):
    path = str(tmp_path / "theme.json")
    with open(path, "w") as f:
        f.write("{not valid json")

    palette = tc.load_theme(path)
    assert palette == tc.ThemePalette()


def test_non_object_json_falls_back_to_defaults(tmp_path):
    path = str(tmp_path / "theme.json")
    with open(path, "w") as f:
        json.dump(["accent", "#00ff00"], f)

    palette = tc.load_theme(path)
    assert palette == tc.ThemePalette()


def test_font_field_accepts_free_text(tmp_path):
    path = str(tmp_path / "theme.json")
    with open(path, "w") as f:
        json.dump({"font_ui": "Comic Sans MS, sans-serif"}, f)

    palette = tc.load_theme(path)
    assert palette.font_ui == "Comic Sans MS, sans-serif"


def test_old_default_values_upgrade_to_the_new_theme(tmp_path):
    import json
    path = tmp_path / "theme.json"
    old = dict(tc.LEGACY_DEFAULTS, accent="#c9752f")
    old["red"] = "#ff0000"  # one field the person really did change
    path.write_text(json.dumps(old))
    palette = tc.load_theme(str(path))
    assert palette.bg == tc.DEFAULT_PALETTE.bg
    assert palette.font_ui == tc.DEFAULT_PALETTE.font_ui
    assert palette.red == "#ff0000"
