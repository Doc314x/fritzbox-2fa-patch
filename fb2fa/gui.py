"""
gui.py – Grafische Oberfläche für den Fritz!Box-2FA-Umschalter
================================================================
tkinter-only (Teil der Python-Standardbibliothek, keine externen Pakete),
damit sich das Ganze mit PyInstaller zu einer einzelnen .exe bündeln lässt.

Die eigentliche Logik liegt komplett in session.py – diese Datei ist nur die
Bedienoberfläche: Eingabefelder (Host, Benutzer, Kennwort, optional TOTP-
Secret), ein An/Aus-Schalter und ein Protokollfenster.

Wichtig fürs Nicht-Blockieren: der Netzwerk-/Bestätigungsfluss läuft in einem
Hintergrund-Thread. Er darf tkinter-Widgets NICHT direkt anfassen (tkinter ist
nicht threadsicher) – stattdessen schiebt er Meldungen in eine Queue, die der
GUI-Thread per root.after() pollt und ins Protokoll schreibt.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from . import session


class _Msg:
    """Kleine Nachrichtentypen für die Thread→GUI-Queue."""
    LOG = "log"
    DONE = "done"     # (erfolg: bool, text: str)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker: threading.Thread | None = None

        root.title("Fritz!Box – Zusätzliche Bestätigung (2FA)")
        root.minsize(560, 460)

        frm = ttk.Frame(root, padding=12)
        frm.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

        self.host = tk.StringVar(value="192.168.0.1")
        self.user = tk.StringVar()
        self.password = tk.StringVar()
        self.totp = tk.StringVar()
        self.enable = tk.BooleanVar(value=False)  # Standard: deaktivieren

        r = 0
        ttk.Label(frm, text="FRITZ!Box-Adresse").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.host).grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(frm, text="Benutzername").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.user).grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(frm, text="Kennwort").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.password, show="•").grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(frm, text="TOTP-Secret (optional)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.totp, show="•").grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(frm, text="nur nötig, wenn die Box ausschließlich\nGoogle Authenticator anbietet",
                  foreground="#666").grid(row=r, column=1, sticky="w")
        r += 1

        sep = ttk.Separator(frm, orient="horizontal")
        sep.grid(row=r, column=0, columnspan=2, sticky="ew", pady=8)
        r += 1

        ttk.Checkbutton(
            frm, text="2FA aktivieren (Haken aus = deaktivieren)",
            variable=self.enable,
        ).grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
        r += 1

        self.run_btn = ttk.Button(frm, text="Ausführen", command=self.on_run)
        self.run_btn.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 8))
        r += 1

        self.status = tk.StringVar(value="Bereit.")
        ttk.Label(frm, textvariable=self.status, foreground="#0a58ca").grid(
            row=r, column=0, columnspan=2, sticky="w")
        r += 1

        self.log = tk.Text(frm, height=12, wrap="word", state="disabled",
                           background="#111", foreground="#eee", insertbackground="#eee")
        self.log.grid(row=r, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        frm.rowconfigure(r, weight=1)

        self.root.after(100, self._drain_queue)

    # ── GUI-Helfer (nur aus dem GUI-Thread aufrufen) ────────────────────────

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == _Msg.LOG:
                    self._log(str(payload))
                elif kind == _Msg.DONE:
                    ok, text = payload  # type: ignore[misc]
                    self._log(text)
                    self.status.set("Fertig." if ok else "Fehlgeschlagen.")
                    self.run_btn.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    # ── Aktion ──────────────────────────────────────────────────────────────

    def on_run(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.user.get().strip() or not self.password.get():
            self.status.set("Bitte Benutzername und Kennwort eingeben.")
            return
        self.run_btn.configure(state="disabled")
        self.status.set("Läuft… bei Aufforderung bitte an der Box bestätigen.")
        params = dict(
            host=self.host.get().strip(),
            user=self.user.get().strip(),
            password=self.password.get(),
            totp=self.totp.get().strip() or None,
            enable=self.enable.get(),
        )
        self.worker = threading.Thread(target=self._work, kwargs=params, daemon=True)
        self.worker.start()

    def _work(self, host, user, password, totp, enable):
        """Läuft im Hintergrund-Thread – nur über self.q mit der GUI reden."""
        def log(msg):
            self.q.put((_Msg.LOG, msg))

        def on_prompt(state):
            hint = ", ".join(state.methods)
            extra = f"  (DTMF-Code: {state.dtmf_code})" if state.dtmf_code else ""
            log(f"► Bitte JETZT an der FRITZ!Box bestätigen — Methoden: {hint}{extra}")

        try:
            sid = session.login(host, user, password)
            log(f"Angemeldet (SID {sid}).")
            action = "aktivieren" if enable else "deaktivieren"
            log(f"'Zusätzliche Bestätigung' wird {action}…")

            if session.tfa_needed(host, sid, "sysSave"):
                session.run_twofactor(host, sid, on_prompt=on_prompt, totp_secret=totp)
                log("Bestätigt, sende Umschaltung…")
            else:
                log("Aktuell keine Bestätigung nötig (2FA derzeit aus), sende direkt…")
            session.set_additional_confirmation(host, sid, enable)

            # Verifikation mit frischer Anmeldung (umgeht das 2FA-Vertrauensfenster).
            verify_sid = session.login(host, user, password)
            still = session.tfa_needed(host, verify_sid, "sysSave")
            if still == enable:
                self.q.put((_Msg.DONE, (True,
                    "✓ Umschaltung verifiziert (Zustand passt). Reboot-frei erledigt.")))
            else:
                self.q.put((_Msg.DONE, (False,
                    "WARNUNG: tfa_needed meldet weiterhin den alten Zustand — bitte manuell prüfen.")))
        except session.TwoFactorTimeout:
            self.q.put((_Msg.DONE, (False,
                "Zeitüberschreitung: an der Box wurde nicht rechtzeitig bestätigt.")))
        except session.TwoFactorBusy as e:
            self.q.put((_Msg.DONE, (False, str(e))))
        except session.FritzBoxError as e:
            self.q.put((_Msg.DONE, (False, f"Fehler: {e}")))
        except Exception as e:  # noqa: BLE001 – letzte Sicherung für die GUI
            self.q.put((_Msg.DONE, (False, f"Unerwarteter Fehler: {e!r}")))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
