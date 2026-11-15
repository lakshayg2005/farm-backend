async def test_admin_routes_need_admin_role(api, client):
    await api.register()
    tokens = await api.login()

    response = await client.get("/users", headers=api.bearer(tokens))

    assert response.status_code == 403


async def test_admin_lists_users_by_role_with_paging(api, client, make_admin):
    await make_admin()
    await api.register(phone="9000000011", roles=["farmer"])
    await api.register(phone="9000000012", roles=["farmer"])
    await api.register(phone="9000000013", roles=["customer"])
    admin = await api.login("+919000000001")

    page1 = (await client.get("/users?role=farmer&limit=1", headers=api.bearer(admin))).json()
    page2 = (
        await client.get(
            f"/users?role=farmer&limit=1&cursor={page1['nextCursor']}", headers=api.bearer(admin)
        )
    ).json()

    assert len(page1["items"]) == 1
    assert len(page2["items"]) == 1
    assert page2["nextCursor"] is None
    assert {page1["items"][0]["phone"], page2["items"][0]["phone"]} == {
        "+919000000011",
        "+919000000012",
    }


async def test_verify_farmer_publishes_event(api, client, make_admin, outbox_types):
    await make_admin()
    farmer = (await api.register(phone="9000000011", roles=["farmer"])).json()
    customer = (await api.register(phone="9000000012", roles=["customer"])).json()
    admin = await api.login("+919000000001")

    ok = await client.post(f"/users/{farmer['id']}/verify-farmer", headers=api.bearer(admin))
    not_farmer = await client.post(
        f"/users/{customer['id']}/verify-farmer", headers=api.bearer(admin)
    )

    assert ok.json()["farmerVerified"] is True
    assert not_farmer.status_code == 409
    assert (await outbox_types())[-1] == "farmer.verified"


async def test_admin_grants_inspector_and_ends_sessions(api, client, make_admin):
    await make_admin()
    user = (await api.register()).json()
    user_tokens = await api.login()
    admin = await api.login("+919000000001")

    response = await client.put(
        f"/users/{user['id']}/roles", headers=api.bearer(admin), json={"roles": ["inspector"]}
    )
    old_refresh = await client.post("/refresh", json={"refreshToken": user_tokens["refreshToken"]})

    assert response.json()["roles"] == ["inspector"]
    assert old_refresh.status_code == 401


async def test_forward_auth(api, client):
    await api.register()
    tokens = await api.login()

    missing = await client.get("/internal/auth/verify")
    ok = await client.get("/internal/auth/verify", headers=api.bearer(tokens))

    assert missing.status_code == 401
    assert ok.status_code == 200
    assert ok.headers["X-User-Roles"] == "customer"
    assert len(ok.headers["X-User-Id"]) == 36


async def test_internal_user_lookup(api, client):
    user = (await api.register()).json()

    response = await client.get(f"/internal/users/{user['id']}")

    assert response.json()["name"] == "Asha"


async def test_public_key(api, client):
    response = await client.get("/public-key")

    assert response.json()["publicKey"].startswith("-----BEGIN PUBLIC KEY-----")
    assert response.json()["algorithm"] == "RS256"
