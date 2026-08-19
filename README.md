# fritzbox-2fa-patch

Werkzeug, um die Fritz!Box-Funktion **„Zusätzliche Bestätigung"** (2FA per
Tastendruck / Google Authenticator / DTMF für sicherheitsrelevante Aktionen)
an- oder auszuschalten — seit FRITZ!OS 7.39 gibt es dafür keinen Menüpunkt
mehr in der WebGUI.

Es gibt eine **grafische Oberfläche (als Windows-`.exe`)** und ein
**Kommandozeilen-Werkzeug**. Beide nutzen denselben, reboot-freien Weg.

---

## Schnellstart: die GUI (Windows-`.exe`)

1. Unter **[Releases](../../releases)** die aktuelle `fb2fa.exe` herunterladen
   (wird per GitHub Actions gebaut, siehe unten).
2. Doppelklick. **FRITZ!Box-Adresse** eingeben — die **Benutzernamen der Box
   werden automatisch geladen** und stehen als Auswahlliste bereit.
3. Benutzer wählen, **Kennwort** eingeben (optional ein **TOTP-Secret**, nur
   nötig, wenn die Box *ausschließlich* Google Authenticator anbietet — siehe
   unten). Dann **„Verbinden / Status abfragen"**: der große **Schiebeschalter**
   zeigt danach den aktuellen 2FA-Zustand.
4. **Schiebeschalter umlegen** → 2FA wird an- bzw. ausgeschaltet (ein „Ausführen"-
   Knopf entfällt). Verlangt die Box eine Bestätigung, erscheint im Protokoll die
   Aufforderung (samt DTMF-Code) — dann an der Box die **Verbindungstaste**
   drücken bzw. den Code am Telefon eingeben. Das Ergebnis wird mit einer
   frischen Anmeldung verifiziert; bei Fehlschlag springt der Schalter auf den
   echten Zustand zurück.

Kein Neustart, kein Config-Datei-Patch.

---

## Kommandozeile: `fb2fa 2fa`

Stdlib-only, Python 3.10+, keine externen Abhängigkeiten.

```
python3 -m fb2fa 2fa --host 192.168.0.1 --user <benutzer>            # deaktivieren
python3 -m fb2fa 2fa --host 192.168.0.1 --user <benutzer> --enable   # aktivieren
python3 -m fb2fa gui                                                 # GUI starten
```

Kennwort wird interaktiv abgefragt (empfohlen) oder per `--password` übergeben.

Meldet sich an, fordert bei Bedarf eine Bestätigung an der Box an, schaltet um
und **verifiziert das Ergebnis mit einer frischen Anmeldung** (umgeht so das
kurze 2FA-Vertrauensfenster, in dem eine gerade bestätigte Sitzung sonst ein
falsches „OK" liefern könnte).

### Google Authenticator

Bietet die Box als Bestätigung **nur** die Authenticator-App an (kein
Tastendruck/DTMF), kann das Tool den TOTP-Code selbst erzeugen und einreichen —
dafür das Base32-Secret angeben:

```
python3 -m fb2fa 2fa --host 192.168.0.1 --user <benutzer> --totp-secret <BASE32>
```

Das Secret erhält man **einmalig** beim Einrichten in der Box-WebGUI
(System → FRITZ!Box-Nutzer → Benutzer → Authenticator-App); es lässt sich später
nicht mehr aus der Box auslesen und muss selbst gesichert werden.

> Der googleauth-Pfad ist exakt nach der ausgelieferten `twofactor.js` der Box
> nachgebildet (`POST /twofactor.lua` mit `tfa_googleauth=<Code>`), aber – anders
> als der Tasten-/DTMF-Weg – noch **nicht** gegen eine echte Box mit
> eingerichtetem Authenticator live getestet.

---

## Herkunft & Verifikation

Der Endpunkt (`POST /data.lua` mit `page=support&twofactor=1`, Formular
`twoFactorDisableForm` auf der aus dem Menü entfernten, aber weiter vorhandenen
„Support"-Seite) stammt aus dem PHP-Quellcode von
[fb_tools](https://www.mengelke.de/Projekte/FritzBox-Tools) (Michael Engelke),
Plugin `fbtp_2fa.php`.

**Live verifiziert gegen eine echte FRITZ!Box 7590 (FRITZ!OS 154.08):** Der
Direkt-Umschalter läuft reboot-frei durch (Login → Bestätigung per Taste →
Umschaltung → Selbst-Verifikation) und wurde im Wechsel (an → aus → an)
mehrfach bestätigt.

---

## Alternativer Weg: Config-Datei patchen

Der klassische Community-Workaround: Config exportieren,
`two_factor_auth_enabled = yes;` in der Sicherungsdatei auf `no` ändern,
Prüfsumme neu berechnen, Datei wieder einspielen.

Die **Prüfsummen-Berechnung ist byte-genau gegen den Referenz-Editor
[Fritz!Box JSTool](https://www.mengelke.de/) verifiziert**: Aus derselben
Originaldatei erzeugt `fb2fa patch` ein Ergebnis, das *Byte für Byte* mit der
JSTool-gepatchten Datei übereinstimmt (identische CRC32-Prüfsumme). Der
Algorithmus ist Standard-CRC32 (Polynom `0xEDB88320`), portiert von
[lpinca/fritzbox-checksum](https://github.com/lpinca/fritzbox-checksum) (MIT).

- `fb2fa export  --host ... --user ... --out backup.export`
- `fb2fa list-vars backup.export [--grep <text>]`
- `fb2fa verify  backup.export`
- `fb2fa patch   backup.export --value no --out patched.export`
- `fb2fa import-all --host ... --user ... patched.export` — **Voll-Import,
  löst einen Neustart aus**

> **Hinweis (früher offene Frage, jetzt geklärt):** Der *selektive* Import
> (nur die Gruppe „FRITZ!Box-Benutzer") schreibt `two_factor_auth_enabled`
> sehr wohl — das wurde live bestätigt. Die frühere Beobachtung „wirkt nicht"
> war ein **Verifikations-Timing-Problem**: der Apply wirkt verzögert (Reboot),
> eine zu frühe Nachkontrolle liest noch den alten Stand. Für den Alltag ist
> der reboot-freie **`fb2fa 2fa`-Direktweg (oben) klar vorzuziehen** — der
> Config-Import bleibt der schwerere Weg mit Neustart.

---

## Aus dem Quellcode bauen (Windows-`.exe`)

Die `.exe` wird von **GitHub Actions** gebaut (`.github/workflows/build.yml`):

- **Automatisch** bei jedem Git-Tag `v*` (z. B. `v1.0.0`) → zusätzlich ein
  **Release** mit angehängter `fb2fa.exe`.
- **Manuell** über *Actions → Build Windows EXE → Run workflow* → die `.exe`
  liegt danach als Artefakt zum Download bereit.

Lokal auf Windows selbst bauen:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name fb2fa fb2fa_gui.py
# Ergebnis: dist\fb2fa.exe
```

(PyInstaller kann nicht cross-kompilieren — eine Windows-`.exe` entsteht nur auf
Windows bzw. auf dem Windows-Runner.)

---

## Sicherheit

Die Export-/Backup-Dateien enthalten **alle** Box-Einstellungen (inkl.
Zugangsdaten) — sicher aufbewahren, **nicht** ins Repo committen.

Das Werkzeug braucht die **normalen Anmeldedaten deiner Box** (Benutzer +
Kennwort) und ändert nur eine Einstellung, die AVM aus dem Menü entfernt hat —
es umgeht keine Anmeldung und ist kein „Exploit". Wer die Box-Zugangsdaten hat,
ist ihr Administrator.

---

## Lizenz

[MIT](LICENSE). Der CRC32-Prüfsummenteil ist von
[lpinca/fritzbox-checksum](https://github.com/lpinca/fritzbox-checksum) (MIT)
portiert; der 2FA-Endpunkt stammt aus
[fb_tools](https://www.mengelke.de/Projekte/FritzBox-Tools) (Michael Engelke).

## Haftungsausschluss

Kein offizielles AVM-Produkt und nicht mit AVM verbunden. „FRITZ!Box" und
„FRITZ!OS" sind Marken der AVM GmbH. Nutzung auf **eigene Gefahr, ohne jede
Gewähr** — das Werkzeug spricht undokumentierte Endpunkte der Box an, die sich
mit FRITZ!OS-Updates ändern können. Der Config-Import-Weg (`import-all`) löst
einen **Neustart** der Box aus (Telefonie/Internet kurz weg). Vor Änderungen an
der Konfiguration eine Sicherung anlegen.
