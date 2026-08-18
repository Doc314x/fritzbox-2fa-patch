"""
cfgfile.py – Patchen einer Fritz!Box-Konfigurationsexportdatei
================================================================
Portiert den Prüfsummen-Algorithmus von github.com/lpinca/fritzbox-checksum
(transformer.js/crc32.js, MIT-lizenziert) nach Python. Dessen CRC32-Klasse
ist Standard-CRC32 (Polynom 0xEDB88320, Startwert/Schlussmaske 0xFFFFFFFF) –
exakt das, was `zlib.crc32` berechnet. Kein Tabellen-Nachbau nötig, nur der
Zustandsautomat, der bestimmt, WELCHE Bytes in welcher Form in die Prüfsumme
einfließen. Gegen eine echte, frisch exportierte 7590-Konfigurationsdatei
verifiziert (verify_checksum() liefert True ohne jede Änderung).

Format einer .export-Datei (vereinfacht):
    **** <Modell> CONFIGURATION EXPORT
    key1=value1
    key2=value2
    **** CFGFILE:<name> ****
    <eingebetteter Inhalt, zeilenweise – eigenes verschachteltes Format:
     block {
             key = value;
             nested_block { ... }
     }>
    // EOF
    (Leerzeile)
    **** END OF FILE ****
    **** B64FILE:<name> ****
    <Base64-Zeilen>
    **** END OF FILE ****
    **** END OF EXPORT <CRC32-Platzhalter, 8 Hex-Zeichen> ****

WICHTIGER FUND (gegen echte Box verifiziert): two_factor_auth_enabled steht
NICHT als Top-Level-„key=value", sondern verschachtelt innerhalb eines
CFGFILE-Blocks, im dortigen eigenen Format mit Leerzeichen und Semikolon:
    boxusers {
            users { ... }
            two_factor_auth_enabled = yes;
            tfa_cfg_version = 1;
    }
patch_variable() sucht deshalb in BEIDEN Kontexten (Top-Level und innerhalb
von CFGFILE-Blöcken) nach `key` und patcht nur den Wert, Einrückung und
Zeilenformat (mit/ohne Leerzeichen, mit/ohne Semikolon) bleiben erhalten.

WICHTIG: Diese Funktionen ändern NUR die eine Zeile, die explizit als Ziel
angegeben wird, und die abschließende Prüfsumme. Alle anderen Bytes bleiben
unverändert – Verifikation dafür: `patch_variable()` gibt zusätzlich die
Zahl der tatsächlich geänderten Vorkommen zurück, ein Aufrufer sollte bei
einem unerwarteten Wert (0 oder >1) abbrechen statt blind weiterzumachen.
"""

from __future__ import annotations

import base64
import re
import zlib
from dataclasses import dataclass

_START_OF_EXPORT_RE = re.compile(r"^\*{4} .+ CONFIGURATION EXPORT$")
_VARIABLE_DEFINITION_RE = re.compile(r"^(.+)=(.+)$")
_START_OF_CFGFILE_RE = re.compile(r"^\*{4} CFGFILE:(.+)$")
_EOF_MARKER = "// EOF"
_START_OF_BXXFILE_RE = re.compile(r"^\*{4} B(64|IN)FILE:(.+)$")
_END_OF_FILE = "**** END OF FILE ****"
_END_OF_EXPORT_RE = re.compile(r"^\*{4} END OF EXPORT ([A-Z0-9]{8}) \*{4}$")

# Erkennt sowohl Top-Level "key=value" als auch eingebettete
# "        key = value;" – erhält Einrückung/Leerzeichen/Semikolon beim
# Patchen, damit nur der Wert sich ändert und die Datei sonst identisch bleibt.
_KV_RE = re.compile(r"^(?P<pre>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<mid>\s*=\s*)(?P<value>.*?)(?P<post>;?\s*)$")

_NO_SECTION, _CONFIGURATION_EXPORT, _CFGFILE, _B64FILE, _BINFILE = range(5)


class CfgFileError(Exception):
    """Die Datei entspricht nicht dem erwarteten Fritz!Box-Exportformat."""


@dataclass
class PatchResult:
    text: str
    matches: int
    old_value: str | None
    checksum: str


def _split_lines(raw: str) -> list[str]:
    # Die Box selbst nutzt \r?\n als Trenner (siehe transformer.js) – wir
    # normalisieren auf \n für die Ausgabe, wie es das Referenz-Tool auch tut.
    return re.split(r"\r?\n", raw)


def _maybe_patch_kv(line: str, patch_key: str | None, patch_value: str | None):
    """Prüft, ob `line` eine key=value- bzw. key = value;-Zuweisung für
    patch_key ist, und liefert ggf. die Zeile mit neuem Wert zurück –
    Einrückung, Leerzeichen um '=' und ein eventuelles ';' bleiben erhalten.

    Gibt (neue_zeile, hat_gepatcht, alter_wert) zurück.
    """
    if patch_key is None:
        return line, False, None
    m = _KV_RE.match(line)
    if not m or m.group("key") != patch_key:
        return line, False, None
    new_line = f"{m.group('pre')}{m.group('key')}{m.group('mid')}{patch_value}{m.group('post')}"
    return new_line, True, m.group("value")


def _recompute(lines: list[str], patch_key: str | None, patch_value: str | None):
    """Läuft einmal durch die Datei, wendet optional einen Patch an (Top-
    Level ODER innerhalb eines CFGFILE-Blocks) und berechnet parallel die
    finale CRC32-Prüfsumme.

    Gibt (neue_zeilen, anzahl_treffer, alter_wert, checksum_hex) zurück.
    """
    section = _NO_SECTION
    crc = 0
    eof_seen = False
    matches = 0
    old_value = None
    out: list[str] = []
    end_idx = None

    for i, line in enumerate(lines):
        new_line = line

        if section == _NO_SECTION:
            if _START_OF_EXPORT_RE.match(line):
                section = _CONFIGURATION_EXPORT

        elif section == _CONFIGURATION_EXPORT:
            m = _VARIABLE_DEFINITION_RE.match(line)
            if m:
                patched, did_patch, old = _maybe_patch_kv(line, patch_key, patch_value)
                if did_patch:
                    matches += 1
                    old_value = old
                    new_line = patched
                # Hash-relevante key/value-Aufteilung IMMER aus der finalen
                # (ggf. gepatchten) Zeile neu ziehen, nicht aus der alten m.
                m2 = _VARIABLE_DEFINITION_RE.match(new_line)
                key, value = m2.group(1), m2.group(2)
                crc = zlib.crc32((key + value).encode("utf-8") + b"\0", crc)
            else:
                m = _START_OF_CFGFILE_RE.match(line)
                if m:
                    section = _CFGFILE
                    eof_seen = False
                    # Regex ist "(.+)$" -> fängt das schließende " ****" der
                    # Markerzeile mit ein (kein Tippfehler, exakt wie im
                    # Original transformer.js – gegen echte Box verifiziert).
                    crc = zlib.crc32(m.group(1).encode("utf-8") + b"\0", crc)
                else:
                    m = _START_OF_BXXFILE_RE.match(line)
                    if m:
                        section = _B64FILE if m.group(1) == "64" else _BINFILE
                        crc = zlib.crc32(m.group(2).encode("utf-8") + b"\0", crc)
                    else:
                        m = _END_OF_EXPORT_RE.match(line)
                        if m:
                            end_idx = i  # Platzhalter erst nach dem Lauf ersetzen

        elif section == _CFGFILE:
            if line == _END_OF_FILE:
                eof_seen = False
                section = _CONFIGURATION_EXPORT
            elif not eof_seen:
                if line == _EOF_MARKER:
                    eof_seen = True
                    crc = zlib.crc32((line + "\n").encode("utf-8"), crc)
                else:
                    patched, did_patch, old = _maybe_patch_kv(line, patch_key, patch_value)
                    if did_patch:
                        matches += 1
                        old_value = old
                        new_line = patched
                    unescaped = new_line.replace("\\\\", "\\")
                    crc = zlib.crc32((unescaped + "\n").encode("utf-8"), crc)
            # nach EOF-Marker erwartet die Box eine Leerzeile, die nicht in
            # die Prüfsumme einfließt (assert.strictEqual(line, '') im Original)

        elif section in (_B64FILE, _BINFILE):
            if line == _END_OF_FILE:
                section = _CONFIGURATION_EXPORT
            else:
                data = base64.b64decode(line) if section == _B64FILE else bytes.fromhex(line)
                crc = zlib.crc32(data, crc)

        out.append(new_line)

    if end_idx is None:
        raise CfgFileError(
            "Kein '**** END OF EXPORT ... ****'-Abschluss gefunden – "
            "keine gültige Fritz!Box-Exportdatei (oder Datei unvollständig)."
        )

    checksum = format(crc & 0xFFFFFFFF, "08X")
    m = _END_OF_EXPORT_RE.match(out[end_idx])
    out[end_idx] = out[end_idx].replace(m.group(1), checksum)

    return out, matches, old_value, checksum


def list_variables(raw: str) -> list[tuple[str, str]]:
    """Listet alle Top-Level key=value-Paare auf (NICHT die innerhalb von
    CFGFILE-Blöcken verschachtelten, siehe list_nested_variables()).
    """
    lines = _split_lines(raw)
    section = _NO_SECTION
    result = []
    for line in lines:
        if section == _NO_SECTION:
            if _START_OF_EXPORT_RE.match(line):
                section = _CONFIGURATION_EXPORT
        elif section == _CONFIGURATION_EXPORT:
            m = _VARIABLE_DEFINITION_RE.match(line)
            if m:
                result.append((m.group(1), m.group(2)))
            elif _START_OF_CFGFILE_RE.match(line):
                section = _CFGFILE
            elif _START_OF_BXXFILE_RE.match(line):
                section = _B64FILE
        elif section == _CFGFILE:
            if line == _END_OF_FILE:
                section = _CONFIGURATION_EXPORT
        elif section == _B64FILE:
            if line == _END_OF_FILE:
                section = _CONFIGURATION_EXPORT
    return result


def find_nested(raw: str, needle: str) -> list[tuple[int, str]]:
    """Durchsucht auch die eingebetteten CFGFILE-Blöcke nach `needle`
    (Groß-/Kleinschreibung ignoriert) – zur Inspektion vor dem Patchen.
    Gibt (Zeilennummer ab 1, Zeileninhalt) zurück.
    """
    lines = _split_lines(raw)
    needle_lower = needle.lower()
    return [(i + 1, line) for i, line in enumerate(lines) if needle_lower in line.lower()]


def verify_checksum(raw: str) -> bool:
    """Prüft, ob die aktuelle Prüfsumme der Datei zu ihrem Inhalt passt."""
    lines = _split_lines(raw)
    m = None
    for line in lines:
        mm = _END_OF_EXPORT_RE.match(line)
        if mm:
            m = mm
    if not m:
        raise CfgFileError("Kein END-OF-EXPORT-Marker gefunden.")
    stated = m.group(1)
    _, _, _, computed = _recompute(lines, None, None)
    return stated == computed


def patch_variable(raw: str, key: str, new_value: str) -> PatchResult:
    """Setzt `key` auf `new_value` – egal ob Top-Level (`key=value`) oder
    innerhalb eines CFGFILE-Blocks (`   key = value;`) – und berechnet die
    Prüfsumme neu. Erwartet GENAU EINEN Treffer im gesamten File; bei 0 oder
    >1 Treffern sollte der Aufrufer NICHT weitermachen (siehe matches).
    """
    lines = _split_lines(raw)
    new_lines, matches, old_value, checksum = _recompute(lines, key, new_value)
    return PatchResult(
        text="\n".join(new_lines),
        matches=matches,
        old_value=old_value,
        checksum=checksum,
    )
