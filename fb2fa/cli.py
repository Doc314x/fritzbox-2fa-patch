"""
cli.py – Kommandozeile für den Fritz!Box-2FA-Patcher
======================================================
Bewusst als einzelne, unabhängige Schritte statt einem Ein-Kommando-
"Zauberknopf": export → patch → import sind riskant genug (Neustart,
Telefonie-Unterbrechung), dass jeder Schritt einzeln nachvollziehbar und
abbrechbar sein soll. Zwischen den Schritten liegen normale Dateien auf der
Platte, die man sich ansehen kann, bevor man weitermacht.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import cfgfile, session

DEFAULT_KEY = "two_factor_auth_enabled"


def _cmd_2fa(args):
    password = args.password or getpass.getpass("Fritz!Box-Kennwort: ")
    sid = session.login(args.host, args.user, password)
    print(f"Angemeldet (SID {sid}).")

    def prompt(state):
        print(f"Bitte jetzt an der Box bestätigen (Methoden: {', '.join(state.methods)})"
              + (f", DTMF-Code: {state.dtmf_code}" if state.dtmf_code else ""))

    action = "aktivieren" if args.enable else "deaktivieren"
    print(f"'Zusätzliche Bestätigung' wird {action}...")
    if session.tfa_needed(args.host, sid, "sysSave"):
        session.run_twofactor(args.host, sid, on_prompt=prompt)
        print("Bestätigt, sende Umschaltung...")
    else:
        print("Aktuell keine Bestätigung nötig (2FA derzeit aus), sende Umschaltung direkt...")
    session.set_additional_confirmation(args.host, sid, args.enable)

    still_needed = session.tfa_needed(args.host, sid, "sysSave")
    if still_needed == args.enable:
        print("Umschaltung verifiziert (tfa_needed passt zum neuen Zustand).")
    else:
        print(
            f"WARNUNG: tfa_needed meldet weiterhin den alten Zustand — "
            f"bitte manuell nachprüfen (z. B. erneuter Login-Check).",
            file=sys.stderr,
        )
        sys.exit(1)


def _cmd_export(args):
    password = args.password or getpass.getpass("Fritz!Box-Kennwort: ")
    file_password = args.file_password or getpass.getpass(
        "Kennwort für die Exportdatei (frei wählbar, wird für den späteren Import wieder gebraucht): "
    )
    sid = session.login(args.host, args.user, password)
    print(f"Angemeldet (SID {sid}).")

    if session.tfa_needed(args.host, sid, "sysSave"):
        print("Export verlangt eine Bestätigung an der Box.")
        session.ensure_confirmed(args.host, sid, "sysSave")
        print("Bestätigt.")
    else:
        print("Keine Bestätigung nötig (unerwartet, bitte Ergebnis prüfen).")

    content = session.export_config(args.host, sid, file_password)
    with open(args.out, "wb") as f:
        f.write(content)
    print(f"Export gespeichert: {args.out} ({len(content)} Bytes)")
    print("WICHTIG: Diese Datei enthält alle Box-Einstellungen — sicher aufbewahren, nicht committen.")


def _cmd_list_vars(args):
    with open(args.file, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    for key, value in cfgfile.list_variables(raw):
        if args.grep and args.grep.lower() not in key.lower():
            continue
        print(f"{key}={value}")


def _cmd_verify(args):
    with open(args.file, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    ok = cfgfile.verify_checksum(raw)
    print("Prüfsumme OK" if ok else "Prüfsumme STIMMT NICHT")
    sys.exit(0 if ok else 1)


def _cmd_patch(args):
    with open(args.file, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    result = cfgfile.patch_variable(raw, args.key, args.value)
    if result.matches != 1:
        print(
            f"ABBRUCH: {result.matches} Treffer für '{args.key}' gefunden, erwartet genau 1. "
            "Nichts geschrieben — bitte mit list-vars den genauen Variablennamen prüfen.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"{args.key}: {result.old_value!r} -> {args.value!r}")
    print(f"Neue Prüfsumme: {result.checksum}")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(result.text)
    print(f"Geschrieben: {args.out}")


def _cmd_import_all(args):
    with open(args.file, "rb") as f:
        content = f.read()
    password = args.password or getpass.getpass("Fritz!Box-Kennwort: ")
    file_password = args.file_password or getpass.getpass("Kennwort der Exportdatei: ")

    print("!! Dies überschreibt ALLE Einstellungen der Box mit dem Inhalt dieser Datei")
    print("!! und löst einen Neustart aus (Telefonie/Internet kurz weg, Logs werden geleert).")
    if not args.yes:
        confirm = input("Wirklich fortfahren? (ja/nein): ")
        if confirm.strip().lower() != "ja":
            print("Abgebrochen.")
            return

    sid = session.login(args.host, args.user, password)
    if session.tfa_needed(args.host, sid, "sysImp"):
        print("Import verlangt eine Bestätigung an der Box.")
        session.ensure_confirmed(args.host, sid, "sysImp")
        print("Bestätigt.")

    body = session.import_config_all(args.host, sid, file_password, content)
    print("Antwort der Box:", body[:300])
    print("Die Box startet jetzt voraussichtlich neu.")


def _cmd_import_selective_start(args):
    with open(args.file, "rb") as f:
        content = f.read()
    password = args.password or getpass.getpass("Fritz!Box-Kennwort: ")
    file_password = args.file_password or getpass.getpass("Kennwort der Exportdatei: ")

    sid = session.login(args.host, args.user, password)
    if session.tfa_needed(args.host, sid, "sysImp"):
        print("Import verlangt eine Bestätigung an der Box.")
        session.ensure_confirmed(args.host, sid, "sysImp")
        print("Bestätigt.")

    body = session.start_selective_import(args.host, sid, file_password, content)
    print("Rohe Antwort der Box (Schritt 1 von 2, Format noch nicht ausgewertet):")
    print(body.decode("utf-8", errors="replace"))
    print()
    print("Schritt 2 (gezielte Auswahl übernehmen) ist noch nicht implementiert —")
    print("siehe TODO in fb2fa/session.py:start_selective_import(). Mit dieser")
    print("Ausgabe können wir ihn jetzt fertigstellen.")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="fb2fa",
        description="Fritz!Box-Konfiguration exportieren, gezielt patchen (z. B. "
                     "two_factor_auth_enabled), wieder einspielen.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default="192.168.0.1")
    common.add_argument("--user", required=True)
    common.add_argument("--password", help="Falls weggelassen: interaktive Abfrage (empfohlen).")

    p_2fa = sub.add_parser(
        "2fa", parents=[common],
        help="'Zusätzliche Bestätigung' direkt an-/ausschalten (empfohlener Weg, kein Config-Patch nötig).",
    )
    p_2fa.add_argument("--enable", action="store_true", help="Aktivieren statt deaktivieren.")
    p_2fa.set_defaults(func=_cmd_2fa)

    p_export = sub.add_parser("export", parents=[common], help="Konfiguration exportieren.")
    p_export.add_argument("--file-password", help="Kennwort für die Exportdatei (interaktiv, falls weggelassen).")
    p_export.add_argument("--out", default="backup.export")
    p_export.set_defaults(func=_cmd_export)

    p_list = sub.add_parser("list-vars", help="Variablen einer Exportdatei auflisten.")
    p_list.add_argument("file")
    p_list.add_argument("--grep", help="Nur Zeilen, deren Schlüssel diesen Text enthält.")
    p_list.set_defaults(func=_cmd_list_vars)

    p_verify = sub.add_parser("verify", help="Prüfsumme einer Exportdatei kontrollieren.")
    p_verify.add_argument("file")
    p_verify.set_defaults(func=_cmd_verify)

    p_patch = sub.add_parser("patch", help="Eine Variable ändern und Prüfsumme neu berechnen.")
    p_patch.add_argument("file")
    p_patch.add_argument("--key", default=DEFAULT_KEY)
    p_patch.add_argument("--value", required=True)
    p_patch.add_argument("--out", default="patched.export")
    p_patch.set_defaults(func=_cmd_patch)

    p_import = sub.add_parser("import-all", parents=[common],
                               help="Vollständigen Reimport durchführen (überschreibt ALLES, Neustart).")
    p_import.add_argument("file")
    p_import.add_argument("--file-password", help="Kennwort der Exportdatei (interaktiv, falls weggelassen).")
    p_import.add_argument("--yes", action="store_true", help="Sicherheitsabfrage überspringen.")
    p_import.set_defaults(func=_cmd_import_all)

    p_import_sel = sub.add_parser(
        "import-selective-start", parents=[common],
        help="Schritt 1 des selektiven Imports (experimentell, siehe TODO).",
    )
    p_import_sel.add_argument("file")
    p_import_sel.add_argument("--file-password", help="Kennwort der Exportdatei (interaktiv, falls weggelassen).")
    p_import_sel.set_defaults(func=_cmd_import_selective_start)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
