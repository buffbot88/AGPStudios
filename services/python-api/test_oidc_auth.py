import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proxy


CLIENTS = {
    "everenfell": {
        "redirect_uris": ["https://everenfell.agpstudios.org/auth/callback"],
        "allowed_origins": ["https://everenfell.agpstudios.org"],
    },
    "paws-and-parcels": {
        "redirect_uris": ["https://pawsandparcels.agpstudios.org/oidc-callback.html"],
        "allowed_origins": ["https://pawsandparcels.agpstudios.org"],
    },
}


def params(client="everenfell", redirect=None, nonce="n" * 24):
    return {
        "client_id": [client],
        "redirect_uri": [redirect or CLIENTS[client]["redirect_uris"][0]],
        "state": ["s" * 24],
        "scope": ["openid profile"],
        "code_challenge": ["c" * 43],
        "code_challenge_method": ["S256"],
        "nonce": [nonce],
    }


def test_oidc_client_registration_and_pkce():
    original = proxy.CONFIG.get("oidc_clients")
    proxy.CONFIG["oidc_clients"] = CLIENTS
    try:
        assert proxy.valid_oidc_params(params()) == (
            "everenfell",
            "https://everenfell.agpstudios.org/auth/callback",
            "s" * 24,
            "c" * 43,
            "n" * 24,
        )
        assert proxy.valid_oidc_params(params(redirect="https://evil.example/callback")) is None
        assert proxy.valid_oidc_params(params(nonce="")) is None
        missing_pkce = params()
        missing_pkce["code_challenge_method"] = ["plain"]
        assert proxy.valid_oidc_params(missing_pkce) is None
        paws = params("paws-and-parcels", nonce="")
        assert proxy.valid_oidc_params(paws)[-1] == ""
    finally:
        proxy.CONFIG["oidc_clients"] = original


def test_id_token_signs_nonce_and_entitlement_claims():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    original_key = proxy.key
    proxy.key = lambda: private
    try:
        token = proxy.issue_token({
            "client_id": "everenfell",
            "user_id": "agp-user-1",
            "username": "admin",
            "email": "admin@example.test",
            "role": "Admin",
            "everenfell_s1": True,
            "nonce": "n" * 24,
        })
        header, payload, signature = token.split(".")
        signing_input = f"{header}.{payload}".encode()
        private.public_key().verify(
            base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        assert claims["sub"] == "agp-user-1"
        assert claims["nonce"] == "n" * 24
        assert claims["everenfell_s1"] is True
    finally:
        proxy.key = original_key


if __name__ == "__main__":
    test_oidc_client_registration_and_pkce()
    test_id_token_signs_nonce_and_entitlement_claims()
    print("OIDC registration, PKCE, nonce, and entitlement claims: PASS")
