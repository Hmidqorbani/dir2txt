"""
dir2txt GUI
-----------
A small Tkinter front-end for dir2txt (https://github.com/Hmidqorbani/dir2txt).

The user picks (or drags in) a folder, presses "Convert", and gets one text file.
The GUI runs dir2txt exactly like `python -m dir2txt -i ... -o ... -d ... -e ...`,
but inside a background thread, so the window never freezes and it also works
when packaged with PyInstaller (no external python needed).

Put this file in the repo root (next to the `dir2txt/` package) and build with:

    pip install pyinstaller tkinterdnd2
    pip install -e .
    pyinstaller --noconfirm --onefile --windowed --name dir2txt-gui ^
        --icon=icon.ico --add-data "icon.ico;." ^
        --collect-all dir2txt --collect-all tkinterdnd2 gui.py
"""

import contextlib
import io
import json
import os
import queue
import runpy
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Optional drag & drop support (pip install tkinterdnd2). The app works without it.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BaseTk = TkinterDnD.Tk
    HAS_DND = True
except Exception:  # pragma: no cover
    BaseTk = tk.Tk
    DND_FILES = None
    HAS_DND = False

APP_TITLE = "dir2txt"
SETTINGS_FILE = Path.home() / ".dir2txt_gui.json"

IGNORE_TEMPLATE = """# .dir2txtignore  (same syntax as .gitignore)

# Python
__pycache__/
*.pyc
.venv/
venv/
.env

# Node
node_modules/

# Git / IDE
.git/
.vscode/
.idea/

# Build output
dist/
build/
*.exe
*.dll
*.so

# Big data / models
*.pt
*.pth
*.onnx
*.h5
"""


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def resource_path(name: str) -> str:
    """Path to a bundled resource (works in dev and inside PyInstaller)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def open_in_os(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class QueueWriter(io.TextIOBase):
    """File-like object that forwards everything written to a queue."""

    def __init__(self, q: "queue.Queue"):
        self.q = q

    def write(self, s: str) -> int:
        if s:
            self.q.put(("log", s))
        return len(s)

    def flush(self) -> None:
        pass


def run_dir2txt(args: list, cwd: str, q: "queue.Queue") -> None:
    """Run `python -m dir2txt <args>` in-process and stream its output to q."""
    old_argv, old_cwd = sys.argv, os.getcwd()
    code = 0
    try:
        os.chdir(cwd)  # so a relative .dir2txtignore / defaults resolve sensibly
        sys.argv = ["dir2txt", *args]
        writer = QueueWriter(q)
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            try:
                runpy.run_module("dir2txt", run_name="__main__")
            except SystemExit as e:  # argparse / sys.exit()
                if e.code is None:
                    code = 0
                elif isinstance(e.code, int):
                    code = e.code
                else:
                    q.put(("log", f"{e.code}\n"))
                    code = 1
    except Exception as e:  # noqa: BLE001
        q.put(("log", f"\nERROR: {type(e).__name__}: {e}\n"))
        code = 1
    finally:
        sys.argv = old_argv
        try:
            os.chdir(old_cwd)
        except OSError:
            pass
        q.put(("done", code))


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class App(BaseTk):
    def __init__(self, initial_dir: str = ""):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("780x640")
        self.minsize(640, 520)
        try:
            self.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass

        self.q: "queue.Queue" = queue.Queue()
        self.worker = None
        self.out_is_auto = True

        self.var_in = tk.StringVar()
        self.var_out = tk.StringVar()
        self.var_desc = tk.StringVar()
        self.var_ext = tk.StringVar()
        self.var_status = tk.StringVar(value="Ready.")

        self._load_settings()
        if initial_dir and os.path.isdir(initial_dir):
            self.var_in.set(os.path.abspath(initial_dir))
        self._build_ui()
        self.var_in.trace_add("write", self._on_input_changed)
        self._on_input_changed()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)

    # ---- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 5}
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(6, weight=1)

        hint = "Drag a folder here or click Browse" if HAS_DND else "Click Browse to choose a folder"
        ttk.Label(root, text="Project folder").grid(row=0, column=0, sticky="w", **pad)
        self.ent_in = ttk.Entry(root, textvariable=self.var_in)
        self.ent_in.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Browse…", command=self._browse_in).grid(row=0, column=2, **pad)
        ttk.Label(root, text=hint, foreground="gray").grid(row=1, column=1, sticky="w", padx=10)

        ttk.Label(root, text="Output file").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.var_out).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Save as…", command=self._browse_out).grid(row=2, column=2, **pad)

        ttk.Label(root, text="Description").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.var_desc).grid(row=3, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(root, text="Ignore extensions").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.var_ext).grid(row=4, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Edit .dir2txtignore", command=self._edit_ignore).grid(row=4, column=2, **pad)
        ttk.Label(root, text="e.g.  pt, onnx, dcm", foreground="gray").grid(row=5, column=1, sticky="w", padx=10)

        # log
        log_frame = ttk.Frame(root)
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew", padx=10, pady=(10, 5))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=10, wrap="none", state="disabled", font=("Consolas", 9))
        sb_y = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        sb_x = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")

        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew", padx=10, pady=5)

        btns = ttk.Frame(root)
        btns.grid(row=8, column=0, columnspan=3, sticky="ew", padx=10, pady=5)
        self.btn_run = ttk.Button(btns, text="▶  Convert", command=self._start)
        self.btn_run.pack(side="left")
        self.btn_copy = ttk.Button(btns, text="Copy content", command=self._copy_result, state="disabled")
        self.btn_copy.pack(side="left", padx=(8, 0))
        self.btn_open = ttk.Button(btns, text="Open file", command=self._open_result, state="disabled")
        self.btn_open.pack(side="left", padx=(8, 0))
        self.btn_folder = ttk.Button(btns, text="Show in folder", command=self._open_folder, state="disabled")
        self.btn_folder.pack(side="left", padx=(8, 0))

        ttk.Label(root, textvariable=self.var_status, anchor="w").grid(
            row=9, column=0, columnspan=3, sticky="ew", padx=10, pady=(5, 0)
        )

        if HAS_DND:
            for widget in (self.ent_in, self.log):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)

    # ---- settings ----------------------------------------------------------
    def _load_settings(self) -> None:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            last = data.get("last_input", "")
            if last and os.path.isdir(last):
                self.var_in.set(last)
            self.var_ext.set(data.get("ignore_ext", ""))
            self.var_desc.set(data.get("description", ""))
        except Exception:
            pass

    def _save_settings(self) -> None:
        try:
            SETTINGS_FILE.write_text(
                json.dumps(
                    {
                        "last_input": self.var_in.get(),
                        "ignore_ext": self.var_ext.get(),
                        "description": self.var_desc.get(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _on_close(self) -> None:
        self._save_settings()
        self.destroy()

    # ---- input / output handling ------------------------------------------
    def _on_drop(self, event) -> None:
        items = self.tk.splitlist(event.data)
        if not items:
            return
        p = items[0]
        if os.path.isfile(p):
            p = os.path.dirname(p)
        self.var_in.set(os.path.abspath(p))

    def _browse_in(self) -> None:
        d = filedialog.askdirectory(title="Select project folder", initialdir=self.var_in.get() or None)
        if d:
            self.var_in.set(os.path.abspath(d))

    def _browse_out(self) -> None:
        f = filedialog.asksaveasfilename(
            title="Save output as",
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
            initialfile=os.path.basename(self.var_out.get()) or "project.txt",
            initialdir=os.path.dirname(self.var_out.get()) or None,
        )
        if f:
            self.out_is_auto = False
            self.var_out.set(os.path.abspath(f))

    def _on_input_changed(self, *_):
        """Suggest an output path next to the project folder (not inside it,
        so the next run never re-reads the previous output)."""
        if not self.out_is_auto:
            return
        src = self.var_in.get().strip()
        if src and os.path.isdir(src):
            p = Path(src).resolve()
            parent = p.parent if p.parent != p else Path.home()
            name = p.name or "project"
            self.var_out.set(str(parent / f"{name}_dir2txt.txt"))
        else:
            self.var_out.set("")

    def _edit_ignore(self) -> None:
        src = self.var_in.get().strip()
        if not os.path.isdir(src):
            messagebox.showwarning(APP_TITLE, "Choose a project folder first.")
            return
        path = os.path.join(src, ".dir2txtignore")
        if not os.path.exists(path):
            if not messagebox.askyesno(APP_TITLE, "No .dir2txtignore found here.\nCreate one with common defaults?"):
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(IGNORE_TEMPLATE)
        try:
            open_in_os(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not open the file:\n{e}")

    # ---- running -----------------------------------------------------------
    def _log(self, text: str, clear: bool = False) -> None:
        self.log.configure(state="normal")
        if clear:
            self.log.delete("1.0", "end")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _build_args(self) -> list:
        args = ["-i", os.path.abspath(self.var_in.get().strip()), "-o", os.path.abspath(self.var_out.get().strip())]
        desc = self.var_desc.get().strip()
        if desc:
            args += ["-d", desc]
        exts = [e.strip().lstrip(".") for e in self.var_ext.get().replace(",", " ").split()]
        exts = [e for e in exts if e]
        if exts:
            args += ["-e", *exts]
        return args

    def _start(self) -> None:
        src = self.var_in.get().strip()
        out = self.var_out.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showwarning(APP_TITLE, "Please choose a valid project folder.")
            return
        if not out:
            messagebox.showwarning(APP_TITLE, "Please choose an output file.")
            return
        out_dir = os.path.dirname(os.path.abspath(out))
        if not os.path.isdir(out_dir):
            messagebox.showwarning(APP_TITLE, f"Output folder does not exist:\n{out_dir}")
            return
        if os.path.exists(out) and not messagebox.askyesno(APP_TITLE, "Output file already exists. Overwrite?"):
            return

        self._save_settings()
        self._set_running(True)
        self._log(f"> dir2txt {' '.join(self._build_args())}\n\n", clear=True)
        self.worker = threading.Thread(
            target=run_dir2txt, args=(self._build_args(), os.path.abspath(src), self.q), daemon=True
        )
        self.worker.start()

    def _set_running(self, running: bool) -> None:
        self.btn_run.configure(state="disabled" if running else "normal")
        if running:
            for b in (self.btn_copy, self.btn_open, self.btn_folder):
                b.configure(state="disabled")
            self.progress.start(12)
            self.var_status.set("Working…")
        else:
            self.progress.stop()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    self._finished(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _finished(self, code: int) -> None:
        self._set_running(False)
        out = self.var_out.get().strip()
        if code == 0 and os.path.isfile(out):
            size = os.path.getsize(out)
            self.var_status.set(f"Done  •  {human_size(size)}  •  ~{size // 4:,} tokens (rough estimate)")
            for b in (self.btn_copy, self.btn_open, self.btn_folder):
                b.configure(state="normal")
        else:
            self.var_status.set(f"Failed (exit code {code}). See the log above.")

    # ---- result actions ----------------------------------------------------
    def _copy_result(self) -> None:
        out = self.var_out.get().strip()
        try:
            if os.path.getsize(out) > 30 * 1024 * 1024:
                messagebox.showwarning(APP_TITLE, "File is larger than 30 MB. Use the file directly instead.")
                return
            with open(out, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()
            self.var_status.set("Copied to clipboard.")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(e))

    def _open_result(self) -> None:
        try:
            open_in_os(self.var_out.get().strip())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(e))

    def _open_folder(self) -> None:
        out = os.path.abspath(self.var_out.get().strip())
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", out])
            else:
                open_in_os(os.path.dirname(out))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(e))


def main() -> None:
    # Sharper text on high-DPI Windows screens.
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    # Optional: `gui.exe "C:\path\to\project"` pre-fills the folder
    # (handy for a right-click "Open with" / context-menu entry).
    initial = sys.argv[1] if len(sys.argv) > 1 else ""
    App(initial).mainloop()


if __name__ == "__main__":
    main()
