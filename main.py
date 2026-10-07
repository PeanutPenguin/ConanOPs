"""
ConanOps entry point. Run with `python main.py`, or package with:
  pyinstaller --noconfirm --onefile --windowed --name ConanOps ^
      --icon assets/conanops.ico --add-data "assets;assets" main.py
(--add-data bundles icons/fonts/animation; without it system fonts are used.)
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


# Own taskbar ID so Windows shows our icon instead of pythonw's when run
# from source. Must be set before any window exists.
APP_USER_MODEL_ID = "ConanOps.ServerManager"


def set_windows_app_id() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        return True
    except (AttributeError, OSError):
        return False


def _install_background_dialog_guards() -> None:
    """With no screen, log dialogs and answer "No"/"OK" instead of blocking forever."""
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
        # Pick up the flags the non-elevated copy was started with.
        sys.argv += admin_mode.take_launch_args()
    # Keep-alive relaunch: start quietly in the tray, exit if already running.
    keep_alive_launch = keep_alive.FLAG in sys.argv
    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("ConanOps")
    app.setWindowIcon(assets.app_icon())
    assets.load_fonts()

    # Single instance: two processes racing on the pending-update marker
    # could break rollback. The wait (not an instant tryLock) lets a
    # post-update relaunch outlast the old process still holding the lock.
    # The folder must exist first or tryLock fails on a fresh install.
    lock_dir = conanops_paths.no_space_root()
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except OSError:
        pass
    lock_path = os.path.join(lock_dir, "conanops.lock")
    lock = QLockFile(lock_path)
    lock.setStaleLockTime(30_000)
    # The elevated copy may wait a long time for its starter to finish closing.
    if not lock.tryLock(2_000 if background else 90_000 if elevated_launch else 5_000):
        if background or keep_alive_launch:
            return 0
        if background_mode.background_instance_running():
            # Ask the windowless instance to step aside (servers keep running).
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
        # Hand over to the elevated copy; if it doesn't start, carry on.
        if admin_mode.relaunch_elevated(sys.argv, lock, lock_path):
            return 0
    app._conanops_lock = lock  # lets _relaunch_after_update release it early
    background_mode.clear_handoff()  # a stale request must not make the next background instance quit
    background_mode.write_owner("background" if background else "window")
    if not keep_alive_launch:
        # Opened on purpose: an earlier deliberate Quit no longer blocks keep-alive.
        keep_alive.clear_user_quit()
    if background:
        app.setQuitOnLastWindowClosed(False)
        _install_background_dialog_guards()
        applog.get_logger("background").info("Started in background (unattended) mode.")

    # Roll back an unconfirmed self-update before anything else loads.
    rollback_message = self_update.check_and_recover_pending_update()
    if rollback_message:
        QMessageBox.warning(None, "Update Rolled Back", rollback_message)

    # Sweep *.conanops-old files a past update couldn't remove.
    self_update.cleanup_leftover_update_files(APP_INSTALL_DIR)

    config = AppConfig.load()

    if config.app_lock_enabled and not background:
        dialog = UnlockDialog(verify_fn=config.verify_app_lock_pin)
        dialog.setStyleSheet(build_stylesheet(load_theme()))
        if dialog.exec() != QDialog.Accepted:
            # Declining to unlock isn't a crash: don't count it as a failed
            # first run of a pending update (which would roll it back).
            self_update.defer_update_confirmation()
            return 0

    # Give the splash a moment to animate before the blocking window build.
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
            # No tray icon: starting hidden would leave no way to open the window.
            window.show()
        if splash is not None:
            splash.close()
        # Confirm an update only once the window exists and has painted.
        QTimer.singleShot(0, self_update.confirm_update_success)

    def start_quit_watch(window) -> None:
        """Closes when the uninstaller asks (admin_mode.request_quit); servers keep running."""
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
        """Background instance: quit (without stopping servers) and release
        the lock when a window asks to take over."""
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
        """Main window build crashed: report it and exit cleanly. A pending
        update stays unconfirmed so the next launch rolls it back."""
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
    """`--uninstall-cleanup`: removes the sign-in entry and scheduled tasks
    and closes other ConanOps processes. Servers and their data are untouched."""
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
        # A leftover admin task would run whatever later sits at our old path.
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
        # An elevated copy can't be killed from here; ask it to close.
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


SPLASH_ANIMATE_MS = 500


if __name__ == "__main__":
    sys.exit(main())
