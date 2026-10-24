"""Starts Azeroth Survivors with the mod.

Rebuilds AzerothSurvivors-52.exe when the official exe or the patches changed,
installs the translations from lang\\ and starts the game. No console window;
errors are shown in a message box.
"""
import contextlib
import ctypes
import io
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
GAME = HERE.parent
sys.path.insert(0, str(HERE))
import auto_update


def fail(message):
    if sys.stderr is not None:
        print(message, file=sys.stderr, flush=True)
    ctypes.windll.user32.MessageBoxW(None, message, "Azeroth Survivors mod", 0x10)
    sys.exit(1)


def warn(message):
    if sys.stderr is not None:
        print(message, file=sys.stderr, flush=True)
    ctypes.windll.user32.MessageBoxW(None, message, "Azeroth Survivors mod", 0x30)


def game_running():
    tasks = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                           creationflags=subprocess.CREATE_NO_WINDOW).stdout.lower()
    return '"azerothsurvivors.exe"' in tasks or '"azerothsurvivors-52.exe"' in tasks


def prepare(update_status):
    update_status("Проверка игры…")
    if game_running():
        return {"error": "The game is already running. Close it first: the launcher edits the save before start.",
                "warning": None}

    update_plan = auto_update.maybe_stage(HERE, update_status)
    if update_plan:
        update_status('Обновление загружено — перезапуск мода…')
        return {"error": None, "warning": None, "update": update_plan}

    update_status("Проверка и сборка мода…")
    out = io.StringIO()
    try:
        import patch_azeroth
        import save
        with contextlib.redirect_stdout(out):
            code = patch_azeroth.main(progress=update_status)
    except Exception:
        return {"error": out.getvalue() + traceback.format_exc(), "warning": None}
    if code:
        return {"error": out.getvalue(), "warning": None}

    update_status("Синхронизация прогресса…")
    # the save is only written after a successful sync, so a failure here is safe to skip
    warning = None
    try:
        save.share_progress(save.read_key())
    except Exception:
        warning = ("Account-wide progress was not synced this time, the game starts anyway.\n\n"
                   + traceback.format_exc())

    update_status("Копирование файлов мода…")
    (GAME / "lang").mkdir(exist_ok=True)
    for f in (HERE / "lang").glob("*.json"):
        shutil.copy2(f, GAME / "lang" / f.name)
    return {"error": None, "warning": warning}


def prepare_with_progress():
    import tkinter as tk
    import tkinter.ttk as ttk

    updates = queue.Queue()
    finished = threading.Event()
    result = {}
    root = tk.Tk()
    root.title("Azeroth Survivors — запуск мода")
    root.resizable(False, False)
    root.protocol("WM_DELETE_WINDOW", lambda: None)
    root.geometry("380x112")
    frame = ttk.Frame(root, padding=(18, 14))
    frame.pack(fill="both", expand=True)
    status = ttk.Label(frame, text="Подготовка мода…", anchor="w")
    status.pack(fill="x", pady=(0, 12))
    progress = ttk.Progressbar(frame, mode="indeterminate", length=340)
    progress.pack(fill="x")
    progress.start(12)

    def report_status(message):
        updates.put(message)
        if sys.stderr is not None:
            print(f"[prepare] {message}", file=sys.stderr, flush=True)

    def prepare_in_background():
        try:
            result.update(prepare(report_status))
        except Exception:
            result.update(error=traceback.format_exc(), warning=None)
        finally:
            finished.set()

    def poll_progress():
        while True:
            try:
                status.configure(text=updates.get_nowait())
            except queue.Empty:
                break
        if finished.is_set():
            progress.stop()
            root.destroy()
        else:
            root.after(50, poll_progress)

    threading.Thread(target=prepare_in_background, daemon=True).start()
    root.after(50, poll_progress)
    root.mainloop()
    return result


def launch():
    prepared = prepare_with_progress()
    if prepared["error"]:
        fail(prepared["error"])
    if prepared["warning"]:
        warn(prepared["warning"])
    if prepared.get('update'):
        auto_update.start_apply(prepared['update'], online='--pvp' in sys.argv)
        return

    if "--pvp" not in sys.argv:
        subprocess.Popen([str(GAME / "AzerothSurvivors-52.exe")], cwd=GAME)
        try:
            import bans  # keeps the Codex bans of mod jewels across restarts
            bans.sync()  # stays (windowless) until the game exits
        except Exception:
            pass  # the game is already running; only the bans aren't kept this time
        return
    # PvP: the companion starts the game itself on a separate profile once a match begins
    try:
        import bans
        import pvp
        threading.Thread(target=bans.sync, kwargs={"until_exit": False}, daemon=True).start()
        pvp.main()
    except Exception:
        fail("The PvP window failed to start.\n\n" + traceback.format_exc())


def main():
    with auto_update.single_launcher(HERE):
        launch()


if __name__ == "__main__":
    main()
