"""
ConanOps' own version number -- shown in the sidebar and on the App
Settings page's updater. This file is itself one of the files a new
version's update zip replaces, so after installing an update this
reflects the new number automatically without anything else needing
to change.
"""
VERSION = "1.0.3"

# GitHub repository ("owner/name") that ConanOps checks for new releases
# -- see app_updates.py and RELEASING.md. Leave empty to turn online
# updates off (updating from a downloaded file still works).
UPDATE_REPO = "PeanutPenguin/ConanOPs"
