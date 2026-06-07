"""Route-first resolution — the core of the MBTA library (PRD §5.3).

Given parsed slots (route token, location phrase, direction hint), resolve them
to a concrete ``(route_id, stop_id, direction_id)`` :class:`ResolvedTarget`, or
return a :class:`Disambiguation` when something is ambiguous or missing.

The order is deliberate and non-negotiable:

1. **Route** first — resolve the route token to a route.
2. **Stop** scoped to that route — fetch only ``/stops?filter[route]=...`` and
   fuzzy-match the location within that small set. We never fuzzy-match across
   all MBTA stops.
3. **Direction** from the route's ``direction_destinations`` — never a hardcoded
   ``direction_id`` 0/1, since its meaning differs per route.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz, process

from .client import MBTAClient
from .models import (
    ROUTE_TYPE_LIGHT_RAIL,
    Disambiguation,
    DisambiguationKind,
    DisambiguationOption,
    ResolvedTarget,
    Route,
    Stop,
)
from .routes import RouteCache

# Per-route /stops cache TTL (PRD §5.7: ~6h).
DEFAULT_STOPS_TTL = 6 * 60 * 60
_STOP_FIELDS = "name"

# Stop fuzzy-match thresholds (rapidfuzz WRatio, 0–100).
STOP_ACCEPT = 82.0  # a match this strong can win outright
STOP_MARGIN = 12.0  # ...if it also beats the runner-up by this much
STOP_CANDIDATE_FLOOR = 60.0  # plausible enough to offer as a disambiguation option
MAX_STOP_CANDIDATES = 3

DIRECTION_CUTOFF = 80.0

# The Green Line is a family of branch routes (Green-B/C/D/E) sharing this id prefix.
# A generic "Green Line" token is resolved across all branches, with the named stop
# selecting the branch(es); `_GREEN_CARRY` is the token re-used when we must re-ask.
_GREEN_BRANCH_PREFIX = "Green-"
_GREEN_LINE_TOKENS = frozenset({"green", "green line", "the green line", "greenline"})
_GREEN_CARRY = "green line"

# Generic street-type words and connectors. Stripped from a location before
# checking which stops actually contain its *distinctive* words, so "Eutaw St"
# is matched on "eutaw" — not on the shared, meaningless "st".
_STREET_WORDS = frozenset(
    {
        "st", "street", "ave", "av", "avenue", "rd", "road", "sq", "square",
        "ln", "lane", "pl", "place", "dr", "drive", "ct", "court", "ter", "terr",
        "terrace", "hwy", "highway", "tnpk", "turnpike", "blvd", "boulevard",
        "pkwy", "parkway", "cir", "circle", "row", "way", "walk", "path", "opp",
        "at", "the", "and", "of", "to", "toward", "towards", "near", "by",
        "station", "stop",
    }
)  # fmt: skip

# Filler words stripped from a direction hint before matching ("toward Maverick").
_DIRECTION_FILLER = {
    "toward",
    "towards",
    "to",
    "for",
    "the",
    "bound",
    "heading",
    "going",
    "into",
    "in",
    "direction",
    "of",
    "travel",
}


def normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _significant_tokens(norm: str) -> list[str]:
    """The distinctive words of a location (drop short/common street words)."""
    return [t for t in norm.split() if len(t) >= 3 and t not in _STREET_WORDS]


def _contains_all(name: str, tokens: list[str]) -> bool:
    name_tokens = set(normalize(name).split())
    return all(t in name_tokens for t in tokens)


def _first_index(ordered_ids: list[str], ids: tuple[str, ...]) -> int | None:
    """Earliest position in ``ordered_ids`` of any of a stop's platform ``ids``."""
    wanted = set(ids)
    for i, sid in enumerate(ordered_ids):
        if sid in wanted:
            return i
    return None


def _is_green_line_token(token: str) -> bool:
    """True for a generic 'Green Line' token (not a specific branch like 'Green Line E')."""
    return normalize(token) in _GREEN_LINE_TOKENS


def _green_target(
    branch_ids: list[str],
    stop_ids: tuple[str, ...],
    stop_name: str,
    direction_id: int,
    dest: str,
) -> ResolvedTarget:
    """A Green Line target spanning one or more branch routes (comma-joined route_id)."""
    return ResolvedTarget(
        route_id=",".join(branch_ids),
        route_name="Green Line",
        stop_ids=stop_ids,
        stop_name=stop_name,
        direction_id=direction_id,
        direction_destination=dest,
        route_type=ROUTE_TYPE_LIGHT_RAIL,
    )


@dataclass(frozen=True, slots=True)
class _StopGroup:
    """One human-facing stop name and every stop_id that shares it.

    MBTA returns separate stop records per direction at a corner, so a single
    name (e.g. "S Huntington Ave @ Huntington Ave") can map to several stop_ids.
    """

    name: str
    ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StopOutcome:
    winner: _StopGroup | None
    candidates: tuple[_StopGroup, ...]


class Resolver:
    """Resolves slots to a :class:`ResolvedTarget` or a :class:`Disambiguation`."""

    def __init__(
        self,
        client: MBTAClient,
        route_cache: RouteCache,
        *,
        stops_ttl: float = DEFAULT_STOPS_TTL,
    ) -> None:
        self._client = client
        self._routes = route_cache
        self._stops_ttl = stops_ttl

    @property
    def client(self) -> MBTAClient:
        """The shared MBTA client (so the agent can build sibling skill services)."""
        return self._client

    @property
    def routes(self) -> RouteCache:
        """The shared route cache (reused by the alerts/facilities skills)."""
        return self._routes

    async def stops_for(self, route_id: str, location: str | None) -> tuple[str, ...] | None:
        """Resolve a location to concrete stop_ids on a route, or None if unclear.

        Used by the alerts skill to optionally narrow alerts to a stop. Unlike
        :meth:`resolve` it does no direction handling and returns ids only.
        """
        if not location or not location.strip():
            return None
        groups = _group_by_name(await self._fetch_stops(route_id))
        winner = _match_stop(location, groups).winner
        return winner.ids if winner else None

    async def resolve(
        self,
        route_token: str | None,
        location: str | None,
        direction_hint: str | None = None,
    ) -> ResolvedTarget | Disambiguation:
        # Step 1 — route.
        if not route_token or not route_token.strip():
            return Disambiguation(kind=DisambiguationKind.ROUTE)
        # The Green Line spans four branch routes; let the named stop pick the branch.
        if _is_green_line_token(route_token):
            return await self._resolve_green(location, direction_hint)
        route = await self._routes.lookup(route_token)
        if route is None:
            return Disambiguation(kind=DisambiguationKind.ROUTE, query=route_token)

        # Step 2 — stop, scoped to this route's stops only.
        if not location or not location.strip():
            return Disambiguation(kind=DisambiguationKind.STOP, route_id=route.id)
        groups = _group_by_name(await self._fetch_stops(route.id))
        outcome = _match_stop(location, groups)
        if outcome.winner is None:
            options = tuple(
                DisambiguationOption(label=g.name, stop_ids=g.ids) for g in outcome.candidates
            )
            return Disambiguation(
                kind=DisambiguationKind.STOP,
                options=options,
                route_id=route.id,
                query=location,
            )

        # Step 3 — direction: a terminus/direction hint, else inferred from a
        # destination stop ("airport to Government Center" -> the Bowdoin direction).
        direction_id = await self._direction_for(route, outcome.winner, groups, direction_hint)
        return _complete(route, outcome.winner.ids, outcome.winner.name, direction_id)

    async def resolve_with_ids(
        self,
        *,
        route_id: str,
        stop_ids: tuple[str, ...],
        stop_name: str,
        direction_hint: str | None,
    ) -> ResolvedTarget | Disambiguation:
        """Finish resolution once a concrete stop has been chosen.

        Used by disambiguation follow-ups: the stop is already a concrete set of
        ids (no more fuzzy matching), so only direction remains.
        """
        # A Green Line target carries branch route_id(s) like "Green-E" or
        # "Green-B,Green-C"; finish it across those branches (may re-narrow on the
        # chosen destination), never as a single ordinary route.
        if route_id.startswith(_GREEN_BRANCH_PREFIX):
            return await self._green_direction(
                route_id.split(","), stop_ids, stop_name, direction_hint
            )
        route = await self._routes.get(route_id)
        if route is None:  # route cache changed under us — ask again
            return Disambiguation(kind=DisambiguationKind.ROUTE)
        # A disambiguation answer carries a concrete direction (the chosen option's
        # label) — match it directly; no destination-stop inference needed here.
        direction_id = _resolve_direction(route, direction_hint)
        return _complete(route, stop_ids, stop_name, direction_id)

    async def _resolve_green(
        self, location: str | None, direction_hint: str | None
    ) -> ResolvedTarget | Disambiguation:
        """Resolve a generic 'Green Line' query by letting the named stop pick the branch.

        The Green Line is four branch routes (Green-B/C/D/E). We match the stop across
        all of them: a branch-only stop (Northeastern -> E) resolves to that branch; a
        trunk stop (Park Street) keeps every serving branch so predictions cover them all,
        and the chosen destination later narrows back to the right branch(es).
        """
        branches = [
            r for r in await self._routes.all_routes() if r.id.startswith(_GREEN_BRANCH_PREFIX)
        ]
        if not branches:
            return Disambiguation(kind=DisambiguationKind.ROUTE, query="Green Line")
        if not location or not location.strip():
            return Disambiguation(kind=DisambiguationKind.STOP, route_id=_GREEN_CARRY)

        all_stops: list[Stop] = []
        serving: dict[str, set[str]] = {}
        for branch in branches:
            stops = await self._fetch_stops(branch.id)
            all_stops.extend(stops)
            for stop in stops:
                key = normalize(stop.name)
                if key:
                    serving.setdefault(key, set()).add(branch.id)

        outcome = _match_stop(location, _group_by_name(all_stops))
        if outcome.winner is None:
            options = tuple(
                DisambiguationOption(
                    label=g.name,
                    stop_ids=g.ids,
                    route_id=",".join(sorted(serving.get(normalize(g.name), set()))),
                )
                for g in outcome.candidates
            )
            return Disambiguation(
                kind=DisambiguationKind.STOP, options=options, route_id=_GREEN_CARRY, query=location
            )

        branch_ids = sorted(serving.get(normalize(outcome.winner.name), set()))
        return await self._green_direction(
            branch_ids, outcome.winner.ids, outcome.winner.name, direction_hint
        )

    async def _green_direction(
        self,
        branch_ids: list[str],
        stop_ids: tuple[str, ...],
        stop_name: str,
        direction_hint: str | None,
    ) -> ResolvedTarget | Disambiguation:
        """Pick the Green Line direction across the serving branches, or ask which."""
        branches: list[Route] = []
        for bid in branch_ids:
            branch = await self._routes.get(bid)
            if branch is not None:
                branches.append(branch)
        if not branches:
            return Disambiguation(kind=DisambiguationKind.ROUTE, query="Green Line")

        # A destination/direction hint: keep only the branches that actually go there.
        if direction_hint and direction_hint.strip():
            matched = []  # (branch_id, direction_id, destination)
            for branch in branches:
                d = _resolve_direction(branch, direction_hint)
                if d is not None:
                    matched.append((branch.id, d, branch.direction_destinations[d]))
            if matched:
                direction_id = matched[0][1]
                dest = matched[0][2]
                narrowed = sorted({bid for bid, d, x in matched if d == direction_id and x == dest})
                return _green_target(narrowed, stop_ids, stop_name, direction_id, dest)

        # No usable hint: offer the distinct destinations across the serving branches.
        options: list[DisambiguationOption] = []
        seen: set[tuple[str, int]] = set()
        for branch in branches:
            for i, dest in enumerate(branch.direction_destinations):
                if (dest, i) not in seen:
                    seen.add((dest, i))
                    options.append(DisambiguationOption(label=dest, direction_id=i))
        return Disambiguation(
            kind=DisambiguationKind.DIRECTION,
            options=tuple(options),
            route_id=",".join(sorted(b.id for b in branches)),
            stop_ids=stop_ids,
            stop_name=stop_name,
        )

    async def _fetch_stops(self, route_id: str) -> list[Stop]:
        data = await self._client.get_json(
            "/stops",
            params={"filter[route]": route_id, "fields[stop]": _STOP_FIELDS},
            cache_ttl=self._stops_ttl,
        )
        return [Stop.from_jsonapi(r) for r in data.get("data", [])]

    async def _fetch_ordered_stop_ids(self, route_id: str, direction_id: int) -> list[str]:
        """Stop ids for one direction, in travel order.

        MBTA returns ``/stops`` sorted by sequence when filtered by route+direction,
        which is what lets us tell whether the origin comes before the destination.
        """
        data = await self._client.get_json(
            "/stops",
            params={
                "filter[route]": route_id,
                "filter[direction_id]": direction_id,
                "fields[stop]": _STOP_FIELDS,
            },
            cache_ttl=self._stops_ttl,
        )
        return [r["id"] for r in data.get("data", []) if r.get("id")]

    async def _direction_for(
        self,
        route: Route,
        origin: _StopGroup,
        groups: list[_StopGroup],
        direction_hint: str | None,
    ) -> int | None:
        """Direction from a terminus/direction hint, else inferred from a destination stop.

        A hint like "Bowdoin" or "inbound" matches a terminus/direction name directly
        (the fast path). A hint that is instead an *intermediate* stop ("Government
        Center") is resolved as a stop on the route and the direction is inferred from
        stop order — so a plain "from A to B" no longer needs the line's endpoint named.
        """
        direct = _resolve_direction(route, direction_hint)
        if direct is not None:
            return direct
        if not direction_hint or not direction_hint.strip():
            return None
        dest = _match_stop(direction_hint, groups).winner
        if dest is None or dest.ids == origin.ids:
            return None
        return await self._infer_direction(route, origin, dest)

    async def _infer_direction(
        self, route: Route, origin: _StopGroup, dest: _StopGroup
    ) -> int | None:
        """The direction_id whose stop order has origin before dest, or None if unclear.

        Conservative by design: a direction wins only if both stops appear on it and
        origin precedes dest. If neither or both directions qualify (branch mismatch,
        ambiguity), return None so the caller falls back to asking — never guess wrong.
        """
        matches = []
        for direction_id in range(len(route.direction_destinations)):
            ordered = await self._fetch_ordered_stop_ids(route.id, direction_id)
            o = _first_index(ordered, origin.ids)
            d = _first_index(ordered, dest.ids)
            if o is not None and d is not None and o < d:
                matches.append(direction_id)
        return matches[0] if len(matches) == 1 else None


def _complete(
    route: Route, stop_ids: tuple[str, ...], stop_name: str, direction_id: int | None
) -> ResolvedTarget | Disambiguation:
    """Build a target for a known route+stop+direction, or ask which direction."""
    if direction_id is None:
        if len(route.direction_destinations) >= 2:
            options = tuple(
                DisambiguationOption(label=dest, direction_id=i)
                for i, dest in enumerate(route.direction_destinations)
            )
            return Disambiguation(
                kind=DisambiguationKind.DIRECTION,
                options=options,
                route_id=route.id,
                stop_ids=stop_ids,
                stop_name=stop_name,
            )
        direction_id = 0  # only one direction of travel exists

    return ResolvedTarget(
        route_id=route.id,
        route_name=route.display_name,
        stop_ids=stop_ids,
        stop_name=stop_name,
        direction_id=direction_id,
        direction_destination=_destination_label(route, direction_id),
        route_type=route.type,
    )


def _group_by_name(stops: list[Stop]) -> list[_StopGroup]:
    """Collapse stops with the same name, preserving first-seen order."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for stop in stops:
        key = normalize(stop.name)
        if not key:
            continue
        if key not in groups:
            groups[key] = [stop.name, [stop.id]]
            order.append(key)
        else:
            groups[key][1].append(stop.id)
    return [_StopGroup(name=groups[k][0], ids=tuple(groups[k][1])) for k in order]


def _match_stop(location: str, groups: list[_StopGroup]) -> _StopOutcome:
    """Fuzzy-match a location phrase against unique stop names.

    Only names scoring at least ``STOP_CANDIDATE_FLOOR`` are "plausible". Then:
    - none plausible -> ask which stop (no usable candidates);
    - exactly one plausible -> it wins (no competitor to confuse it with);
    - several plausible -> the top wins only if it clearly beats the runner-up
      (>= ``STOP_ACCEPT`` and ahead by ``STOP_MARGIN``); otherwise the close
      matches become disambiguation candidates.
    """
    norm = normalize(location)
    scored = sorted(
        ((fuzz.WRatio(norm, normalize(g.name)), g) for g in groups),
        key=lambda pair: pair[0],
        reverse=True,
    )

    # Distinctive-word containment first: a stop that actually contains the
    # location's significant words ("Eutaw") beats fuzzy matches that merely share
    # a common word ("St"). One container -> it wins; several -> disambiguate
    # among the real matches, ignoring the fuzzy noise entirely.
    significant = _significant_tokens(norm)
    if significant:
        contained = [g for _, g in scored if _contains_all(g.name, significant)]
        # An exact name match wins outright over rivals that merely share a token
        # ("Park Street" beats "Mission Park", which only shares "park").
        exact = [g for g in contained if normalize(g.name) == norm]
        if len(exact) == 1:
            return _StopOutcome(winner=exact[0], candidates=())
        if len(contained) == 1:
            return _StopOutcome(winner=contained[0], candidates=())
        if len(contained) >= 2:
            return _StopOutcome(winner=None, candidates=tuple(contained[:MAX_STOP_CANDIDATES]))

    plausible = [(score, group) for score, group in scored if score >= STOP_CANDIDATE_FLOOR]

    if not plausible:
        return _StopOutcome(winner=None, candidates=())
    if len(plausible) == 1:
        return _StopOutcome(winner=plausible[0][1], candidates=())

    best_score, best_group = plausible[0]
    second_score = plausible[1][0]
    if best_score >= STOP_ACCEPT and (best_score - second_score) >= STOP_MARGIN:
        return _StopOutcome(winner=best_group, candidates=())

    candidates = tuple(group for _, group in plausible[:MAX_STOP_CANDIDATES])
    return _StopOutcome(winner=None, candidates=candidates)


def _resolve_direction(route: Route, hint: str | None) -> int | None:
    """Map a direction hint to a ``direction_id`` using this route's data.

    Primary match is against ``direction_destinations`` ("toward Maverick");
    ``direction_names`` ("inbound"/"eastbound") are also accepted. Returns None
    when no confident match — the caller then asks which direction.
    """
    if hint is None or not hint.strip():
        return None
    cleaned = " ".join(t for t in normalize(hint).split() if t not in _DIRECTION_FILLER)
    if not cleaned:
        return None

    dests = [normalize(d) for d in route.direction_destinations]
    names = [normalize(n) for n in route.direction_names]

    # 1. Exact destination ("maverick").
    for i, dest in enumerate(dests):
        if dest == cleaned:
            return i
    # 2. Direction name ("inbound", "eastbound", "east").
    for i, name in enumerate(names):
        if _name_matches(name, cleaned):
            return i
    # 3. Confident fuzzy destination match.
    best = process.extractOne(cleaned, dests, scorer=fuzz.WRatio, score_cutoff=DIRECTION_CUTOFF)
    if best is not None:
        return best[2]
    # 4. Substring either way ("wonder" -> "wonderland").
    for i, dest in enumerate(dests):
        if cleaned in dest or dest in cleaned:
            return i
    return None


def _name_matches(name_norm: str, cleaned: str) -> bool:
    base_name = name_norm.removesuffix("bound")
    base_hint = cleaned.removesuffix("bound")
    return (
        cleaned == name_norm
        or base_hint == base_name
        or base_hint == name_norm
        or cleaned == base_name
    )


def _destination_label(route: Route, direction_id: int) -> str:
    dests = route.direction_destinations
    if 0 <= direction_id < len(dests):
        return dests[direction_id]
    return ""


# Cutoff for matching a free-text disambiguation answer to one of the options.
OPTION_MATCH_CUTOFF = 70.0


def match_disambiguation_option(
    pending: Disambiguation, answer: str
) -> DisambiguationOption | None:
    """Match a user's free-text answer to one of a disambiguation's options.

    Used on the turn after we asked a clarifying question ("toward Maverick" ->
    the Maverick option). LLM-free: exact/substring first, then a confident fuzzy
    match. Returns None when nothing matches well enough.
    """
    if not pending.options or not answer or not answer.strip():
        return None
    norm = normalize(answer)
    labels = [normalize(opt.label) for opt in pending.options]

    for i, label in enumerate(labels):
        if label and (label == norm or label in norm or norm in label):
            return pending.options[i]

    best = process.extractOne(norm, labels, scorer=fuzz.WRatio, score_cutoff=OPTION_MATCH_CUTOFF)
    if best is None:
        return None
    return pending.options[best[2]]
