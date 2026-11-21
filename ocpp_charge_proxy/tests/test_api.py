import pytest
import pytest_asyncio
from src.api import create_api_app
from src.shared_state import SharedState


@pytest.fixture
def shared_state():
    return SharedState()


@pytest.fixture
def mock_commands():
    calls = []

    async def plug():
        calls.append("plug")

    async def unplug():
        calls.append("unplug")

    async def set_current(amps: int):
        calls.append(("set_current", amps))

    return {"plug": plug, "unplug": unplug, "set_current": set_current, "calls": calls}


@pytest.fixture
def app(shared_state, mock_commands):
    return create_api_app(
        shared_state,
        on_plug=mock_commands["plug"],
        on_unplug=mock_commands["unplug"],
        on_set_current=mock_commands["set_current"],
    )


@pytest_asyncio.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_get_state(client, shared_state):
    shared_state.state = "Charging"
    shared_state.connected_to_server = True
    resp = await client.get("/api/state")
    assert resp.status == 200
    data = await resp.json()
    assert data["state"] == "Charging"
    assert data["connected_to_server"] is True


@pytest.mark.asyncio
async def test_post_plug(client, mock_commands):
    resp = await client.post("/api/plug")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert "plug" in mock_commands["calls"]


@pytest.mark.asyncio
async def test_post_unplug(client, mock_commands):
    resp = await client.post("/api/unplug")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert "unplug" in mock_commands["calls"]


@pytest.mark.asyncio
async def test_post_current_valid(client, mock_commands):
    resp = await client.post("/api/current", json={"amps": 16})
    assert resp.status == 200
    assert ("set_current", 16) in mock_commands["calls"]


@pytest.mark.asyncio
async def test_post_current_invalid(client):
    resp = await client.post("/api/current", json={"amps": 15})
    assert resp.status == 400
    data = await resp.json()
    assert data["status"] == "error"


@pytest.mark.asyncio
async def test_post_current_missing_body(client):
    resp = await client.post("/api/current")
    assert resp.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [16.9, True, "16", None, {}, float("inf")])
async def test_current_requires_integer_without_coercion(client, mock_commands, value):
    response = await client.post("/api/current", json={"amps": value})
    assert response.status == 400
    assert not mock_commands["calls"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), True, "2.4", {}, []]
)
async def test_power_rejects_nonfinite_or_nonnumeric(aiohttp_client, value):
    calls = []

    async def set_power(power):
        calls.append(power)

    client = await aiohttp_client(create_api_app(SharedState(), on_set_power=set_power))
    response = await client.post("/api/power", json={"power_kw": value})
    assert response.status == 400
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, [], None])
async def test_power_requires_explicit_value(aiohttp_client, body):
    async def set_power(power):
        raise AssertionError("must not mutate")

    client = await aiohttp_client(create_api_app(SharedState(), on_set_power=set_power))
    assert (await client.post("/api/power", json=body)).status == 400
