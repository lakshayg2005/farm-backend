"""Generate the RS256 key pair used to sign (identity) and verify (all services) access tokens.

    uv run python scripts/gen_jwt_keys.py            # writes secrets/jwt_private.pem and jwt_public.pem

The secrets/ folder is git-ignored. Never commit the private key; staging uses its own pair.
"""

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

out = Path(sys.argv[1] if len(sys.argv) > 1 else "secrets")
out.mkdir(parents=True, exist_ok=True)
private_path, public_path = out / "jwt_private.pem", out / "jwt_public.pem"
if private_path.exists():
    sys.exit(f"{private_path} already exists; delete it first to rotate keys")

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private_path.write_bytes(
    key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
)
public_path.write_bytes(
    key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
)
print(f"wrote {private_path} and {public_path}")
