"""
totp.py – TOTP-Codegenerierung (RFC 6238), stdlib-only
========================================================
Kein pyotp o. ä. nötig: TOTP ist reine HMAC-SHA1-Mathematik über ein
geteiltes Secret + die aktuelle Zeit, exakt das, was Google Authenticator /
FreeOTP / Authy & Co. selbst berechnen. Wenn wir dasselbe Secret kennen (das
der Nutzer beim einmaligen Einrichten in der Box-WebGUI erhält, z. B. als
QR-Code, dessen Klartext-Secret man sich merken/notieren muss), können wir
denselben Code erzeugen wie die App – die Box unterscheidet nicht, WER den
richtigen Code eingibt.

WICHTIG: Das Secret lässt sich NICHT nachträglich aus der Box auslesen (auch
nicht aus einem Konfig-Export – dort steht nur ein verschlüsselter Blob).
Es muss beim einmaligen Einrichten (System → FRITZ!Box-Nutzer → Benutzer →
Authenticator-App) vom Nutzer selbst gesichert werden.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def generate(secret_b32: str, for_time: float | None = None,
             digits: int = 6, period: int = 30) -> str:
    """Berechnet den aktuell gültigen TOTP-Code für ein Base32-Secret."""
    if for_time is None:
        for_time = time.time()
    normalized = secret_b32.strip().replace(" ", "").upper()
    padding = "=" * (-len(normalized) % 8)
    key = base64.b32decode(normalized + padding)

    counter = int(for_time // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code_int).zfill(digits)
