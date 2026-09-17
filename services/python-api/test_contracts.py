#!/usr/bin/env python3
"""Phase 1 contract smoke tests; no credentials or data mutations required."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("ASHAT_TEST_BASE", "https://www.agpstudios.org")

def get(path, expected=200):
    try:
        response = urllib.request.urlopen(BASE + path, timeout=15)
    except urllib.error.HTTPError as exc:
        assert exc.code == expected, (path, exc.code)
        return exc.code, exc.headers, exc.read()
    assert response.status == expected, (path, response.status)
    return response.status, response.headers, response.read()

def main():
    _, headers, body = get("/api/oauth/.well-known/openid-configuration")
    discovery = json.loads(body)
    assert discovery["issuer"] == BASE + "/api/oauth"
    assert discovery["code_challenge_methods_supported"] == ["S256"]
    assert discovery["id_token_signing_alg_values_supported"] == ["RS256"]

    _, _, body = get("/api/oauth/.well-known/jwks.json")
    keys = json.loads(body)["keys"]
    assert len(keys) == 1 and keys[0]["alg"] == "RS256" and keys[0]["kty"] == "RSA"

    _, _, body = get("/api/auth/session")
    assert json.loads(body) == {"authenticated": False}

    _, _, body = get("/api/game-status")
    status = json.loads(body)
    assert status["source"] == "paws-and-parcels"
    assert isinstance(status["online"], bool)

    status, headers, body = get("/api/support", expected=401)
    assert json.loads(body)["error"]["code"] == "authentication_required"
    get("/api/account/summary", expected=401)
    _, _, body = get("/api/admin/users", expected=403)
    assert json.loads(body)["error"]["code"] == "admin_authentication_required"
    request = urllib.request.Request(BASE + "/api/account", data=b'{"confirm":"DELETE"}', headers={"Content-Type":"application/json"}, method="DELETE")
    try: urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as exc: assert exc.code == 401
    get("/api/oauth/authorize?" + urllib.parse.urlencode({
        "response_type": "invalid", "client_id": "contract-test",
        "redirect_uri": "https://pawsandparcels.agpstudios.org/callback", "state": "contract-test",
    }), expected=400)
    print("phase 1 contract smoke tests: PASS")

if __name__ == "__main__":
    main()
