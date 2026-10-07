from __future__ import annotations

import changelog


def test_is_major_update_true_for_major_version_bump():
    assert changelog.is_major_update("3.0", "4.0", "") is True


def test_is_major_update_false_for_matching_versions_and_no_keywords():
    assert changelog.is_major_update("3.5", "3.5", "fixed some bugs") is False


def test_is_major_update_true_for_minor_bump_when_both_present():
    assert changelog.is_major_update("3.5", "3.6", "") is True


def test_is_major_update_true_on_keyword_regardless_of_version():
    assert changelog.is_major_update("3.5", "3.5", "This patch includes a full Unreal Engine 5 migration.") is True


def test_is_major_update_keyword_match_is_case_insensitive():
    assert changelog.is_major_update("1.0", "1.0", "UE5 NETWORKING MODEL overhaul") is True
