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
    """Zentrierter Ein/Aus-Schiebeschalter im iOS-Stil, mit weicher Slide-
    Animation, Knopfschatten und Hover. Ein Klick ruft `command(neuer_zustand)`
    auf; programmatisches Setzen via set_state() löst `command` bewusst NICHT aus.

    Gezeichnet mit gestapelten Ovalen (kein echtes Anti-Aliasing im Tk-Canvas,
    aber durch einen dünnen, leicht helleren Rand wirken die Kanten ruhiger)."""

    # Farbpaletten je Zustand: (Track, Track-Rand)
    _TRACK_ON = ("#22c55e", "#16a34a")
    _TRACK_ON_HOVER = ("#16a34a", "#15803d")
    _TRACK_OFF = ("#cbd5e1", "#b6c2d1")
    _TRACK_OFF_HOVER = ("#b8c2d0", "#a3b0c0")
    _TRACK_DISABLED = ("#e5e7eb", "#e5e7eb")

    def __init__(self, master, command=None, width=176, height=68, **kw):
        bg = "#f0f0f0"
        try:
            bg = ttk.Style().lookup("TFrame", "background") or bg
        except tk.TclError:
            pass
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, bd=0, bg=bg, **kw)
        self._cw, self._ch = width, height
        self._on = False
        self._enabled = True
        self._hover = False
        self._command = command
        self._pos = 0.0          # 0.0 = aus (Knopf links) … 1.0 = an (rechts)
        self._anim = None
        self.configure(cursor="hand2")
        self.bind("<Button-1>", self._clicked)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._render()

    # ── Zeichnen ────────────────────────────────────────────────────────────

    def _track_colors(self):
        if not self._enabled:
            return self._TRACK_DISABLED
        if self._on:
            return self._TRACK_ON_HOVER if self._hover else self._TRACK_ON
        return self._TRACK_OFF_HOVER if self._hover else self._TRACK_OFF

    def _pill(self, x0, y0, x1, y1, fill, outline):
        r = (y1 - y0) / 2
        self.create_oval(x0, y0, x0 + 2 * r, y1, fill=fill, outline=outline)
        self.create_oval(x1 - 2 * r, y0, x1, y1, fill=fill, outline=outline)
        self.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill)
        # obere/untere Kante nachziehen, damit der Rand rundum gleich wirkt
        self.create_line(x0 + r, y0, x1 - r, y0, fill=outline)
        self.create_line(x0 + r, y1, x1 - r, y1, fill=outline)

    def _render(self):
        self.delete("all")
        p = 6
        w, h = self._cw, self._ch
        r = (h - 2 * p) / 2
        track, track_edge = self._track_colors()
        self._pill(p, p, w - p, h - p, track, track_edge)

        # Beschriftung gegenüber dem Knopf (bleibt lesbar, wandert nicht mit)
        if self._enabled:
            if self._pos >= 0.5:
                self.create_text(p + r + 4, h / 2, text="AN", anchor="w",
                                 fill="white", font=("Segoe UI", int(r * 0.62), "bold"))
            else:
                self.create_text(w - p - r - 4, h / 2, text="AUS", anchor="e",
                                 fill="#5b6472", font=("Segoe UI", int(r * 0.62), "bold"))

        # Knopf mit weichem Schatten
        travel = (w - 2 * p) - 2 * r
        kx = p + self._pos * travel
        knob = "#f9fafb" if self._enabled else "#f3f4f6"
        ring = "#cbd5e1" if self._enabled else "#e5e7eb"
        self.create_oval(kx + 1.5, p + 2.5, kx + 2 * r + 1.5, h - p + 2.5,
                         fill="#c9cdd4", outline="")             # weicher Schatten
                                                                 # (Tk kennt kein Alpha)
        self.create_oval(kx, p, kx + 2 * r, h - p, fill=knob, outline=ring)
        self.create_oval(kx + r * 0.62, p + r * 0.62,
                         kx + 2 * r - r * 0.62, h - p - r * 0.62,
                         fill="", outline=ring)                  # feiner Innenring

    # ── Animation ───────────────────────────────────────────────────────────

    def _animate_to(self, target):
        if self._anim is not None:
            self.after_cancel(self._anim)
            self._anim = None

        def step():
            d = target - self._pos
            if abs(d) < 0.05:
                self._pos = target
                self._render()
                self._anim = None
                return
            self._pos += d * 0.34          # ease-out
            self._render()
            self._anim = self.after(16, step)

        step()

    # ── Ereignisse ──────────────────────────────────────────────────────────

    def _clicked(self, _evt):
        if not self._enabled or self._command is None:
            return
        self._command(not self._on)

    def _set_hover(self, hover):
        self._hover = hover
        self._render()

    # ── öffentliche API ─────────────────────────────────────────────────────

    def set_state(self, on: bool, animate: bool = True):
        self._on = bool(on)
        target = 1.0 if self._on else 0.0
        if animate and self.winfo_ismapped():
            self._animate_to(target)
        else:
            self._pos = target
            self._render()

    def get_state(self) -> bool:
        return self._on

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
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
        self.switch = ToggleSwitch(frm, command=self.on_toggle, width=176, height=68)
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
