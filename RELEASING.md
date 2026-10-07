# Releasing a ConanOps update

GitHub builds every release for you on a real Windows machine
(`.github/workflows/release.yml`) -- no building on your own PC.

## Every release

1. **Raise the version.** On GitHub, open `version.py`, click the pencil, change
   `VERSION` (e.g. `1.0.2` -> `1.0.3`) and click **Commit changes**.
2. **Write what's new** the same way in `RELEASE_NOTES.md`. People see this text
   inside ConanOps before they update.
3. **Build it.** **Actions** tab -> **Build ConanOps release** -> **Run workflow** ->
   **Run workflow**. It takes about 10-15 minutes.
4. **Check it.** **Releases** -> the new **Draft**. It has `ConanOps-update.zip` and
   `ConanOps-Setup-<version>.exe` attached. Download the setup exe and try it.
5. **Publish.** Edit the draft -> **Publish release**. Within a day every copy of
   ConanOps offers (or installs) it.

Drafts are invisible to everyone else, and so are pre-releases -- ConanOps only
offers published, non-pre-release releases that have `ConanOps-update.zip` attached.

## If a build fails

Open the failed run in the **Actions** tab; the red step shows what went wrong.
"is already released" means `VERSION` wasn't raised. If **Run tests** fails, a test
broke on Windows and no release was made -- the run's summary lists which tests failed.

## Building on your own PC instead

`BUILD_EXE.bat` still works (needs Python, and Inno Setup 6 for the installer).
Upload the files from `dist\` to a release by hand. Code signing: see SIGNING.md.

## Safety nets built into ConanOps

- Downloads are checked against the size and SHA-256 checksum GitHub publishes for
  each release file, then validated as a ConanOps package before installing.
- The current version is backed up first; if the new one fails to start, the next
  launch puts the old one back automatically.
- A bad release is fixed by publishing a newer one; deleting a release just stops it
  being offered.
