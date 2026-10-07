"""
ConanOps entry point.

Run with:  python main.py
Package with PyInstaller for a standalone .exe, e.g.:
  pyinstaller --noconfirm --onefile --windowed --name ConanOps ^
      --icon assets/conanops.ico --add-data "assets;assets" main.py
(--add-data bundles the icon, fonts and loading animation -- see
ui/assets.py; without it the app still runs, just with system fonts and
no artwork.)
"""
import os
import sys

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import admin_mode
import background_mode
import keep_alive
import conanops_paths
import self_update
import applog
from models import AppConfig
from ui.app_lock_dialog import UnlockDialog
from ui.main_window import MainWindow, APP_INSTALL_DIR
from ui import assets
from ui.splash import SplashScreen
from ui.theme import build_stylesheet
from theme_config import load_theme


# Windows groups taskbar buttons by "AppUserModelID". When ConanOps runs
# from source, the process is pythonw.exe, so without its own ID Windows
# files the window under Python and shows Python's generic icon (the
# paper-with-Python-logo) on the taskbar instead of ConanOps'. Giving
# the process its own ID makes the taskbar use the window icon we set.
# Must happen before any window exists.
APP_USER_MODEL_ID = "ConanOps.ServerManager"


def set_windows_app_id() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        return True
    except (AttributeError, OSError):
        return False  # very old Windows: harmless, just keeps the generic icon


def _install_background_dialog_guards() -> None:
    """The background instance has no screen and nobody to click
    anything: any dialog that would normally pop up (a failure message,
    a confirmation) is logged and answered "No"/"OK" instead of waiting
    forever for a click that will never come."""
    from PySide6.QtWidgets import QDialog
    log = applog.get_logger("background")

    def static(kind, default):
        def _stub(parent=None, title="", text="", *args, **kwargs):
            log.warning(f"[no screen] {kind}: {title} -- {text}")
            return default
        return staticmethod(_stub)

    QMessageBox.information = static("information", QMessageBox.Ok)
    QMessageBox.warning = static("warning", QMessageBox.Ok)
    QMessageBox.critical = static("critical", QMessageBox.Ok)
    QMessageBox.question = static("question", QMessageBox.No)
    QDialog.exec = lambda self: (log.warning(f"[no screen] dialog skipped: {self.windowTitle()}"), QDialog.Rejected)[1]


def main() -> int:
    if "--uninstall-cleanup" in sys.argv:
        return _uninstall_cleanup()
    background = background_mode.BACKGROUND_FLAG in sys.argv
    elevated_launch = admin_mode.ELEVATED_FLAG in sys.argv
    if elevated_launch:
        # Started by the "Run with admin rights" task (admin_mode.py): pick
        # up the flags the copy that handed over had (e.g. --keep-alive).
        sys.argv += admin_mode.take_launch_args()
    # Started by the keep-alive watcher (keep_alive.py) after ConanOps
    # stopped: open quietly in the tray, and just exit if another copy
    # turns out to be running already.
    keep_alive_launch = keep_alive.FLAG in sys.argv
    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("ConanOps")
    app.setWindowIcon(assets.app_icon())
    assets.load_fonts()

    # Two ConanOps processes both reacting to the same pending-update
    # marker (one confirming/clearing it while the other is mid-rollback
    # decision, say) is exactly the kind of race this file exists to
    # avoid, so refuse to run a second instance at all. The lock is
    # held for this process's whole lifetime by keeping `lock` alive in
    # main()'s scope through app.exec().
    #
    # The 5s wait (not an instant tryLock) matters specifically because
    # of _relaunch_after_update(): the OLD process is still holding
    # this same lock for a brief moment after spawning the new one
    # (Qt/worker teardown on close() isn't instant), so a strict
    # instant-fail check here would make the post-update relaunch lose
    # the race against its own predecessor and exit immediately,
    # leaving no ConanOps process running at all.
    # The folder has to exist first: on a brand-new install (a fresh
    # ConanOps.exe in an empty folder) it doesn't yet, QLockFile can't
    # create its file, and tryLock() failing made the very first launch
    # report "ConanOps is already running" and quit.
    lock_dir = conanops_paths.no_space_root()
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except OSError:
        pass
    lock_path = os.path.join(lock_dir, "conanops.lock")
    lock = QLockFile(lock_path)
    lock.setStaleLockTime(30_000)  # a crashed instance's lock is treated as stale after 30s
    # The admin-rights copy may be waiting for the copy that started it
    # to finish closing (stopping workers, the web server...), which can
    # take much longer than a normal relaunch.
    if not lock.tryLock(2_000 if background else 90_000 if elevated_launch else 5_000):
        if background or keep_alive_launch:
            return 0  # someone already has it (normally the window) -- nothing to do
        if background_mode.background_instance_running():
            # Unattended mode is managing the servers with no window.
            # Ask it to step aside (it exits without stopping anything)
            # and take over.
            background_mode.request_handoff()
            if not lock.tryLock(30_000):
                background_mode.clear_handoff()
                QMessageBox.information(
                    None, "ConanOps Already Running",
                    "ConanOps is running in the background and didn't hand over in time. Try again in a minute.",
                )
                return 0
        else:
            QMessageBox.information(None, "ConanOps Already Running", "ConanOps is already running.")
            return 0
    if not background and admin_mode.should_relaunch(sys.argv):
        # "Run with admin rights" is on: hand over to the elevated copy
        # (no prompt). If it doesn't start, carry on as we are.
        if admin_mode.relaunch_elevated(sys.argv, lock, lock_path):
            return 0
    app._conanops_lock = lock  # so _relaunch_after_update can release it early -- see its comment
    background_mode.clear_handoff()  # a stale request must not make the next background instance quit
    background_mode.write_owner("background" if background else "window")
    if not keep_alive_launch:
        # Opened on purpose (or by sign-in): a previous deliberate Quit no
        # longer stands, so keep-alive may reopen ConanOps again.
        keep_alive.clear_user_quit()
    if background:
        app.setQuitOnLastWindowClosed(False)
        _install_background_dialog_guards()
        applog.get_logger("background").info("Started in background (unattended) mode.")

    # As close to the top as possible: if the previous launch was a
    # self-update that never confirmed it started up cleanly, roll it
    # back to the backup before anything else (config, the main
    # window, etc.) loads whatever that update left behind. See
    # self_update.check_and_recover_pending_update()'s docstring.
    rollback_message = self_update.check_and_recover_pending_update()
    if rollback_message:
        QMessageBox.warning(None, "Update Rolled Back", rollback_message)

    # Independent of whether an update just happened: sweeps up any
    # *.conanops-old file a PAST update left behind if it couldn't be
    # cleaned up at the time (still locked, say) -- see
    # cleanup_leftover_update_files()'s docstring for why this can't
    # just rely on the marker-driven cleanup alone.
    self_update.cleanup_leftover_update_files(APP_INSTALL_DIR)

    config = AppConfig.load()

    # An update is confirmed as working only once the main window has
    # actually been built and shown (see finish_startup below). Before,
    # it was confirmed here, before the window existed -- so an update
    # that crashed while building the window was never rolled back, and
    # left the startup screen frozen with the process still running.

    if config.app_lock_enabled and not background:
        dialog = UnlockDialog(verify_fn=config.verify_app_lock_pin)
        dialog.setStyleSheet(build_stylesheet(load_theme()))
        if dialog.exec() != QDialog.Accepted:
            # Declined to unlock: a deliberate choice, not a crash -- keep
            # a pending update's next launch from counting as its failed
            # first run (which would roll back a good update).
            self_update.defer_update_confirmation()
            return 0

    # The startup screen gets a moment of real event-loop time to
    # animate before the (blocking) main-window build starts. Skipped
    # for a start-minimized launch, which is meant to be silent.
    splash = None
    start_hidden = config.start_minimized_to_tray or keep_alive_launch
    if not start_hidden and not background:
        splash = SplashScreen()
        splash.apply_stylesheet(build_stylesheet(load_theme()))
        splash.show_centered()

    def finish_startup() -> None:
        if splash is not None:
            splash.set_status("Loading your servers…")
        try:
            window = MainWindow(config=config, **({"background": True} if background else {}))
        except Exception as e:  # noqa: BLE001 - shown to the person, then a clean exit
            startup_failed(e)
            return
        app._conanops_window = window  # keep a reference for app.exec()'s lifetime
        start_quit_watch(window)
        if background:
            start_handoff_watch(window)
            QTimer.singleShot(0, self_update.confirm_update_success)
            return
        if not start_hidden:
            window.show()
        elif not (window.tray_icon and window.tray_icon.isVisible()):
            # No tray icon available on this system (or it failed to
            # create one) -- "start minimized" would otherwise mean
            # "start invisible, with no way to ever open it again."
            # Falling back to a normal visible launch is the only safe
            # choice here.
            window.show()
        if splash is not None:
            splash.close()
        # Confirm only now, after the window exists -- and on the next
        # event-loop pass, so it has also painted at least once.
        QTimer.singleShot(0, self_update.confirm_update_success)

    def start_quit_watch(window) -> None:
        """The uninstaller asking ConanOps to close (see
        admin_mode.request_quit). Closing never stops servers."""
        admin_mode.clear_quit_request()
        timer = QTimer(app)

        def check():
            if not admin_mode.quit_requested():
                return
            timer.stop()
            applog.get_logger("startup").info("Closing: the uninstaller asked.")
            admin_mode.clear_quit_request()
            window._really_quit = True
            window.close()
            lock.unlock()
            app.quit()

        timer.timeout.connect(check)
        timer.start(2_000)

    def start_handoff_watch(window) -> None:
        """Background instance: every 2s, check whether a window has asked
        to take over. If so, quit WITHOUT stopping any server -- closing
        ConanOps never stops servers -- and release the lock."""
        timer = QTimer(app)

        def check():
            if not background_mode.handoff_requested():
                return
            timer.stop()
            applog.get_logger("background").info("Handing over to the ConanOps window.")
            background_mode.clear_handoff()
            window._really_quit = True
            window.close()
            background_mode.clear_owner()
            lock.unlock()
            app.quit()

        timer.timeout.connect(check)
        timer.start(2_000)

    def startup_failed(error: Exception) -> None:
        """Building the main window crashed. Close the startup screen,
        say what happened, and exit -- instead of leaving a frozen
        startup screen and a background process that makes the next
        launch report "already running". The update (if any) is left
        unconfirmed, so the next launch rolls it back."""
        import traceback
        applog.get_logger("startup").error("Main window failed to build:\n" + traceback.format_exc())
        if splash is not None:
            splash.close()
        if self_update.update_pending():
            next_step = ("This started after an update, so ConanOps will put the previous version back "
                         "automatically the next time you open it.")
        else:
            next_step = ("Try opening ConanOps again. If it keeps happening, reinstall the last version "
                         "that worked.")
        QMessageBox.critical(
            None, "ConanOps couldn't start",
            f"Something went wrong while opening ConanOps:\n\n{type(error).__name__}: {error}\n\n{next_step}",
        )
        app.exit(1)

    QTimer.singleShot(SPLASH_ANIMATE_MS if splash is not None else 0, finish_startup)
    try:
        return app.exec()
    finally:
        background_mode.clear_owner()


def _uninstall_cleanup() -> int:
    """Run by the uninstaller (ConanOps.exe --uninstall-cleanup): removes
    what ConanOps registered with Windows outside its own folder -- the
    sign-in Run entry and the unattended-mode scheduled task -- and ends
    other ConanOps processes so their files can be removed. Game servers
    keep running, and server data/backups are left alone."""
    log = applog.get_logger("uninstall")
    try:
        import startup_registration
        startup_registration.unregister()
    except OSError as e:
        log.warning(f"Couldn't remove the sign-in entry: {e}")
    try:
        keep_alive.disable()
    except Exception as e:  # noqa: BLE001 - best-effort
        log.warning(f"Couldn't remove the keep-alive task: {e}")
    try:
        # A leftover task would run whatever is later put at ConanOps'
        # old path with admin rights, so it must go (one prompt).
        admin_mode.disable()
    except Exception as e:  # noqa: BLE001 - best-effort
        log.warning(f"Couldn't remove the admin-rights task: {e}")
    try:
        config = AppConfig.load()
        task = background_mode.status()
        if config.background_mode_enabled or (task and task.get("exists")):
            background_mode.unregister()
    except Exception as e:  # noqa: BLE001 - best-effort
        log.warning(f"Couldn't remove the background task: {e}")
    try:
        # A copy running with admin rights can't be ended from here; ask
        # it to close and give it a moment.
        import time
        admin_mode.request_quit()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _other_conanops_running():
            time.sleep(0.5)
    except Exception as e:  # noqa: BLE001
        log.warning(f"Couldn't ask ConanOps to close: {e}")
    try:
        background_mode.stop_other_instances()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Couldn't stop other ConanOps instances: {e}")
    return 0


def _other_conanops_running() -> bool:
    import psutil
    me = os.getpid()
    skip = {me}
    try:
        skip.add(psutil.Process(me).ppid())
    except Exception:  # noqa: BLE001
        pass
    exe_name = os.path.basename(sys.executable).lower() if getattr(sys, "frozen", False) else "conanops.exe"
    return any(p.info["pid"] not in skip and (p.info["name"] or "").lower() == exe_name
               for p in psutil.process_iter(["pid", "name"]))


# How long the startup screen animates before the main window starts
# building. Short on purpose: long enough to read, not a delay anyone
# waits through.
SPLASH_ANIMATE_MS = 500


if __name__ == "__main__":
    sys.exit(main())
