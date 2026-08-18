"""
gui.py – Grafische Oberfläche für den Fritz!Box-2FA-Umschalter
================================================================
tkinter-only (Standardbibliothek, keine externen Pakete), damit sich alles mit
PyInstaller zu einer einzelnen .exe bündeln lässt. Die eigentliche Logik liegt
in session.py – hier ist nur die Bedienoberfläche.

Ablauf:
  1. FRITZ!Box-Adresse eingeben → Benutzernamen werden automatisch geladen.
  2. Benutzer wählen, Kennwort (und optional TOTP-Secret) eingeben.
  3. "Verbinden" liest den aktuellen 2FA-Zustand und stellt den Schiebeschalter.
  4. Schiebeschalter umlegen → schaltet 2FA an/aus (reboot-frei), fragt bei
     Bedarf die Bestätigung an der Box an und verifiziert das Ergebnis.

Threading: jeder Netzaufruf läuft in einem Hintergrund-Thread (tkinter ist
nicht threadsicher). Threads reden mit der GUI ausschließlich über eine Queue,
die der GUI-Thread per root.after() pollt.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from . import session


# ── Schiebeschalter (Canvas-Widget im iOS-Stil) ─────────────────────────────

class ToggleSwitch(tk.Canvas):
    """Ein zentrierter Ein/Aus-Schiebeschalter. Ein Klick ruft `command(neuer_zustand)`
    auf; programmatisches Setzen via set_state() löst `command` bewusst NICHT aus."""

    def __init__(self, master, command=None, width=150, height=60, **kw):
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, bd=0, **kw)
        self._cw, self._ch = width, height
        self._on = False
        self._enabled = True
        self._command = command
        self.bind("<Button-1>", self._clicked)
        self._render()

    def _colors(self):
        if not self._enabled:
            return "#4b5563", "#9ca3af"      # track, knob (ausgegraut)
        return ("#22c55e" if self._on else "#9aa3af"), "#ffffff"

    def _render(self):
        self.delete("all")
        p = 5
        w, h = self._cw, self._ch
        r = (h - 2 * p) / 2
        track, knob = self._colors()
        # Pille: zwei Kreise + Rechteck
        self.create_oval(p, p, p + 2 * r, h - p, fill=track, outline=track)
        self.create_oval(w - p - 2 * r, p, w - p, h - p, fill=track, outline=track)
        self.create_rectangle(p + r, p, w - p - r, h - p, fill=track, outline=track)
        # Knopf links (aus) oder rechts (an)
        kx = (w - p - 2 * r) if self._on else p
        self.create_oval(kx, p, kx + 2 * r, h - p, fill=knob, outline="#e5e7eb")
        # Beschriftung im Track
        label = "AN" if self._on else "AUS"
        tx = p + r if self._on else w - p - r
        self.create_text(tx, h / 2, text=label, fill="white",
                         font=("Segoe UI", int(r * 0.7), "bold"))

    def _clicked(self, _evt):
        if not self._enabled or self._command is None:
            return
        self._command(not self._on)

    # öffentliche API
    def set_state(self, on: bool):
        self._on = bool(on)
        self._render()

    def get_state(self) -> bool:
        return self._on

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self._render()


# ── Queue-Nachrichtentypen (Thread → GUI) ───────────────────────────────────

_LOG = "log"
_STATUS = "status"         # (text)
_USERS = "users"           # (list[str])
_STATE = "state"           # (on: bool, connected: bool) – Schalter setzen
_BUSY = "busy"             # (busy: bool) – Eingaben sperren/entsperren


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.connected = False

        root.title("Fritz!Box – Zusätzliche Bestätigung (2FA)")
        root.minsize(560, 560)

        frm = ttk.Frame(root, padding=14)
        frm.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

        self.host = tk.StringVar(value="192.168.0.1")
        self.user = tk.StringVar()
        self.password = tk.StringVar()
        self.totp = tk.StringVar()

        r = 0
        ttk.Label(frm, text="FRITZ!Box-Adresse").grid(row=r, column=0, sticky="w", pady=3)
        host_entry = ttk.Entry(frm, textvariable=self.host)
        host_entry.grid(row=r, column=1, sticky="ew", pady=3)
        host_entry.bind("<FocusOut>", lambda e: self.load_users())
        host_entry.bind("<Return>", lambda e: self.load_users())
        r += 1

        ttk.Label(frm, text="Benutzername").grid(row=r, column=0, sticky="w", pady=3)
        self.user_box = ttk.Combobox(frm, textvariable=self.user, values=[])
        self.user_box.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1

        ttk.Label(frm, text="Kennwort").grid(row=r, column=0, sticky="w", pady=3)
        pw_entry = ttk.Entry(frm, textvariable=self.password, show="•")
        pw_entry.grid(row=r, column=1, sticky="ew", pady=3)
        pw_entry.bind("<Return>", lambda e: self.connect())
        r += 1

        ttk.Label(frm, text="TOTP-Secret (optional)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.totp, show="•").grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(frm, text="nur nötig, wenn die Box ausschließlich\nGoogle Authenticator anbietet",
                  foreground="#6b7280").grid(row=r, column=1, sticky="w")
        r += 1

        self.connect_btn = ttk.Button(frm, text="Verbinden / Status abfragen", command=self.connect)
        self.connect_btn.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        r += 1

        ttk.Separator(frm, orient="horizontal").grid(row=r, column=0, columnspan=2, sticky="ew", pady=8)
        r += 1

        # Zentraler, großer Schiebeschalter
        self.headline = ttk.Label(frm, text="Zusätzliche Bestätigung (2FA)",
                                  font=("Segoe UI", 11, "bold"))
        self.headline.grid(row=r, column=0, columnspan=2)
        r += 1
        self.switch = ToggleSwitch(frm, command=self.on_toggle, width=160, height=64)
        self.switch.grid(row=r, column=0, columnspan=2, pady=10)
        self.switch.set_enabled(False)
        r += 1
        self.state_lbl = ttk.Label(frm, text="Noch nicht verbunden.", foreground="#6b7280")
        self.state_lbl.grid(row=r, column=0, columnspan=2)
        r += 1

        self.status = tk.StringVar(value="Adresse eingeben – die Benutzer werden dann geladen.")
        ttk.Label(frm, textvariable=self.status, foreground="#0a58ca").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
        r += 1

        self.log = tk.Text(frm, height=9, wrap="word", state="disabled",
                           background="#111827", foreground="#e5e7eb", insertbackground="#e5e7eb")
        self.log.grid(row=r, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        frm.rowconfigure(r, weight=1)

        self.root.after(100, self._drain_queue)
        # Benutzer gleich beim Start laden (Standard-IP)
        self.root.after(200, self.load_users)

    # ── GUI-Thread-Helfer ───────────────────────────────────────────────────

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", str(text) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.connect_btn.configure(state=state)
        self.switch.set_enabled(self.connected and not busy)

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == _LOG:
                    self._log(payload)
                elif kind == _STATUS:
                    self.status.set(payload)
                elif kind == _USERS:
                    users = payload
                    self.user_box.configure(values=users)
                    if users and not self.user.get():
                        self.user.set(users[0])
                elif kind == _STATE:
                    on, connected = payload
                    self.connected = connected
                    self.switch.set_state(on)
                    self.switch.set_enabled(connected)
                    self.state_lbl.configure(
                        text=("2FA ist derzeit " + ("AN" if on else "AUS")) if connected
                        else "Noch nicht verbunden.",
                        foreground=("#16a34a" if (connected and on) else
                                    "#6b7280" if not connected else "#b45309"))
                elif kind == _BUSY:
                    self._set_busy(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _post(self, kind, payload=None):
        self.q.put((kind, payload))

    # ── Aktionen (starten Hintergrund-Threads) ──────────────────────────────

    def _run_bg(self, target, **kw):
        if self.worker and self.worker.is_alive():
            return False
        self.worker = threading.Thread(target=target, kwargs=kw, daemon=True)
        self.worker.start()
        return True

    def load_users(self):
        host = self.host.get().strip()
        if not host:
            return
        self._run_bg(self._work_load_users, host=host)

    def connect(self):
        if not self.user.get().strip() or not self.password.get():
            self.status.set("Bitte Benutzername und Kennwort eingeben.")
            return
        self._post(_BUSY, True)
        self.status.set("Verbinde…")
        self._run_bg(self._work_connect,
                     host=self.host.get().strip(), user=self.user.get().strip(),
                     password=self.password.get())

    def on_toggle(self, desired: bool):
        if not self.connected:
            return
        self._post(_BUSY, True)
        self.status.set(f"Schalte 2FA {'AN' if desired else 'AUS'} … bei Aufforderung an der Box bestätigen.")
        self._run_bg(self._work_toggle,
                     host=self.host.get().strip(), user=self.user.get().strip(),
                     password=self.password.get(), totp=self.totp.get().strip() or None,
                     desired=desired)

    # ── Hintergrund-Arbeit (nur über self.q mit der GUI reden) ───────────────

    def _work_load_users(self, host):
        try:
            users = session.list_users(host)
            if users:
                self._post(_USERS, users)
                self._post(_STATUS, f"{len(users)} Benutzer geladen – Kennwort eingeben und verbinden.")
            else:
                self._post(_STATUS, "Keine Benutzer gefunden (Adresse prüfen).")
        except Exception as e:  # noqa: BLE001
            self._post(_STATUS, f"Benutzer konnten nicht geladen werden: {e}")

    def _work_connect(self, host, user, password):
        try:
            sid = session.login(host, user, password)
            on = session.tfa_needed(host, sid, "sysSave")
            self._post(_LOG, f"Verbunden (SID {sid}). 2FA ist aktuell {'AN' if on else 'AUS'}.")
            self._post(_STATE, (on, True))
            self._post(_STATUS, "Verbunden. Schalter umlegen, um 2FA zu ändern.")
        except session.FritzBoxLoginError:
            self._post(_LOG, "Anmeldung fehlgeschlagen – Benutzername/Kennwort prüfen.")
            self._post(_STATUS, "Anmeldung fehlgeschlagen.")
            self._post(_STATE, (False, False))
        except Exception as e:  # noqa: BLE001
            self._post(_LOG, f"Fehler: {e}")
            self._post(_STATUS, "Fehler beim Verbinden.")
            self._post(_STATE, (False, False))
        finally:
            self._post(_BUSY, False)

    def _work_toggle(self, host, user, password, totp, desired):
        def on_prompt(state):
            hint = ", ".join(state.methods)
            extra = f"  (DTMF-Code: {state.dtmf_code})" if state.dtmf_code else ""
            self._post(_LOG, f"► Bitte JETZT an der FRITZ!Box bestätigen — Methoden: {hint}{extra}")

        try:
            sid = session.login(host, user, password)
            self._post(_LOG, f"'Zusätzliche Bestätigung' wird {'aktiviert' if desired else 'deaktiviert'}…")
            if session.tfa_needed(host, sid, "sysSave"):
                session.run_twofactor(host, sid, on_prompt=on_prompt, totp_secret=totp)
                self._post(_LOG, "Bestätigt, sende Umschaltung…")
            else:
                self._post(_LOG, "Aktuell keine Bestätigung nötig, sende direkt…")
            session.set_additional_confirmation(host, sid, desired)

            # Verifikation mit frischer Anmeldung (umgeht das 2FA-Vertrauensfenster).
            vsid = session.login(host, user, password)
            actual = session.tfa_needed(host, vsid, "sysSave")
            self._post(_STATE, (actual, True))
            if actual == desired:
                self._post(_LOG, f"✓ Verifiziert: 2FA ist jetzt {'AN' if actual else 'AUS'} (reboot-frei).")
                self._post(_STATUS, "Fertig.")
            else:
                self._post(_LOG, "WARNUNG: Zustand passt nicht zum Wunsch — bitte erneut prüfen.")
                self._post(_STATUS, "Umschaltung nicht bestätigt.")
        except session.TwoFactorTimeout:
            self._post(_LOG, "Zeitüberschreitung: an der Box wurde nicht rechtzeitig bestätigt.")
            self._post(_STATUS, "Abgebrochen (Timeout).")
            self._refresh_state(host, user, password)
        except session.TwoFactorRejected as e:
            self._post(_LOG, f"Bestätigung abgelehnt: {e}")
            self._post(_STATUS, "Bestätigung abgelehnt.")
            self._refresh_state(host, user, password)
        except session.TwoFactorBusy as e:
            self._post(_LOG, str(e))
            self._post(_STATUS, "Box gerade gesperrt – später erneut.")
            self._refresh_state(host, user, password)
        except Exception as e:  # noqa: BLE001
            self._post(_LOG, f"Fehler: {e}")
            self._post(_STATUS, "Fehler.")
            self._refresh_state(host, user, password)
        finally:
            self._post(_BUSY, False)

    def _refresh_state(self, host, user, password):
        """Nach einem Fehlschlag den Schalter wieder auf den echten Ist-Zustand
        setzen (statt auf dem gewünschten stehenzubleiben)."""
        try:
            sid = session.login(host, user, password)
            self._post(_STATE, (session.tfa_needed(host, sid, "sysSave"), True))
        except Exception:  # noqa: BLE001
            pass


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
