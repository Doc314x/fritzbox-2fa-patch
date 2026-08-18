# fritzbox-2fa-patch

Werkzeug, um die Fritz!Box-Funktion **"Zusätzliche Bestätigung"** (2FA per
Tastendruck/Google-Authenticator/DTMF für sicherheitsrelevante Aktionen)
per Kommandozeile an- oder auszuschalten — seit FRITZ!OS 7.39 gibt es dafür
keinen Menüpunkt mehr in der WebGUI.

## Der funktionierende Weg: `fb2fa 2fa`

```
python3 -m fb2fa 2fa --host 192.168.0.1 --user <benutzername>
python3 -m fb2fa 2fa --host 192.168.0.1 --user <benutzername> --enable
```

Meldet sich an, fordert bei Bedarf eine 2FA-Bestätigung an der Box an
(Tastendruck/DTMF), schaltet danach direkt um und verifiziert das Ergebnis.
Kein Neustart, keine Config-Datei nötig.

**Herkunft:** Der Endpunkt (`POST /data.lua` mit `page=support&twofactor=1`,
das Formular `twoFactorDisableForm` auf der von AVM aus dem Menü entfernten,
aber weiter vorhandenen "Support"-Seite) stammt nicht aus eigenem Raten,
sondern aus dem tatsächlichen PHP-Quellcode von
[fb_tools](https://www.mengelke.de/Projekte/FritzBox-Tools) (Michael
Engelke), Plugin `fbtp_2fa.php`, extrahiert aus dem offiziell vertriebenen
`.deb`-Paket. fb_tools ist ein etabliertes, seit Jahren gepflegtes
Community-Werkzeug — dieses Tool hier bildet nur den einen Befehl in Python
nach, mit Fokus auf Nachvollziehbarkeit und Verifikation.

**Verifiziert gegen eine echte FRITZ!Box 7590 (FRITZ!OS 154.08.25 / 8.25),
vierfach unabhängig:**
1. `tfa_needed()` liefert danach `False` (vorher `True`)
2. TR-064 `X_AVM-DE_Auth.GetInfo` liefert `NewEnabled=0` (vorher `1`)
3. Ein Config-Export läuft direkt im Anschluss **ganz ohne** erneute
   Bestätigung durch
4. Die exportierte Datei zeigt `two_factor_auth_enabled = no;`
5. Praxistest: „Rufannahme sofort" (`call_delay=0`) lässt sich setzen, ohne
   dass die Box eine Bestätigung verlangt

Rundlauf getestet (aus → an → aus), jedes Mal selbstverifizierend.

## Alternativer Weg: Config-Datei patchen (funktioniert auch, mit Neustart)

Der klassische Community-Workaround: Config exportieren,
`two_factor_auth_enabled = yes;` in der Sicherungsdatei von Hand auf `no`
ändern (z. B. mit dem [Fritz!Box JSTool von Michael
Engelke](https://www.mengelke.de/)), Prüfsumme neu berechnen, Datei wieder
einspielen. Dieser Weg ist in diesem Repo ebenfalls implementiert (`fb2fa
export`, `fb2fa patch`, `fb2fa import-all`).

**Klargestellt per echtem Vergleichstest:** Über den echten Browser/das
JSTool + reguläre "Wiederherstellen"-Funktion der Box **funktioniert das**
— sauber verifiziert mit einem unkontaminierten Test (frischer Export vor
jeder Änderung → lokal gepatcht → reimportiert → Neustart anhand
zurückgesetztem internen Zeitstempel bestätigt → frischer Export danach
zeigt `two_factor_auth_enabled = no`). Löst dabei, anders als `fb2fa 2fa`,
einen Neustart aus (Telefonie/Internet kurz weg).

**Offene Frage:** Die eigene, rein API-gesteuerte Nachbildung dieses
Ablaufs in `fb2fa import-all` (ohne echten Browser) hat in zwei eigenen
Tests **nicht** funktioniert, obwohl eine Kontrollprobe (`tfa_cfg_version`
in einem Nachbarfeld geändert) im selben Lauf korrekt persistiert wurde.
Es muss also einen Unterschied zwischen dem echten Browser-Upload und dem
hier implementierten Multipart-Request geben, der bislang nicht gefunden
wurde — `fb2fa import-all` sollte deshalb noch nicht blind vertraut werden,
auch wenn der CRC32-Mechanismus selbst (Prüfsummenberechnung) nachweislich
korrekt arbeitet. Für produktiven Einsatz bis auf Weiteres: `fb2fa 2fa`
(oben) oder der manuelle Weg über die echte WebGUI/das JSTool.

## Setup

Keine externen Abhängigkeiten (stdlib-only), Python 3.10+.

```
python3 -m fb2fa 2fa --host 192.168.0.1 --user <benutzername>
```

Passwort wird interaktiv abgefragt (empfohlen), oder per `--password`.

## Weitere Befehle (Config-Export/Patch/Import, für andere Einstellungen)

- `fb2fa export --host ... --user ... --out backup.export`
- `fb2fa list-vars backup.export [--grep <text>]` — Top-Level-Variablen auflisten
- `fb2fa patch backup.export --key <name> --value <wert> --out patched.export`
- `fb2fa verify backup.export` — Prüfsumme kontrollieren
- `fb2fa import-all --host ... --user ... patched.export` — vollständiger
  Reimport, **löst einen Neustart aus**, Telefonie/Internet kurz weg

Für Felder außerhalb von `two_factor_auth_enabled` (das AVM schützt) ist
dieser Weg voll funktionsfähig — die Prüfsummenberechnung wurde gegen die
echte Box verifiziert (Selbsttest mit synthetischer Beispieldatei plus
Kontrollprobe mit echtem Feld, siehe oben).

`fb2fa import-selective-start` (selektiver Import einzelner
Einstellungsgruppen statt der ganzen Config) ist ebenfalls implementiert,
aber weniger gut getestet — für die eigentliche 2FA-Frage inzwischen
irrelevant, da der `fb2fa 2fa`-Befehl das Problem direkt löst.
