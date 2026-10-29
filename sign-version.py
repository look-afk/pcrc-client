#!/usr/bin/env python3
"""Подписать version.json (Ed25519).

Использование:
    python sign-version.py <путь/к/version.json>

Каноничная payload-строка (должна совпадать с client_final.py):
    json.dumps({k: v for k, v in data.items() if k != "sig"},
               sort_keys=True, separators=(",", ":")).encode("utf-8")

Подпись покрывает ВСЕ поля метаданных (version, url, sha256, zip_url, zip_sha256).

Приватный ключ: .signing/update_signing_private.pem (в .gitignore, НЕ коммитить).
Публичный ключ вшит в client_final.py (UPDATE_PUBLIC_KEY).
"""
import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PRIV_KEY = Path(__file__).parent / ".signing" / "update_signing_private.pem"


def canonical(data: dict) -> bytes:
    d2 = {k: v for k, v in data.items() if k != "sig"}
    return json.dumps(d2, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    if not PRIV_KEY.exists():
        print(f"ERROR: private key not found: {PRIV_KEY}")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    for field in ("version", "url", "sha256"):
        if not data.get(field):
            print(f"ERROR: version.json missing '{field}'")
            return 1
    priv = serialization.load_pem_private_key(PRIV_KEY.read_bytes(), password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        print("ERROR: key is not Ed25519")
        return 1
    data.pop("sig", None)
    sig = priv.sign(canonical(data))
    data["sig"] = base64.b64encode(sig).decode("ascii")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Signed {path} -> sig={data['sig'][:32]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
