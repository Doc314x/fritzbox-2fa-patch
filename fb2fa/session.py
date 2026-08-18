"""
session.py – Fritz!Box-Login, 2FA-Bestätigungsfluss, Export/Import-Requests
=============================================================================
Stdlib-only (wie das analoge fritzbox_client.py im imap-mover-Projekt), damit
dieses Tool ohne pip-Installation läuft.

Drei Bausteine:
  1. login()            – PBKDF2-Challenge-Response (login_sid.lua Version 2)
  2. Zwei-Faktor-Fluss   – tfa_needed() / TwoFactorFlow, Nachbau von
     /js/twofactor.js: erst /twofactor.lua mit tfa_start anstoßen, dann mit
     tfa_active pollen, bis der Nutzer an der Box bestätigt hat. ERST DANACH
     ist die anschließende firmwarecfg-Anfrage innerhalb eines beobachteten
     Vertrauensfensters ohne erneute Bestätigung erfolgreich.
  3. export_config() / import_config_all() / start_selective_import() /
     confirm_selective_import() – die eigentlichen firmwarecfg-Aufrufe, exakt
     nach den Feldnamen aus system/export.js bzw. system/import.js der Box
     (nicht geraten, sondern aus deren eigenem, ausgeliefertem JS gelesen).

WARNUNG: export_config()/import_config_all()/... wurden bislang NICHT gegen
eine echte Box in einem vollständigen Lauf verifiziert (der Export bricht
ohne 2FA-Bestätigung mit leerer Antwort ab, siehe README). Vor produktivem
Einsatz: erst mit --dry-run/list-vars gegen eine echte exportierte Datei
prüfen, danach den Import-Pfad mit Bedacht (Sicherungskopie!) testen.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable

from . import totp


class FritzBoxError(Exception):
    pass


class FritzBoxLoginError(FritzBoxError):
    pass


class TwoFactorTimeout(FritzBoxError):
    """Der Nutzer hat innerhalb des Zeitlimits nicht an der Box bestätigt."""


class TwoFactorRejected(FritzBoxError):
    """Die Bestätigung wurde abgebrochen oder war falsch (z. B. Google-Authenticator-Code)."""


class TwoFactorBusy(FritzBoxError):
    """starterror-Antwort der Box: bereits ein Bestätigungsvorgang aktiv
    (Code 91, ~2 Min. warten) oder DoS-Sperre nach 3 Fehlversuchen (Code 92,
    60 Min. warten). Siehe dlgStartFailureOtherSession()/dlgStartFailureDos()
    in /js/twofactor.js – KEIN normaler Bestätigungs-Prompt, unbedingt von
    den echten Methoden (button/googleauth/dtmf) unterscheiden, sonst wird
    fälschlich auf eine Bestätigung gewartet, die nie kommt."""

    def __init__(self, code: int):
        self.code = code
        wait = "~2 Minuten" if code == 91 else "60 Minuten" if code == 92 else "unbekannt"
        super().__init__(f"Box lehnt neue Bestätigung ab (Code {code}), bitte {wait} warten.")


# ── HTTP-Grundlagen ──────────────────────────────────────────────────────────

def _get(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _post_form(url: str, data: dict, timeout: float) -> str:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _post_multipart(url: str, text_fields: dict, file_field: tuple[str, str, bytes] | None,
                     timeout: float) -> tuple[int, bytes]:
    """POST als multipart/form-data. file_field = (feldname, dateiname, inhalt) oder None.

    Gibt (status, rohe_antwort_bytes) zurück – Export liefert die Konfig-
    Datei direkt als Body, kein JSON, daher bytes statt str.
    """
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in text_fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    if file_field is not None:
        fname, filename, content = file_field
        header = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{fname}"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode("utf-8")
        parts.append(header + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _is_login_page(body: str) -> bool:
    return '"sid":"0000000000000000"' in body or body.lstrip()[:15].lower().startswith("<!doctype html")


# ── Login ─────────────────────────────────────────────────────────────────

def login(host: str, user: str, password: str, timeout: float = 10) -> str:
    challenge_url = f"http://{host}/login_sid.lua?version=2"
    xml = _get(challenge_url, timeout)

    m = re.search(r"<Challenge>(.*?)</Challenge>", xml)
    if not m:
        raise FritzBoxError(f"Challenge-Antwort der Box nicht verstanden: {xml[:300]!r}")
    challenge = m.group(1)

    parts = challenge.split("$")
    if len(parts) != 5 or parts[0] != "2":
        raise FritzBoxError(
            f"Unerwartetes Challenge-Format: {challenge!r} (nur PBKDF2 wird unterstützt)"
        )
    _, iter1, salt1, iter2, salt2 = parts
    salt1b, salt2b = bytes.fromhex(salt1), bytes.fromhex(salt2)
    hash1 = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt1b, int(iter1))
    hash2 = hashlib.pbkdf2_hmac("sha256", hash1, salt2b, int(iter2))
    response = f"{salt2}${hash2.hex()}"

    body = _post_form(challenge_url, {"username": user, "response": response}, timeout)
    m = re.search(r"<SID>(.*?)</SID>", body)
    if not m or m.group(1) == "0000000000000000":
        raise FritzBoxLoginError("Anmeldung fehlgeschlagen (Benutzername/Kennwort prüfen).")
    return m.group(1)


# ── tfaNeeded-Check (aus data.lua-Seitendaten, wie es export.js/import.js tun) ──

def tfa_needed(host: str, sid: str, page: str, timeout: float = 10) -> bool:
    """page: 'sysSave' (Export) oder 'sysImp' (Import)."""
    body = _post_form(f"http://{host}/data.lua",
                       {"sid": sid, "page": page, "xhr": "1", "lang": "de"}, timeout)
    if _is_login_page(body):
        raise FritzBoxLoginError("Sitzung abgelaufen, bitte erneut anmelden.")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise FritzBoxError(f"Unerwartete data.lua-Antwort: {body[:300]!r}")
    return bool(data.get("data", {}).get("tfaNeeded"))


# ── Zwei-Faktor-Bestätigungsfluss (Nachbau von /js/twofactor.js) ────────────

@dataclass
class TwoFactorState:
    methods: list[str]
    dtmf_code: str | None


def _split_state(state: str) -> TwoFactorState:
    # Format wie in twofactor.js splitState(): "button,googleauth,dtmf;1234"
    # ACHTUNG: "starterror;91" hat dieselbe Syntax, ist aber KEIN
    # Bestätigungs-Prompt, sondern ein Fehler (siehe TwoFactorBusy) – muss
    # vom Aufrufer separat behandelt werden, bevor _split_state() greift.
    main, _, code = state.partition(";")
    methods = [m for m in main.split(",") if m]
    return TwoFactorState(methods=methods, dtmf_code=code or None)


def run_twofactor(host: str, sid: str, timeout: float = 10,
                   on_prompt: Callable[[TwoFactorState], None] = None,
                   poll_interval: float = 1.5, max_wait: float = 180,
                   totp_secret: str | None = None) -> None:
    """Stößt eine Bestätigung an und wartet, bis der Nutzer sie an der Box
    (Taste/DTMF) durchgeführt hat – oder bestätigt automatisch per Google
    Authenticator, wenn ein `totp_secret` (Base32) übergeben wird und die Box
    diese Methode anbietet.

    on_prompt wird einmal mit den verfügbaren Methoden aufgerufen, sobald
    bekannt (zum Anzeigen an den Nutzer) – Standard: print().
    Wirft TwoFactorTimeout bzw. TwoFactorRejected bei Fehlschlag.
    """
    if on_prompt is None:
        on_prompt = lambda st: print(  # noqa: E731
            f"Bitte jetzt an der Box bestätigen (Methoden: {', '.join(st.methods)})"
            + (f", DTMF-Code: {st.dtmf_code}" if st.dtmf_code else "")
        )

    body = _post_form(f"http://{host}/twofactor.lua", {"sid": sid, "tfa_start": ""}, timeout)
    try:
        answer = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise FritzBoxError(f"Unerwartete twofactor.lua-Antwort (tfa_start): {body[:300]!r}")

    state_raw = answer.get("state", "")
    main_part = state_raw.split(";", 1)[0]
    if main_part == "starterror":
        code_str = state_raw.partition(";")[2]
        raise TwoFactorBusy(int(code_str) if code_str.isdigit() else -1)

    state = _split_state(state_raw)
    if not state.methods:
        raise FritzBoxError(f"Konnte Bestätigungsmethoden nicht ermitteln: {answer!r}")
    on_prompt(state)

    # Google Authenticator: wenn angeboten UND ein Secret vorliegt, direkt einen
    # TOTP-Code einreichen (Feldname aus der echten twofactor.js der Box:
    # POST /twofactor.lua mit tfa_googleauth=<6-stellig>; err==1 = Code falsch).
    # HINWEIS: gegen eine echte Box mit eingerichtetem Authenticator NICHT
    # live getestet – die Test-7590 bot nur button/dtmf an. Feldnamen und
    # Fehler-Semantik stammen 1:1 aus dem ausgelieferten Box-JS.
    if "googleauth" in state.methods and totp_secret:
        code = totp.generate(totp_secret)
        body = _post_form(f"http://{host}/twofactor.lua",
                          {"sid": sid, "tfa_googleauth": code}, timeout)
        try:
            answer = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            raise FritzBoxError(f"Unerwartete twofactor.lua-Antwort (tfa_googleauth): {body[:300]!r}")
        if answer.get("err") == 1:
            raise TwoFactorRejected(
                "Google-Authenticator-Code abgelehnt (falsch oder abgelaufen) – "
                "Secret und Systemuhrzeit prüfen."
            )
        return

    # Ohne Secret ist googleauth für uns nicht bedienbar; wenn die Box KEINE
    # pollbare Methode (Taste/DTMF) anbietet, würden wir sonst nutzlos ins
    # Timeout laufen – deshalb hier sofort mit klarer Meldung abbrechen.
    if not any(m in ("button", "dtmf") for m in state.methods):
        raise TwoFactorRejected(
            "Die Box verlangt Google Authenticator, es wurde aber kein "
            "TOTP-Secret angegeben (--totp-secret <BASE32> bzw. Feld in der GUI)."
        )

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        body = _post_form(f"http://{host}/twofactor.lua", {"sid": sid, "tfa_active": ""}, timeout)
        try:
            answer = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        if answer.get("done"):
            if answer.get("active"):
                return
            raise TwoFactorRejected("Bestätigung abgebrochen oder fehlgeschlagen.")
    raise TwoFactorTimeout(f"Keine Bestätigung innerhalb von {max_wait:.0f} Sekunden.")


def ensure_confirmed(host: str, sid: str, page: str, timeout: float = 10,
                      on_prompt: Callable[[TwoFactorState], None] = None,
                      totp_secret: str | None = None) -> None:
    """Prüft tfaNeeded für die gegebene Seite und führt bei Bedarf den
    Bestätigungsfluss durch. Danach ist die SID für die anschließende
    firmwarecfg-Anfrage innerhalb eines beobachteten Vertrauensfensters
    bestätigt (siehe docs im Haupt-README zum beobachteten Verhalten)."""
    if tfa_needed(host, sid, page, timeout):
        run_twofactor(host, sid, timeout, on_prompt, totp_secret=totp_secret)


# ── "Zusätzliche Bestätigung" (2FA) direkt umschalten ───────────────────────
#
# DER ECHTE, FUNKTIONIERENDE WEG — kein Config-Datei-Patch. Der Export/Patch/
# Import-Ansatz (siehe unten) wurde zweifach rigoros gegen eine echte Box
# verifiziert und wirkt NICHT: two_factor_auth_enabled bleibt beim Reimport
# unverändert, obwohl andere Felder im selben Block korrekt übernommen werden
# (AVM schützt dieses eine Feld gezielt vor Config-Import). Der echte
# Schalter liegt auf der "Support"-Seite (System → Support in der klassischen
# Navigation, dort aber ohne Menüpunkt mehr erreichbar) und wird ganz normal
# per Formular submitted, mit dem üblichen 2FA-Bestätigungsfluss davor.
#
# Herkunft: aus dem tatsächlichen PHP-Quellcode von fb_tools (Michael
# Engelke, mengelke.de), Plugin fbtp_2fa.php, extrahiert aus dem offiziell
# vertriebenen .deb-Paket. Dort: request('post', "/data.lua",
# "xhr=1&page=support&twofactor=1&sid=$sid") zum Deaktivieren.
#
# Verifiziert gegen echte Box (4 unabhängige Belege nach dem Toggle):
# tfa_needed() liefert False, TR-064 X_AVM-DE_Auth.GetInfo NewEnabled=0,
# ein Export läuft ganz ohne erneute Bestätigung durch, UND die exportierte
# Datei zeigt two_factor_auth_enabled=no.

def set_additional_confirmation(host: str, sid: str, enabled: bool, timeout: float = 15) -> None:
    """Schaltet "Zusätzliche Bestätigung" (2FA) an/aus. Erwartet eine SID,
    die GERADE per run_twofactor()/ensure_confirmed() bestätigt wurde — im
    selben Skriptlauf, ohne Verzögerung (das Vertrauensfenster ist kurz,
    empirisch beobachtet: eine erneute Bestätigung nach bereits einigen
    Sekunden Pause kann mit TwoFactorBusy scheitern, siehe run_twofactor()).
    """
    fields = {"xhr": "1", "page": "support", "twofactor": "1", "sid": sid}
    if enabled:
        fields["twofactor_auth_enabled"] = "on"
    _post_form(f"http://{host}/data.lua", fields, timeout)
    # Die Antwort ist die neu gerenderte HTML-Seite, kein JSON mit
    # Erfolgsstatus — deshalb hier KEINE Prüfung auf "ok" o.ä. mgl., der
    # Aufrufer MUSS das Ergebnis separat verifizieren (z. B. tfa_needed()).


# ── Export / Import (firmwarecfg) ───────────────────────────────────────────

def export_config(host: str, sid: str, file_password: str, timeout: float = 30) -> bytes:
    """Exportiert die volle Konfiguration. Ruft VORHER ensure_confirmed()
    NICHT selbst auf – das macht der Aufrufer explizit, damit der
    Bestätigungs-Prompt nicht überraschend mitten in einem Skript auftaucht.
    """
    status, content = _post_multipart(
        f"http://{host}/cgi-bin/firmwarecfg",
        {"sid": sid, "ImportExportPassword": file_password, "ConfigExport": ""},
        None,
        timeout,
    )
    if status != 200 or not content:
        raise FritzBoxError(
            f"Export fehlgeschlagen (Status {status}, {len(content)} Bytes). "
            "Häufigste Ursache: 2FA-Bestätigung fehlt oder ist abgelaufen — "
            "vorher ensure_confirmed(host, sid, 'sysSave') aufrufen."
        )
    return content


def import_config_all(host: str, sid: str, file_password: str, content: bytes,
                       filename: str = "patched.export", timeout: float = 30) -> bytes:
    """Vollständiger Reimport (ConfigImportFile) – überschreibt ALLE
    Einstellungen mit dem Inhalt der Datei. Löst einen Neustart der Box aus.
    """
    status, body = _post_multipart(
        f"http://{host}/cgi-bin/firmwarecfg",
        {"sid": sid, "ImportExportPassword": file_password},
        ("ConfigImportFile", filename, content),
        timeout,
    )
    if status != 200:
        raise FritzBoxError(f"Import fehlgeschlagen (Status {status}): {body[:300]!r}")
    return body


@dataclass
class TakeoverGroup:
    node: str          # z. B. "cfgtakeover5"
    label: str         # z. B. "FRITZ!Box-Benutzer"
    hint: str          # zusätzlicher Kontext, z. B. der Benutzername


def start_selective_import(host: str, sid: str, file_password: str, content: bytes,
                            filename: str = "patched.export", timeout: float = 30) -> None:
    """Erster Schritt des selektiven Imports (ConfigTakeOverImportFile):
    lädt die Datei hoch, die Box parst sie serverseitig und hält das
    Ergebnis sessionsgebunden vor (Zugriff über list_takeover_groups()).

    Verifiziert gegen echte Box: liefert eine HTML-Seite ("Einlesen der
    Sicherungsdatei war erfolgreich") zurück, kein JSON – wir prüfen nur
    auf den Erfolgstext, der eigentliche Inhalt kommt über data.lua.
    """
    status, body = _post_multipart(
        f"http://{host}/cgi-bin/firmwarecfg",
        {"sid": sid, "ImportExportPassword": file_password},
        ("ConfigTakeOverImportFile", filename, content),
        timeout,
    )
    text = body.decode("utf-8", errors="replace")
    if status != 200 or "erfolgreich" not in text.lower():
        raise FritzBoxError(f"Selektiver Import (Schritt 1) fehlgeschlagen (Status {status}): {text[:300]!r}")


def list_takeover_groups(host: str, sid: str, timeout: float = 10) -> list[TakeoverGroup]:
    """Liest die von der Box nach start_selective_import() dynamisch
    ermittelten Einstellungsgruppen (welche in der hochgeladenen Datei
    stecken und einzeln übernommen werden können)."""
    body = _post_form(f"http://{host}/data.lua",
                       {"sid": sid, "page": "cfgtakeover_edit", "xhr": "1", "lang": "de"}, timeout)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise FritzBoxError(f"Unerwartete data.lua-Antwort (cfgtakeover_edit): {body[:300]!r}")
    settings = data.get("data", {}).get("settings")
    if settings is None:
        raise FritzBoxError(
            "Keine Einstellungsgruppen gefunden — vermutlich wurde start_selective_import() "
            "nicht (mehr) für diese Session ausgeführt (Zustand verfällt evtl. mit der Zeit)."
        )
    groups = []
    for s in settings:
        hint = " ".join(s.get(f"add{i}_text", "") for i in range(1, 11)).strip()
        groups.append(TakeoverGroup(node=s["_node"], label=s.get("gui_text", ""), hint=hint))
    return groups


def confirm_selective_import(host: str, sid: str, all_groups: list[TakeoverGroup],
                              selected_nodes: set[str], timeout: float = 30) -> None:
    """Schritt 2: wendet die Auswahl an. VERIFIZIERT GEGEN ECHTE BOX.

    Kritischer Fund beim Live-Test: Es reicht NICHT, nur die gewünschten
    _node-Felder mit "1" zu senden (ergab HTTP 200 + {"apply":"ok"}, aber
    OHNE tatsächliche Wirkung — kein Neustart, Einstellung unverändert bei
    Nachkontrolle). Diese Seite nutzt das app-weite "newval"-Reaktivmuster,
    das ALLE bekannten Felder erwartet, nicht nur die geänderten (anders als
    normales HTML-Formularverhalten, wo unchecked-Boxen fehlen dürfen). Erst
    mit explizit ALLEN Gruppen-Feldern (gewünschte="1", alle anderen="0")
    wirkt die Anfrage wirklich – verifiziert per anschließendem Praxistest
    (Rufannahme sofort ohne 2FA möglich) UND per Neuexport (Wert geändert).
    """
    fields = {"sid": sid, "page": "cfgtakeover_edit", "xhr": "1", "apply": ""}
    for g in all_groups:
        fields[g.node] = "1" if g.node in selected_nodes else "0"

    body = _post_form(f"http://{host}/data.lua", fields, timeout)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise FritzBoxError(f"Unerwartete data.lua-Antwort (apply): {body[:300]!r}")
    if data.get("data", {}).get("apply") != "ok":
        raise FritzBoxError(f"Box meldet keinen Erfolg: {body[:300]!r}")
    # ACHTUNG: "apply":"ok" ist KEIN verlässlicher Erfolgsbeleg (siehe oben) –
    # der Aufrufer MUSS das tatsächliche Ergebnis separat verifizieren
    # (z. B. erneuter Export + Wertkontrolle, oder ein Verhaltenstest).
