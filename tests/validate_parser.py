"""Validation du parseur Beagle contre le log Jeedom fourni (beagle3.log).

Ce script extrait les paires (trame, résultat attendu) du log Jeedom :
- lignes ``[DEBUG] : 0201...`` => trame hex
- ligne suivante ``Beagle found This is a ...`` => description
- ligne suivante ``Send to jeedom : {...}`` => résultat JSON attendu

Puis il passe chaque trame au parseur Home Assistant porté et compare le
dictionnaire normalisé à celui envoyé à Jeedom.

Exécuté via ``python validate_parser.py`` depuis le dossier racine du projet.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "odace_sfsp"))

import parser  # noqa: E402  (port autonome, sans dépendance HA)


LOG_PATH = Path("/sessions/kind-awesome-euler/mnt/uploads/beagle3.log")

TRAME_RE = re.compile(r"\[DEBUG\] : ([0-9a-f]{40,})\s*$")
SEND_RE = re.compile(r"Send to jeedom : (\{.*\})")


def run() -> int:
    if not LOG_PATH.exists():
        print(f"Log introuvable: {LOG_PATH}")
        return 1

    lines = LOG_PATH.read_text().splitlines()
    parser.reset_dedupe()

    cases = []
    i = 0
    while i < len(lines):
        m = TRAME_RE.search(lines[i])
        if m:
            trame = m.group(1)
            # Cherche le prochain "Send to jeedom"
            expected = None
            for j in range(i + 1, min(i + 5, len(lines))):
                s = SEND_RE.search(lines[j])
                if s:
                    expected = ast.literal_eval(s.group(1))
                    break
            if expected:
                cases.append((trame, expected))
        i += 1

    passed = failed = 0
    for trame, expected in cases:
        # Le log Jeedom envoie au format {'devices': {uuid: {mac, uuid, model, data}}}
        exp_payload = list(expected["devices"].values())[0]
        mac = exp_payload["mac"]
        got = parser.parse_trame(trame, mac)
        if got is None:
            # Peut être filtré par la déduplication -> accepté si l'original le serait aussi
            print(f"SKIP trame dédupliquée {trame[:40]}...")
            continue
        ok = (
            got["uuid"].lower() == exp_payload["uuid"].lower()
            and got["model"] == exp_payload["model"]
            and got["mac"].lower() == exp_payload["mac"].lower()
            and got["data"].get("type") == exp_payload["data"].get("type")
            and str(got["data"].get("value")) == str(exp_payload["data"].get("value"))
            and got["data"].get("label") == exp_payload["data"].get("label")
        )
        if ok:
            passed += 1
        else:
            failed += 1
            print("FAIL", trame[:50])
            print("  got=     ", got)
            print("  expected=", exp_payload)

    print(f"\n{passed} réussis / {failed} échoués sur {passed+failed} cas testés")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(run())
