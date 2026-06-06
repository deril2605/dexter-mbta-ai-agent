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

    async def resolve(
        self,
        route_token: str | None,
        location: str | None,
        direction_hint: str | None = None,
    ) -> ResolvedTarget | Disambiguation:
        # Step 1 — route.
        if not route_token or not route_token.strip():
            return Disambiguation(kind=DisambiguationKind.ROUTE)
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

        # Step 3 — direction (handled by _complete, shared with resolve_with_ids).
        return _complete(route, outcome.winner.ids, outcome.winner.name, direction_hint)

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
        route = await self._routes.get(route_id)
        if route is None:  # route cache changed under us — ask again
            return Disambiguation(kind=DisambiguationKind.ROUTE)
        return _complete(route, stop_ids, stop_name, direction_hint)

    async def _fetch_stops(self, route_id: str) -> list[Stop]:
        data = await self._client.get_json(
            "/stops",
            params={"filter[route]": route_id, "fields[stop]": _STOP_FIELDS},
            cache_ttl=self._stops_ttl,
        )
        return [Stop.from_jsonapi(r) for r in data.get("data", [])]


def _complete(
    route: Route, stop_ids: tuple[str, ...], stop_name: str, direction_hint: str | None
) -> ResolvedTarget | Disambiguation:
    """Resolve direction for a known route+stop, or ask which direction."""
    direction_id = _resolve_direction(route, direction_hint)
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
