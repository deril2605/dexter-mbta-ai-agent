"""Milestone 2 — route-first resolution (the highest-risk milestone).

Covers: known stop resolves; stop ambiguity yields candidates; route-first
scoping of the /stops fetch; direction resolved from direction_destinations;
missing direction yields a direction question; bad/missing route asks for
clarification. All HTTP is respx-mocked.
"""

from __future__ import annotations

import httpx
import pytest

from dexter.mbta.client import MBTAClient
from dexter.mbta.models import Disambiguation, DisambiguationKind, ResolvedTarget
from dexter.mbta.resolution import Resolver
from dexter.mbta.routes import RouteCache

from .conftest import MBTA_BASE_URL, ROUTES_PAYLOAD


def stops_payload(*stops: tuple[str, str]) -> dict:
    """Build a /stops JSON:API payload from (id, name) pairs."""
    return {
        "data": [{"type": "stop", "id": sid, "attributes": {"name": name}} for sid, name in stops]
    }


@pytest.fixture
def mock_routes(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json=ROUTES_PAYLOAD)
    )
    return respx_mock


def make_resolver(client: MBTAClient) -> Resolver:
    return Resolver(client, RouteCache(client))


# --- Happy path -------------------------------------------------------------


async def test_resolves_route_stop_and_direction(mock_routes):
    stops = mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(
            200,
            json=stops_payload(
                ("70", "Maverick Station"),
                ("71", "Bennington St @ Brooks St"),
                ("72", "Wonderland"),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve(
            route_token="116", location="Bennington Street", direction_hint="toward Maverick"
        )

    assert isinstance(result, ResolvedTarget)
    assert result.route_id == "116"
    assert result.route_name == "116"
    assert result.stop_ids == ("71",)
    assert result.stop_name == "Bennington St @ Brooks St"
    assert result.direction_id == 1  # dests = ("Wonderland", "Maverick")
    assert result.direction_destination == "Maverick"
    assert stops.called


async def test_stops_fetch_is_scoped_to_the_route(mock_routes):
    stops = mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        await make_resolver(client).resolve("116", "Maverick", "Wonderland")

    # Route-first: the stop fetch must be filtered to this route only.
    assert stops.calls.last.request.url.params["filter[route]"] == "116"


# --- Stop disambiguation ----------------------------------------------------


async def test_ambiguous_stop_yields_candidates(mock_routes):
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(
            200,
            json=stops_payload(
                ("71", "Bennington St @ Brooks St"),
                ("72", "Bennington St @ Boardman St"),
                ("70", "Maverick Station"),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("116", "Bennington St", "Maverick")

    assert isinstance(result, Disambiguation)
    assert result.kind is DisambiguationKind.STOP
    assert result.route_id == "116"
    labels = {opt.label for opt in result.options}
    assert "Bennington St @ Brooks St" in labels
    assert "Bennington St @ Boardman St" in labels
    # Each option carries the stop_ids needed to resolve on the next turn.
    assert all(opt.stop_ids for opt in result.options)


async def test_duplicate_named_stops_collapse_to_one_choice(mock_routes):
    # Two platform records share a name (the directional-platform case). They must
    # collapse into one resolved stop carrying BOTH ids — not a duplicated option.
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(
            200,
            json=stops_payload(
                ("11", "S Huntington Ave @ Huntington Ave"),
                ("22", "S Huntington Ave @ Huntington Ave"),
                ("70", "Maverick Station"),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve(
            "116", "S Huntington Ave at Huntington Ave", "Maverick"
        )

    assert isinstance(result, ResolvedTarget)
    assert set(result.stop_ids) == {"11", "22"}  # both platforms kept
    assert result.stop_name == "S Huntington Ave @ Huntington Ave"


async def test_distinctive_word_wins_over_fuzzy_noise(mock_routes):
    # "Eutaw St" should resolve to the one stop containing "Eutaw" — not get lost
    # among stops that merely share the common word "St".
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(
            200,
            json=stops_payload(
                ("1", "Meridian St @ Eutaw St"),
                ("2", "Western Ave @ Cooper St"),
                ("3", "Salem Tnpk @ Ballard St"),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("116", "Eutaw St", "Maverick")

    assert isinstance(result, ResolvedTarget)
    assert result.stop_ids == ("1",)
    assert result.stop_name == "Meridian St @ Eutaw St"


async def test_exact_stop_name_beats_token_sharing_rival(mock_routes):
    # "Park Street" must win outright over "Mission Park" (they only share "park").
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(
            200,
            json=stops_payload(
                ("1", "Park Street"),
                ("2", "Mission Park"),
                ("70", "Maverick Station"),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("116", "Park Street", "Maverick")

    assert isinstance(result, ResolvedTarget)
    assert result.stop_name == "Park Street"
    assert result.stop_ids == ("1",)


async def test_unknown_stop_yields_stop_clarification(mock_routes):
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(
            200, json=stops_payload(("70", "Maverick Station"), ("72", "Wonderland"))
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("116", "Zxqw Plaza", "Maverick")

    assert isinstance(result, Disambiguation)
    assert result.kind is DisambiguationKind.STOP


# --- Direction disambiguation ----------------------------------------------


async def test_missing_direction_asks_which_way(mock_routes):
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("116", "Maverick", direction_hint=None)

    assert isinstance(result, Disambiguation)
    assert result.kind is DisambiguationKind.DIRECTION
    # Options come from this route's direction_destinations, carrying direction_id.
    labels = [opt.label for opt in result.options]
    assert labels == ["Wonderland", "Maverick"]
    assert {opt.direction_id for opt in result.options} == {0, 1}
    # Carried context so the follow-up only needs the direction.
    assert result.route_id == "116"
    assert result.stop_ids == ("70",)
    assert result.stop_name == "Maverick Station"


async def test_direction_hint_resolves_other_way(mock_routes):
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("116", "Maverick", "Wonderland")

    assert isinstance(result, ResolvedTarget)
    assert result.direction_id == 0
    assert result.direction_destination == "Wonderland"


# --- Direction inference from two stops -------------------------------------

# Blue Line: dir 0 -> Bowdoin (west), dir 1 -> Wonderland (east). Stop order
# travelling toward Bowdoin; the eastbound list is just its reverse.
_BLUE_ORDER_TO_BOWDOIN = ["wonderland", "airport", "maverick", "gov", "bowdoin"]
_BLUE_STOPS = stops_payload(
    ("airport", "Airport"),
    ("gov", "Government Center"),
    ("maverick", "Maverick"),
    ("bowdoin", "Bowdoin"),
    ("wonderland", "Wonderland"),
)


def _ordered_payload(ids: list[str]) -> dict:
    return {"data": [{"type": "stop", "id": sid, "attributes": {}} for sid in ids]}


def _blue_stops_handler(request: httpx.Request) -> httpx.Response:
    """Serve the route's stops, and the per-direction ordered ids for inference."""
    direction = request.url.params.get("filter[direction_id]")
    if direction is None:
        return httpx.Response(200, json=_BLUE_STOPS)
    order = _BLUE_ORDER_TO_BOWDOIN if direction == "0" else list(reversed(_BLUE_ORDER_TO_BOWDOIN))
    return httpx.Response(200, json=_ordered_payload(order))


async def test_two_stops_infer_direction_without_asking(mock_routes):
    # "blue line from airport to government center" — Government Center isn't a
    # terminus, but it sits toward Bowdoin from Airport, so direction is unambiguous.
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(side_effect=_blue_stops_handler)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve(
            "Blue Line", "Airport", direction_hint="Government Center"
        )

    assert isinstance(result, ResolvedTarget)
    assert result.direction_id == 0
    assert result.direction_destination == "Bowdoin"
    assert result.stop_name == "Airport"


async def test_two_stops_reverse_infers_opposite_direction(mock_routes):
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(side_effect=_blue_stops_handler)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve(
            "Blue Line", "Government Center", direction_hint="Airport"
        )

    assert isinstance(result, ResolvedTarget)
    assert result.direction_id == 1
    assert result.direction_destination == "Wonderland"


async def test_terminus_hint_skips_inference(mock_routes):
    # A terminus hint resolves directly — it must NOT make the per-direction fetch.
    stops = mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(side_effect=_blue_stops_handler)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("Blue Line", "Airport", "Bowdoin")

    assert isinstance(result, ResolvedTarget)
    assert result.direction_id == 0
    assert all("filter[direction_id]" not in str(call.request.url) for call in stops.calls)


async def test_destination_off_route_falls_back_to_direction_question(mock_routes):
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(side_effect=_blue_stops_handler)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("Blue Line", "Airport", "Harvard")

    assert isinstance(result, Disambiguation)
    assert result.kind is DisambiguationKind.DIRECTION


async def test_same_origin_and_destination_falls_back(mock_routes):
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(side_effect=_blue_stops_handler)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("Blue Line", "Airport", "Airport")

    assert isinstance(result, Disambiguation)
    assert result.kind is DisambiguationKind.DIRECTION


# --- Green Line: a generic line token, stop picks the branch ----------------

_GREEN_BRANCH_STOPS = {
    "Green-B": (("place-pktrm", "Park Street"), ("place-gover", "Government Center"),
                ("place-lake", "Boston College")),
    "Green-C": (("place-pktrm", "Park Street"), ("place-gover", "Government Center"),
                ("place-clmnl", "Cleveland Circle")),
    "Green-D": (("place-pktrm", "Park Street"), ("place-unsqu", "Union Square"),
                ("place-river", "Riverside")),
    "Green-E": (("place-pktrm", "Park Street"), ("place-nuniv", "Northeastern University"),
                ("place-mispk", "Mission Park"), ("place-hsmnl", "Heath Street")),
}  # fmt: skip


def _green_stops_handler(request: httpx.Request) -> httpx.Response:
    route = request.url.params.get("filter[route]")
    return httpx.Response(200, json=stops_payload(*_GREEN_BRANCH_STOPS.get(route, ())))


async def test_green_line_branch_stop_selects_correct_branch(mock_routes):
    # The reported bug: "green line from Northeastern" must resolve to the E branch
    # (Heath St / Medford-Tufts), never Green-B (Boston College).
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(side_effect=_green_stops_handler)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve(
            "green line", "Northeastern University", "Heath Street"
        )

    assert isinstance(result, ResolvedTarget)
    assert result.route_id == "Green-E"
    assert result.route_name == "Green Line"
    assert result.direction_destination == "Heath Street"
    assert result.direction_id == 0


async def test_green_line_branch_stop_asks_only_that_branch_directions(mock_routes):
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(side_effect=_green_stops_handler)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("green line", "Northeastern University", None)

    assert isinstance(result, Disambiguation)
    assert result.kind is DisambiguationKind.DIRECTION
    labels = {o.label for o in result.options}
    assert labels == {"Heath Street", "Medford/Tufts"}  # E branch only
    assert "Boston College" not in labels  # the old wrong answer
    assert result.route_id == "Green-E"


async def test_green_line_trunk_stop_with_destination_narrows_branches(mock_routes):
    # Park Street is on every branch; "toward Government Center" keeps only B and C.
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(side_effect=_green_stops_handler)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve(
            "green line", "Park Street", "Government Center"
        )

    assert isinstance(result, ResolvedTarget)
    assert result.route_id == "Green-B,Green-C"
    assert result.direction_destination == "Government Center"
    assert result.direction_id == 1


async def test_green_line_trunk_stop_offers_all_destinations(mock_routes):
    mock_routes.get(f"{MBTA_BASE_URL}/stops").mock(side_effect=_green_stops_handler)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("green line", "Park Street", None)

    assert isinstance(result, Disambiguation)
    assert result.kind is DisambiguationKind.DIRECTION
    assert result.route_id == "Green-B,Green-C,Green-D,Green-E"
    labels = {o.label for o in result.options}
    assert {"Boston College", "Heath Street", "Riverside"} <= labels


async def test_green_line_direction_answer_narrows_to_one_branch(mock_routes):
    # The clarify path: answering "Heath Street" at a trunk stop narrows to Green-E.
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve_with_ids(
            route_id="Green-B,Green-C,Green-D,Green-E",
            stop_ids=("place-pktrm",),
            stop_name="Park Street",
            direction_hint="Heath Street",
        )

    assert isinstance(result, ResolvedTarget)
    assert result.route_id == "Green-E"
    assert result.direction_destination == "Heath Street"


# --- Route clarification ----------------------------------------------------


async def test_unrecognized_route_asks_for_clarification(mock_routes):
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve("999", "Maverick", "Maverick")

    assert isinstance(result, Disambiguation)
    assert result.kind is DisambiguationKind.ROUTE
    assert result.query == "999"


async def test_missing_route_asks_for_clarification(mock_routes):
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await make_resolver(client).resolve(None, "Maverick", "Maverick")

    assert isinstance(result, Disambiguation)
    assert result.kind is DisambiguationKind.ROUTE
