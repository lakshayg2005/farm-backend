async def test_register_normalises_phone_and_publishes_event(api, client, outbox_types):
    response = await api.register(phone="98765 43210", roles=["farmer", "customer"])

    assert response.status_code == 201
    user = response.json()
    assert user["phone"] == "+919876543210"
    assert user["roles"] == ["customer", "farmer"]
    assert user["phoneVerified"] is False
    assert "passwordHash" not in user
    assert await outbox_types() == ["user.registered"]


async def test_register_rejects_duplicate_phone(api, client):
    await api.register()

    response = await api.register(phone="+91 98765-43210")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PHONE_TAKEN"


async def test_cannot_self_register_as_admin(api, client):
    response = await api.register(roles=["admin"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_login_and_me(api, client):
    await api.register()

    tokens = await api.login()
    me = await client.get("/me", headers=api.bearer(tokens))

    assert tokens["tokenType"] == "bearer"
    assert tokens["expiresIn"] == 900
    assert me.status_code == 200
    assert me.json()["name"] == "Asha"


async def test_login_failures_look_the_same(api, client):
    await api.register()

    wrong_password = await client.post(
        "/login", json={"phone": "+919876543210", "password": "nope-nope"}
    )
    unknown_phone = await client.post(
        "/login", json={"phone": "+919000000099", "password": api.PASSWORD}
    )

    for response in (wrong_password, unknown_phone):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_otp_verifies_phone_and_logs_in(api, client):
    await api.register()

    sent = await client.post("/otp/request", json={"phone": "9876543210"})
    code = sent.json()["devCode"]
    wrong = await client.post(
        "/otp/verify",
        json={"phone": "9876543210", "code": "000000" if code != "000000" else "111111"},
    )
    right = await client.post("/otp/verify", json={"phone": "9876543210", "code": code})
    reused = await client.post("/otp/verify", json={"phone": "9876543210", "code": code})

    assert sent.status_code == 202
    assert wrong.status_code == 400
    assert right.status_code == 200
    assert right.json()["user"]["phoneVerified"] is True
    assert reused.json()["error"]["code"] == "OTP_INVALID"


async def test_otp_locks_after_too_many_wrong_codes(api, client, settings):
    await api.register()
    code = (await client.post("/otp/request", json={"phone": "9876543210"})).json()["devCode"]
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(settings.otp_max_attempts):
        await client.post("/otp/verify", json={"phone": "9876543210", "code": wrong})
    locked = await client.post("/otp/verify", json={"phone": "9876543210", "code": code})

    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "OTP_LOCKED"


async def test_otp_for_unknown_phone(api, client):
    response = await client.post("/otp/request", json={"phone": "9000000099"})

    assert response.status_code == 404


async def test_refresh_rotates_and_detects_reuse(api, client):
    await api.register()
    first = await api.login()

    second = await client.post("/refresh", json={"refreshToken": first["refreshToken"]})
    reuse = await client.post("/refresh", json={"refreshToken": first["refreshToken"]})
    after_reuse = await client.post(
        "/refresh", json={"refreshToken": second.json()["refreshToken"]}
    )

    assert second.status_code == 200
    assert second.json()["refreshToken"] != first["refreshToken"]
    assert reuse.json()["error"]["code"] == "TOKEN_REUSED"
    # reuse ends every session, including the legitimately rotated one
    assert after_reuse.status_code == 401


async def test_access_token_is_not_a_refresh_token(api, client):
    await api.register()
    tokens = await api.login()

    response = await client.post("/refresh", json={"refreshToken": tokens["accessToken"]})

    assert response.status_code == 401


async def test_logout_revokes_refresh_token(api, client):
    await api.register()
    tokens = await api.login()

    out = await client.post("/logout", json={"refreshToken": tokens["refreshToken"]})
    again = await client.post("/refresh", json={"refreshToken": tokens["refreshToken"]})

    assert out.status_code == 204
    assert again.status_code == 401


async def test_update_me(api, client, outbox_types):
    await api.register()
    tokens = await api.login()

    response = await client.patch(
        "/me",
        headers=api.bearer(tokens),
        json={"name": "Asha Devi", "email": "asha@example.com", "homeLat": 21.19, "homeLng": 81.38},
    )

    assert response.status_code == 200
    assert response.json()["homeLat"] == 21.19
    assert await outbox_types() == ["user.registered", "user.updated"]


async def test_add_own_role_shows_in_next_token(api, client):
    await api.register()
    tokens = await api.login()

    added = await client.post("/me/roles", headers=api.bearer(tokens), json={"role": "farmer"})
    refreshed = (
        await client.post("/refresh", json={"refreshToken": tokens["refreshToken"]})
    ).json()
    check = await client.get("/internal/auth/verify", headers=api.bearer(refreshed))

    assert added.json()["roles"] == ["customer", "farmer"]
    assert check.headers["X-User-Roles"] == "customer,farmer"


async def test_cannot_grant_self_admin(api, client):
    await api.register()
    tokens = await api.login()

    response = await client.post("/me/roles", headers=api.bearer(tokens), json={"role": "admin"})

    assert response.status_code == 422


async def test_me_requires_token(api, client):
    response = await client.get("/me")

    assert response.status_code == 401
