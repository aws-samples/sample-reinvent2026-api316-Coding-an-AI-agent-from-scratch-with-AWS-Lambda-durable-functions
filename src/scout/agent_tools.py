"""Tools for the single Expo Booth Scout agent.

The agent is one tool-use loop (see ``handler.py``). Most tools are pure data
operations executed as a plain ``context.step`` (profile, search, details,
plan_route, finalize_guide). Two are *durable*: ``request_approval`` runs as a
``wait_for_callback`` and ``reserve_swags`` as a ``wait_for_condition``. Both are
implemented in the handler because they need the durable context, though their
Converse tool specs live here alongside the rest.

Pure-data tools return JSON-serializable dicts. The model calls them by name;
``run_data_tool`` dispatches the data ones.
"""

from __future__ import annotations

from typing import Any

from . import catalog


# Tool name constants (used by the handler to route + terminate the loop).
PROFILE = "profile_interests"
SEARCH = "search_catalog"
DETAILS = "get_booth_details"
PLAN_ROUTE = "plan_route"
REQUEST_APPROVAL = "request_approval"
RESERVE_SWAGS = "reserve_swags"
FINALIZE = "finalize_guide"


# ---------------------------------------------------------------------------
# Pure data-tool implementations
# ---------------------------------------------------------------------------
def profile_interests(description: str) -> dict[str, Any]:
    """Return the raw description echoed back for the model to structure.

    Profiling is really the model's job; this tool just confirms the input the
    agent is working from (and is a natural first tool call to show).
    """
    return {"description": description}


# Common phrasings the tag vocabulary abbreviates. Purely lexical fixes have no
# way to connect "machine learning" to the tag "ml", so map the handful that come
# up rather than sending the agent back round the loop to rephrase.
_ALIASES = {
    "machine learning": "ml",
    "artificial intelligence": "ai",
    "generative ai": "genai",
    "gen ai": "genai",
    "llm": "genai",
    "llms": "genai",
    "ci/cd": "ci-cd",
    "data warehouse": "warehouse",
    "infrastructure as code": "deployment",
    "cost management": "cost-optimization",
}


def _tag_variants(interest: str) -> set[str]:
    """Forms of one interest to try against the tag vocabulary.

    Tags are hyphenated single tokens (``vector-database``), but a model naturally
    writes ``vector database``. Matching both spellings — plus a few known aliases —
    avoids an empty result that would otherwise send the agent round the loop again
    to rephrase.
    """
    raw = interest.lower().strip()
    if not raw:
        return set()
    spaced = raw.replace("_", " ").replace("-", " ")
    collapsed = "-".join(spaced.split())
    variants = {raw, collapsed}
    for form in (raw, " ".join(spaced.split())):
        if form in _ALIASES:
            variants.add(_ALIASES[form])
    return variants


def unknown_alias_targets() -> list[str]:
    """Alias targets that are not real tags.

    An alias pointing at a tag that does not exist silently matches nothing, which
    is the exact failure this table exists to prevent. Kept as a function so a test
    can assert it stays empty as the catalog changes.
    """
    vocabulary = set(catalog.all_tags())
    return sorted({v for v in _ALIASES.values() if v not in vocabulary})


def _near_misses(wanted: set[str], tags: tuple[str, ...]) -> list[str]:
    """Tags that partially overlap what was asked for, to guide a retry."""
    out = set()
    for term in wanted:
        if len(term) < 3:
            continue  # too short to be a meaningful substring
        for tag in tags:
            if term in tag or tag in term:
                out.add(tag)
    return sorted(out)


def search_catalog(interests: list[str]) -> dict[str, Any]:
    """Find catalog booths whose tags overlap the given interests.

    Matching is an exact set intersection over a fixed vocabulary, so an unknown
    term silently matches nothing. When that happens the result carries the full
    vocabulary and any near misses, letting the agent correct itself in one turn
    instead of blind-retrying different phrasings.
    """
    wanted: set[str] = set()
    for interest in interests:
        wanted |= _tag_variants(interest)

    hits = []
    for booth in catalog.booths():
        tags = {t.lower() for t in booth["tags"]}
        overlap = sorted(wanted & tags)
        if overlap:
            hits.append(
                {
                    "booth_number": booth["booth_number"],
                    "name": booth["name"],
                    "matched_tags": overlap,
                    "tags": booth["tags"],
                    "pitch": booth["pitch"],
                    "location": booth["location"],
                    "swag": booth["swag"],
                }
            )

    if hits:
        return {"results": hits, "count": len(hits)}

    vocabulary = catalog.all_tags()
    near = _near_misses(wanted, vocabulary)
    return {
        "results": [],
        "count": 0,
        "searched_for": sorted(wanted),
        "did_you_mean": near,
        "available_tags": list(vocabulary),
        "hint": (
            "No tag matched. Tags must be used verbatim from available_tags — they "
            "are hyphenated single tokens, so 'ai' and 'agents' work where "
            "'AI agents' does not. Retry with tags from that list."
        ),
    }


def get_booth_details(booth_number: str) -> dict[str, Any]:
    """Return the full catalog record for one booth."""
    booth = catalog.booth_by_number(booth_number)
    if booth is None:
        return {"error": f"no booth {booth_number}"}
    return booth


def plan_route(stops: list[dict[str, Any]]) -> dict[str, Any]:
    """Order chosen booths into an efficient walking route.

    ``stops`` is a list of {booth_number, name, location:{x,y}, swag?}.
    Nearest-neighbor from the hall entrance; deterministic (ties by booth
    number) so replay is stable.
    """
    entrance = catalog.load_catalog()["floor_grid"]["entrance"]

    def dist(a: dict[str, Any], b: dict[str, Any]) -> float:
        return float(
            abs(a.get("x", 0) - b.get("x", 0)) + abs(a.get("y", 0) - b.get("y", 0))
        )

    remaining = list(stops)
    current = dict(entrance)
    ordered = []
    order = 1
    while remaining:
        remaining.sort(
            key=lambda s: (dist(current, s.get("location", {})), s.get("booth_number", ""))
        )
        nxt = remaining.pop(0)
        leg = dist(current, nxt.get("location", {}))
        ordered.append(
            {
                "order": order,
                "booth_number": nxt.get("booth_number"),
                "name": nxt.get("name"),
                "location": nxt.get("location", {}),
                "swag": nxt.get("swag", ""),
                "leg_distance": leg,
            }
        )
        current = nxt.get("location", {})
        order += 1
    return {"route": ordered}


_DATA_IMPLS = {
    PROFILE: lambda a: profile_interests(a.get("description", "")),
    SEARCH: lambda a: search_catalog(a.get("interests", [])),
    DETAILS: lambda a: get_booth_details(a.get("booth_number", "")),
    PLAN_ROUTE: lambda a: plan_route(a.get("stops", [])),
}


def run_data_tool(name: str, args: dict[str, Any], logger=None) -> dict[str, Any]:
    """Execute a pure data tool by name.

    Pass the step's logger to get an ``[agent] tool <name> -> ok`` line tagged
    with the step's name in the logs.
    """
    impl = _DATA_IMPLS.get(name)
    if impl is None:
        return {"error": f"not a data tool: {name}"}
    result = impl(args)
    if logger is not None:
        logger.info(f"[agent] tool {name} -> ok")
    return result


# ---------------------------------------------------------------------------
# Converse tool specs advertised to the model
# ---------------------------------------------------------------------------
def _spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "toolSpec": {
            "name": name,
            "description": description,
            "inputSchema": {
                "json": {"type": "object", "properties": properties, "required": required}
            },
        }
    }


def tool_config() -> dict[str, Any]:
    """The full toolConfig for the single agent."""
    return {
        "tools": [
            _spec(
                PROFILE,
                "Record and confirm the attendee's raw description before profiling their interests.",
                {"description": {"type": "string"}},
                ["description"],
            ),
            _spec(
                SEARCH,
                # The vocabulary is published here on purpose. Matching is an exact
                # set intersection, so without it the model guesses phrasings, gets
                # empty results, and burns extra turns rephrasing.
                "Find expo booths whose topic tags overlap a list of interests. "
                "Interests must be chosen verbatim from this tag vocabulary "
                "(hyphenated single tokens — use 'ai' and 'agents', not 'AI agents'): "
                + ", ".join(catalog.all_tags()),
                {
                    "interests": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(catalog.all_tags())},
                        "description": "Tags from the vocabulary listed in this tool's description.",
                    }
                },
                ["interests"],
            ),
            _spec(
                DETAILS,
                "Get the full record (tags, pitch, swag, location) for one booth.",
                {"booth_number": {"type": "string"}},
                ["booth_number"],
            ),
            _spec(
                PLAN_ROUTE,
                "Order chosen booths into an efficient walking route from the entrance. "
                "Provide stops as objects with booth_number, name, location {x,y}, and swag.",
                {
                    "stops": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "booth_number": {"type": "string"},
                                "name": {"type": "string"},
                                "location": {"type": "object"},
                                "swag": {"type": "string"},
                            },
                            "required": ["booth_number", "location"],
                        },
                    }
                },
                ["stops"],
            ),
            _spec(
                REQUEST_APPROVAL,
                "Show the attendee the proposed hit list and wait for their approve/reject "
                "decision. Call this once you have a routed hit list.",
                {
                    "hit_list": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "order": {"type": "number"},
                                "booth_number": {"type": "string"},
                                "name": {"type": "string"},
                                "swag": {"type": "string"},
                                "talking_points": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["order", "booth_number", "name"],
                        },
                    }
                },
                ["hit_list"],
            ),
            _spec(
                RESERVE_SWAGS,
                "Reserve the swag item each approved booth offers and wait until the "
                "exhibitors confirm. Returns the confirmed swag reservations.",
                {
                    "stops": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "order": {"type": "number"},
                                "booth_number": {"type": "string"},
                                "name": {"type": "string"},
                                "swag": {"type": "string"},
                                "talking_points": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["order", "booth_number", "name"],
                        },
                    }
                },
                ["stops"],
            ),
            _spec(
                FINALIZE,
                "Return the final personalized expo guide. Call this once the swag is "
                "reserved (or the attendee rejected the hit list).",
                {
                    "status": {
                        "type": "string",
                        "description": "CONFIRMED if the attendee approved, REJECTED if not",
                    },
                    "guide": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "order": {"type": "number"},
                                "booth_number": {"type": "string"},
                                "name": {"type": "string"},
                                "swag": {"type": "string"},
                                "talking_points": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["order", "booth_number", "name"],
                        },
                    },
                    "message": {"type": "string"},
                },
                ["status"],
            ),
        ],
        "toolChoice": {"auto": {}},
    }
