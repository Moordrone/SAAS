"""End-to-end tests over HTTP, including the error envelope."""

PASSWORD = "correct-horse-battery-staple"


def _signup(client, email="dev@example.com"):
    return client.post(
        "/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "full_name": "Dev User"},
    )


def test_health(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_signup_then_login_then_me(client):
    r = _signup(client)
    assert r.status_code == 201, r.text
    assert r.json()["email_verified"] is False

    r = client.post("/v1/auth/login", json={"email": "dev@example.com", "password": PASSWORD})
    assert r.status_code == 200
    token = r.json()["access_token"]

    r = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "dev@example.com"


def test_me_requires_authentication(client):
    r = client.get("/v1/me")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "not_authenticated"


def test_errors_use_problem_json_with_a_request_id(client):
    _signup(client)
    r = _signup(client)  # duplicate

    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "email_already_registered"
    assert body["status"] == 409
    assert body["request_id"]
    assert r.headers["x-request-id"] == body["request_id"]


def test_validation_errors_are_structured(client):
    r = client.post(
        "/v1/auth/signup",
        json={"email": "not-an-email", "password": "short", "full_name": ""},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert {e["field"] for e in body["errors"]} >= {"email", "password"}


def test_new_account_sees_its_trial_credits(client):
    _signup(client)
    r = client.post("/v1/auth/login", json={"email": "dev@example.com", "password": PASSWORD})
    token = r.json()["access_token"]

    r = client.get("/v1/credits/balance", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert float(body["available"]) == 50.0
    assert float(body["held"]) == 0.0

    r = client.get("/v1/credits/transactions", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()[0]["operation"] == "grant"


def test_password_reset_request_does_not_leak_account_existence(client):
    _signup(client)
    known = client.post("/v1/auth/password-reset/request", json={"email": "dev@example.com"})
    unknown = client.post("/v1/auth/password-reset/request", json={"email": "ghost@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


# --- projects over HTTP ---------------------------------------------------

def _auth(client, email="dev2@example.com"):
    client.post(
        "/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "full_name": "Dev"},
    )
    r = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_catalog_is_public(client):
    r = client.get("/v1/catalog/components")
    assert r.status_code == 200
    keys = {c["key"] for c in r.json()["components"]}
    assert {"RectangularPatch", "MicrostripLine"} <= keys


def test_materials_carry_their_reference_frequency(client):
    r = client.get("/v1/catalog/materials")
    fr4 = next(s for s in r.json()["substrates"] if s["key"] == "FR4")
    assert fr4["epsilon_r"] == 4.4
    assert fr4["reference_frequency_hz"] == 1e9


def test_full_design_flow_over_http(client):
    """Signup to a real physical answer, without a solver in sight."""
    h = _auth(client, "flow@example.com")

    r = client.post(
        "/v1/projects",
        json={"name": "2.45 GHz patch", "component_type": "RectangularPatch"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["status"] == "draft"

    definition = {
        "schema_version": "1.0.0",
        "component": {"family": "Antennas", "type": "RectangularPatch"},
        "parameters": {
            "frequency_center": {"value": 2.45, "unit": "GHz", "provenance": "user"},
            "substrate_material": {"value": "RO4003C", "provenance": "user"},
            "substrate_height": {"value": 0.813, "unit": "mm", "provenance": "user"},
        },
    }
    r = client.put(
        f"/v1/projects/{pid}/definition",
        json={"definition": definition, "change_summary": "Initial design"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_valid"] is True
    assert r.json()["content_hash"].startswith("sha256:")

    r = client.post(f"/v1/projects/{pid}/estimate", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # 41.3 mm wide, 33 mm long for RO4003C at 2.45 GHz.
    assert 0.040 < body["results"]["patch"]["width_m"] < 0.043
    assert 6.0 < body["results"]["performance"]["directivity_dbi"] < 9.0


def test_invalid_definition_reports_what_is_missing(client):
    h = _auth(client, "partial@example.com")
    r = client.post(
        "/v1/projects",
        json={"name": "wip", "component_type": "RectangularPatch"},
        headers=h,
    )
    pid = r.json()["id"]

    r = client.post(f"/v1/projects/{pid}/validate", headers=h)
    assert r.status_code == 200
    assert r.json()["valid"] is False
    assert "frequency_center" in r.json()["missing"]


def test_unknown_component_is_rejected_with_alternatives(client):
    h = _auth(client, "wrong@example.com")
    r = client.post(
        "/v1/projects", json={"name": "x", "component_type": "Teleporter"}, headers=h
    )
    assert r.status_code == 400
    assert r.json()["code"] == "engineering_error"
    assert "RectangularPatch" in r.json()["detail"]


def test_projects_require_authentication(client):
    assert client.get("/v1/projects").status_code == 401
