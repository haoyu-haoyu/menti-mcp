#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2",
#   "httpx>=0.27",
# ]
# ///
"""MCP server for Mentimeter Quiz Competition.

Lets an AI agent join a Menti quiz, poll the presenter state,
and submit answers — built for AI-vs-AI competition.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE = "https://www.menti.com/core"
ABLY_REST = "https://realtime.ably.menti.com"
# Public read-only Ably key surfaced by menti.com's own web client.
ABLY_PUBLIC_KEY = "TTRRVlBRLjlIVVNPQTpFa3lZOWVkcXBqQWNjNzBrVzJ5RVJHUTRCS3FOa3lIb0UxQzJ0OXBnaHdj"

mcp = FastMCP("menti")


@dataclass
class Session:
    identifier: str | None = None
    jwt: str | None = None
    vote_key: str | None = None
    code: str | None = None
    player: dict | None = None
    deck: dict | None = None
    question_index: dict[str, dict] = field(default_factory=dict)


SESSION = Session()


def _build_question_index(deck: dict) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for slide in deck["slide_deck"]["slides"]:
        for ic in slide.get("interactive_contents", []):
            idx[ic["interactive_content_id"]] = {
                "slide_public_key": slide["slide_public_key"],
                "title": slide.get("title"),
                "type": slide["static_content"]["type"],
                "countdown": ic.get("countdown"),
                "policy": ic.get("response_policy"),
                "choices": [
                    {"id": c["interactive_content_choice_id"], "title": c["title"]}
                    for c in ic.get("choices", [])
                ],
            }
    return idx


def _find_slide(deck: dict | None, slide_public_key: str) -> dict | None:
    if not deck:
        return None
    for s in deck["slide_deck"]["slides"]:
        if s["slide_public_key"] == slide_public_key:
            return s
    return None


@dataclass
class StateFetch:
    """Result of fetching the latest presenter state.

    `state` is None both for "no messages yet" (ok=True) and for failures
    (ok=False, with `error` set). Callers can distinguish via `ok`.
    """
    state: dict | None
    ok: bool
    error: str | None = None


async def _fetch_state(vote_key: str) -> StateFetch:
    channel = f"series_public:{vote_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{ABLY_REST}/channels/{channel}/messages",
                params={"limit": 1, "envelope": "json"},
                headers={
                    "authorization": f"Basic {ABLY_PUBLIC_KEY}",
                    "x-ably-version": "6",
                },
            )
    except httpx.HTTPError as e:
        return StateFetch(None, False, f"network: {e!r}")
    if r.status_code >= 400:
        return StateFetch(None, False, f"http {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as e:
        return StateFetch(None, False, f"invalid json: {e!r}")
    if not isinstance(data, dict):
        return StateFetch(None, False, f"unexpected json shape: {type(data).__name__}")
    raw_msgs = data.get("response", [])
    if not isinstance(raw_msgs, list):
        return StateFetch(None, False, "unexpected response field type")
    if not raw_msgs:
        return StateFetch(None, True)
    msg = raw_msgs[0]
    if not isinstance(msg, dict):
        return StateFetch(None, False, "unexpected message entry type")
    raw = msg.get("data")
    timestamp = msg.get("timestamp")
    if raw is None:
        return StateFetch({"_timestamp": timestamp}, True)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        parsed = None
    state = parsed if isinstance(parsed, dict) else {"raw": raw}
    state["_timestamp"] = timestamp
    return StateFetch(state, True)


def _decorate_state(state: dict, deck: dict | None) -> dict:
    slide_key = state.get("slidePublicKey")
    slide = _find_slide(deck, slide_key) if slide_key else None
    slide_info = None
    if slide:
        slide_info = {
            "title": slide.get("title"),
            "type": slide["static_content"]["type"],
            "interactive_contents": [
                {
                    "id": ic["interactive_content_id"],
                    "choices": [
                        {
                            "id": c["interactive_content_choice_id"],
                            "title": c["title"],
                        }
                        for c in ic.get("choices", [])
                    ],
                }
                for ic in slide.get("interactive_contents", [])
            ],
        }
    slide_state = (state.get("slideStates") or {}).get(slide_key or "", {})
    is_live = (
        state.get("currentStep") == "quiz"
        and bool(slide_state.get("started"))
    )
    return {
        "timestamp": state.get("_timestamp"),
        "slide_public_key": slide_key,
        "current_step": state.get("currentStep"),
        "next_step": state.get("nextStep"),
        "slide_index": state.get("slideIndex"),
        "total_slides": state.get("totalSlides"),
        "is_in_lobby": state.get("isInQuizLobby"),
        "is_live_question": is_live,
        "slide": slide_info,
    }


@mcp.tool()
async def join_quiz(code: str, name: str) -> dict:
    """Join a Mentimeter quiz as a player.

    Runs the full join sequence: mint identifier, resolve vote key from join
    code, fetch the deck, open session, register player, set player name.

    Args:
        code: 7 or 8 digit Menti join code (spaces are stripped).
        name: Player display name shown on the leaderboard.
    """
    code = code.replace(" ", "")
    local = Session(code=code)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{BASE}/identifiers",
                headers={"accept": "application/json", "content-type": "application/json"},
                json={},
            )
            r.raise_for_status()
            data = r.json()
        local.identifier = data["identifier"]
        local.jwt = data.get("jwt")

        headers = {"accept": "application/json", "x-identifier": local.identifier}
        async with httpx.AsyncClient(base_url=BASE, headers=headers, timeout=10.0) as c:
            r = await c.get(f"/audience/slide-deck/{code}/participation-key")
            r.raise_for_status()
            local.vote_key = r.json()["participation_key"]

            r = await c.get(
                f"/audience/slide-deck/{local.vote_key}",
                params={"source": "voteCode"},
            )
            r.raise_for_status()
            local.deck = r.json()
            local.question_index = _build_question_index(local.deck)

            r = await c.post(
                f"/audience/connect/{local.vote_key}",
                json={"is_desktop_experience": False},
            )
            r.raise_for_status()

            r = await c.post(
                f"/audience/quiz/{local.vote_key}/players",
                params={"tries": 1},
            )
            r.raise_for_status()
            local.player = r.json()

            r = await c.patch(
                f"/audience/quiz/{local.vote_key}/players",
                json={"name": name},
            )
            r.raise_for_status()
            local.player = r.json()
    except httpx.HTTPStatusError as e:
        return {
            "ok": False,
            "error": f"http {e.response.status_code}: {e.response.text[:300]}",
            "step_url": str(e.request.url),
        }
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"network: {e!r}"}

    # Swap atomically only after every step succeeded.
    SESSION.identifier = local.identifier
    SESSION.jwt = local.jwt
    SESSION.code = local.code
    SESSION.vote_key = local.vote_key
    SESSION.deck = local.deck
    SESSION.question_index = local.question_index
    SESSION.player = local.player

    slides = local.deck["slide_deck"]["slides"]
    return {
        "ok": True,
        "vote_key": local.vote_key,
        "quiz_name": local.deck["slide_deck"]["name"],
        "total_slides": len(slides),
        "total_questions": sum(1 for s in slides if s.get("interactive_contents")),
        "player": local.player,
    }


@mcp.tool()
async def get_deck() -> dict:
    """Return all slides in the current deck (questions, types, choices).

    Useful for pre-computing answers before the quiz starts, or inspecting
    the structure. Call after join_quiz.
    """
    deck = SESSION.deck
    vote_key = SESSION.vote_key
    if not deck:
        return {"ok": False, "error": "Not joined. Call join_quiz first."}
    return {
        "ok": True,
        "name": deck["slide_deck"]["name"],
        "vote_key": vote_key,
        "slides": [
            {
                "key": s["slide_public_key"],
                "title": s["title"],
                "type": s["static_content"]["type"],
                "interactive_contents": [
                    {
                        "id": ic["interactive_content_id"],
                        "countdown": ic.get("countdown"),
                        "policy": ic.get("response_policy"),
                        "choices": [
                            {
                                "id": c["interactive_content_choice_id"],
                                "title": c["title"],
                            }
                            for c in ic.get("choices", [])
                        ],
                    }
                    for ic in s.get("interactive_contents", [])
                ],
            }
            for s in deck["slide_deck"]["slides"]
        ],
    }


@mcp.tool()
async def get_current_state() -> dict:
    """Fetch the latest presenter state (which slide is live, current step).

    Returns fields including `current_step` (quiz-lobby / quiz / leaderboard /
    static-content), `is_live_question` (True iff the presenter has started
    a question that accepts responses), and `slide` (the current slide's
    title and choices, if any). On transport failure returns `{"error": ...}`.
    """
    vote_key = SESSION.vote_key
    deck = SESSION.deck
    if not vote_key:
        return {"ok": False, "error": "Not joined."}
    fetch = await _fetch_state(vote_key)
    if not fetch.ok:
        return {"ok": False, "error": fetch.error}
    if fetch.state is None:
        return {"ok": True, "status": "no_state"}
    return {"ok": True, **_decorate_state(fetch.state, deck)}


@mcp.tool()
async def wait_for_live_question(
    timeout_s: float = 120.0,
    poll_interval_s: float = 1.5,
    since_slide_key: str | None = None,
) -> dict:
    """Block until a question is live, then return it.

    If `since_slide_key` is given, waits until a question with a DIFFERENT
    slide_public_key becomes live (use this to wait for the NEXT question
    after you've already answered the current one). Transient poll failures
    are swallowed (polling continues); only a final timeout returns
    `{timeout: true}`.

    Args:
        timeout_s: How long to wait before returning {timeout: true}.
        poll_interval_s: Polling cadence against Ably REST.
        since_slide_key: Skip questions with this slide_public_key.
    """
    vote_key = SESSION.vote_key
    deck = SESSION.deck
    if not vote_key:
        return {"ok": False, "error": "Not joined."}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        fetch = await _fetch_state(vote_key)
        if fetch.ok and fetch.state is not None:
            decorated = _decorate_state(fetch.state, deck)
            if (
                decorated["is_live_question"]
                and decorated["slide_public_key"] != since_slide_key
            ):
                return {"ok": True, **decorated}
        await asyncio.sleep(poll_interval_s)
    return {
        "ok": False,
        "timeout": True,
        "error": f"No live question within {timeout_s}s",
    }


async def _submit_answer(
    vote_key: str,
    identifier: str,
    interactive_content_id: str,
    question: dict,
    choice: str | list[str],
) -> dict:
    """Core answer-submission logic. Takes a fully-snapshotted argument set
    so callers never re-read from SESSION mid-flight (avoids cross-call races).
    """
    resolved = _resolve_choice_list(question, choice)
    if not resolved:
        return {
            "ok": False,
            "error": f"No choice matches {choice!r}",
            "available_choices": [c["title"] for c in question["choices"]],
        }
    payload = {
        "response": {
            "type": "quiz-choice",
            "choices": [{"interactive_content_choice_id": m["id"]} for m in resolved],
        }
    }
    headers = {"accept": "application/json", "x-identifier": identifier}
    try:
        async with httpx.AsyncClient(base_url=BASE, headers=headers, timeout=10.0) as c:
            r = await c.post(
                f"/audience/{vote_key}/responses/{interactive_content_id}",
                json=payload,
            )
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"network: {e!r}"}
    ok = r.status_code in (200, 201)
    result = {
        "ok": ok,
        "status": r.status_code,
        "question": question["title"],
        "submitted": [{"id": m["id"], "title": m["title"]} for m in resolved],
        "response_body": r.text,
    }
    if not ok:
        result["error"] = f"http {r.status_code}: {r.text[:200]}"
    return result


@mcp.tool()
async def submit_answer(
    interactive_content_id: str,
    choice: str | list[str],
) -> dict:
    """Submit an answer to a quiz-choice question.

    Args:
        interactive_content_id: The question's `interactive_content_id`
            (from get_deck or wait_for_live_question).
        choice: Either a choice UUID, a visible choice text ("56"), or a
            list of them for multi-select. A string containing a comma is
            tried as a single label first, then as a comma-separated list —
            so "Paris, France" matches a single label if one exists.
    """
    vote_key = SESSION.vote_key
    identifier = SESSION.identifier
    question = SESSION.question_index.get(interactive_content_id)
    if not vote_key or not identifier:
        return {"ok": False, "error": "Not joined."}
    if not question:
        return {
            "ok": False,
            "error": f"Unknown interactive_content_id: {interactive_content_id}",
        }
    return await _submit_answer(
        vote_key, identifier, interactive_content_id, question, choice
    )


def _resolve_choice(question: dict, raw: str) -> dict | None:
    for c in question["choices"]:
        if c["id"] == raw:
            return c
    low = raw.strip().lower()
    for c in question["choices"]:
        if c["title"].strip().lower() == low:
            return c
    return None


def _resolve_choice_list(question: dict, choice: str | list[str]) -> list[dict]:
    """Resolve `choice` (UUID / label / comma-string / list) to concrete choices.

    A list argument is unambiguous — each item must resolve. A string is
    tried whole first (so "Paris, France" stays intact if it's a real label),
    and only comma-split as a fallback. Returns [] if any piece is unresolved.
    """
    if isinstance(choice, list):
        resolved: list[dict] = []
        for item in choice:
            match = _resolve_choice(question, item)
            if match is None:
                return []
            resolved.append(match)
        return resolved
    whole = _resolve_choice(question, choice)
    if whole is not None:
        return [whole]
    if "," not in choice:
        return []
    pieces = [p.strip() for p in choice.split(",") if p.strip()]
    fallback: list[dict] = []
    for p in pieces:
        match = _resolve_choice(question, p)
        if match is None:
            return []
        fallback.append(match)
    return fallback


@mcp.tool()
async def answer_current_question(choice: str | list[str]) -> dict:
    """Submit an answer to whatever question is currently live on the presenter.

    Convenience wrapper: finds the live question via Ably state, resolves
    `choice` against its choices, then submits. Returns an error if no
    question is currently live. Snapshots all session state up front so
    concurrent tool calls don't cross-contaminate.
    """
    vote_key = SESSION.vote_key
    identifier = SESSION.identifier
    deck = SESSION.deck
    question_index = SESSION.question_index
    if not vote_key or not identifier:
        return {"ok": False, "error": "Not joined."}
    fetch = await _fetch_state(vote_key)
    if not fetch.ok:
        return {"ok": False, "error": fetch.error}
    if fetch.state is None:
        return {"ok": False, "error": "No presenter state yet."}
    decorated = _decorate_state(fetch.state, deck)
    if not decorated["is_live_question"]:
        return {
            "ok": False,
            "error": "No live question right now.",
            "current_step": decorated["current_step"],
            "is_in_lobby": decorated["is_in_lobby"],
        }
    slide = decorated.get("slide")
    if not slide:
        return {
            "ok": False,
            "error": "Live slide missing from local deck cache — rejoin to refresh.",
            "slide_public_key": decorated["slide_public_key"],
        }
    ics = slide["interactive_contents"]
    if not ics:
        return {"ok": False, "error": "Live slide has no interactive content."}
    ic_id = ics[0]["id"]
    question = question_index.get(ic_id)
    if not question:
        return {
            "ok": False,
            "error": "Live question not in local deck index — rejoin to refresh.",
            "interactive_content_id": ic_id,
        }
    return await _submit_answer(vote_key, identifier, ic_id, question, choice)


@mcp.tool()
async def play_quiz(
    answer_map: dict[str, str],
    total_questions: int = 10,
    timeout_per_question_s: float = 300.0,
    poll_interval_s: float = 0.3,
    stop_on_missing_answer: bool = False,
) -> dict:
    """Autonomously play an entire quiz using a pre-computed answer map.

    For each question the tool waits until the presenter *advances to the
    next quiz slide* (detected when `slide_public_key` changes and the new
    slide has interactive content), then immediately submits the matching
    pre-chosen answer from `answer_map`. Looping all questions inside one
    MCP call removes per-question LLM-turn latency — the only overhead is
    the Ably poll cadence (default 0.3 s) plus the submit round-trip.

    Triggering on slide-change rather than on the presenter's "Start"
    button means:
      • submit fires the instant the teacher moves to the question,
      • no `started=true` flag is required — Menti's server accepts the
        response either way (empirically confirmed),
      • between-question idle periods (teacher talking) do not advance
        the loop since `slide_public_key` hasn't changed.

    Args:
        answer_map: Dict mapping each quiz slide's `slide_public_key` to a
            choice (UUID, visible label, or comma-joined list). Build this
            from `get_deck()` before calling. Missing keys cause that
            question to be skipped (or the loop to abort if
            `stop_on_missing_answer=True`).
        total_questions: Max number of questions to play. Loop stops early
            if a wait times out (presenter finished / went idle).
        timeout_per_question_s: How long to wait for each question to
            appear. Generous default (5 min) accommodates variable teacher
            pacing.
        poll_interval_s: Ably-REST poll cadence while waiting.
        stop_on_missing_answer: If True, abort the loop when an answer for
            the current slide_public_key is not in `answer_map`. If False
            (default), skip the question and wait for the next one.
    """
    vote_key = SESSION.vote_key
    identifier = SESSION.identifier
    deck = SESSION.deck
    question_index = SESSION.question_index
    if not vote_key or not identifier:
        return {"ok": False, "error": "Not joined.", "results": []}

    results: list[dict] = []
    last_slide_key: str | None = None
    submitted_ok = 0
    aborted_reason: str | None = None
    # Give up early if the poll loop can't reach Ably for this many consecutive
    # failures in a row. Keeps transient blips silent but surfaces persistent
    # outages as real errors instead of a bogus "timeout".
    MAX_CONSECUTIVE_FETCH_FAILURES = 20

    for i in range(total_questions):
        deadline = time.monotonic() + timeout_per_question_s
        live: dict | None = None
        consecutive_failures = 0
        last_fetch_error: str | None = None
        total_fetch_errors = 0  # cumulative across this question's wait window
        while time.monotonic() < deadline:
            fetch = await _fetch_state(vote_key)
            if fetch.ok:
                consecutive_failures = 0
                if fetch.state is not None:
                    decorated = _decorate_state(fetch.state, deck)
                    slide = decorated.get("slide")
                    slide_key = decorated.get("slide_public_key")
                    # Trigger the instant the presenter moves to a NEW quiz
                    # slide (i.e. the slide_public_key changes and the new
                    # slide carries at least one interactive_content). We do
                    # NOT require `is_live_question` because the server
                    # accepts submissions even before the presenter's
                    # Start-button flips `started` to true.
                    if (
                        slide_key is not None
                        and slide_key != last_slide_key
                        and slide is not None
                        and slide.get("interactive_contents")
                    ):
                        live = decorated
                        break
            else:
                consecutive_failures += 1
                total_fetch_errors += 1
                last_fetch_error = fetch.error
                if consecutive_failures >= MAX_CONSECUTIVE_FETCH_FAILURES:
                    break
            await asyncio.sleep(poll_interval_s)

        if live is None:
            if consecutive_failures >= MAX_CONSECUTIVE_FETCH_FAILURES:
                err = (
                    f"Ably polling failed {consecutive_failures} times in a row: "
                    f"{last_fetch_error}"
                )
            elif last_fetch_error is not None and consecutive_failures > 0:
                # Hit the per-question deadline while still in a failure streak —
                # tell the caller what was going wrong rather than masking it as
                # a generic timeout.
                err = (
                    f"Timeout after {timeout_per_question_s}s; "
                    f"last {consecutive_failures} Ably poll(s) failed: {last_fetch_error}"
                )
            else:
                err = f"Timeout after {timeout_per_question_s}s waiting for next question"
            entry = {"index": i + 1, "ok": False, "error": err}
            if total_fetch_errors > 0:
                entry["fetch_errors_during_wait"] = total_fetch_errors
            results.append(entry)
            aborted_reason = err
            break

        slide_key = live["slide_public_key"]
        last_slide_key = slide_key
        slide = live.get("slide")
        if slide is None:
            results.append(
                {
                    "index": i + 1,
                    "slide_public_key": slide_key,
                    "ok": False,
                    "error": "Live slide missing from local deck cache — rejoin to refresh",
                }
            )
            continue
        ics = slide.get("interactive_contents") or []
        if not ics:
            results.append(
                {
                    "index": i + 1,
                    "slide_public_key": slide_key,
                    "title": slide.get("title"),
                    "ok": False,
                    "error": "Live slide has no interactive content",
                }
            )
            continue
        ic_id = ics[0]["id"]
        question = question_index.get(ic_id)
        if not question:
            results.append(
                {
                    "index": i + 1,
                    "slide_public_key": slide_key,
                    "ok": False,
                    "error": "Live question not in local deck index — rejoin to refresh",
                    "interactive_content_id": ic_id,
                }
            )
            continue

        choice = answer_map.get(slide_key)
        if choice is None:
            entry = {
                "index": i + 1,
                "slide_public_key": slide_key,
                "title": question["title"],
                "ok": False,
                "error": f"No answer in answer_map for slide_public_key {slide_key!r}",
            }
            results.append(entry)
            if stop_on_missing_answer:
                aborted_reason = entry["error"]
                break
            continue

        submit_result = await _submit_answer(
            vote_key, identifier, ic_id, question, choice
        )
        if submit_result.get("ok"):
            submitted_ok += 1
        results.append(
            {
                "index": i + 1,
                "slide_public_key": slide_key,
                **submit_result,
            }
        )

    overall_ok = aborted_reason is None and all(r.get("ok") for r in results)
    out: dict = {
        "ok": overall_ok,
        "questions_attempted": len(results),
        "questions_submitted": submitted_ok,
        "results": results,
    }
    if aborted_reason is not None:
        out["error"] = aborted_reason
    return out


@mcp.tool()
async def get_my_player() -> dict:
    """Return the current player record (name, emoji identity, index)."""
    player = SESSION.player
    if not player:
        return {"ok": False, "error": "Not joined."}
    return {"ok": True, "player": player}


@mcp.tool()
async def get_leaderboard_events(limit: int = 20) -> dict:
    """Pull the most recent messages from the leaderboard Ably channels.

    The presenter populates these channels mid-quiz with per-player score
    events. Each channel entry includes its HTTP status so a transport error
    ("status": 503) is distinguishable from a genuinely empty channel
    ("status": 200, "messages": []).
    """
    vote_key = SESSION.vote_key
    if not vote_key:
        return {"ok": False, "error": "Not joined."}
    channels = [
        f"leaderboard:{vote_key}",
        f"series_player_public:{vote_key}",
        f"series_public:{vote_key}",
    ]
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=10.0) as c:
        for ch in channels:
            entry: dict = {"messages": []}
            try:
                r = await c.get(
                    f"{ABLY_REST}/channels/{ch}/messages",
                    params={"limit": limit, "envelope": "json"},
                    headers={
                        "authorization": f"Basic {ABLY_PUBLIC_KEY}",
                        "x-ably-version": "6",
                    },
                )
            except httpx.HTTPError as e:
                entry["status"] = 0
                entry["error"] = f"network: {e!r}"
                out[ch] = entry
                continue
            entry["status"] = r.status_code
            if r.status_code != 200:
                entry["error"] = r.text[:300]
                out[ch] = entry
                continue
            try:
                payload = r.json()
            except ValueError as e:
                entry["error"] = f"invalid json: {e!r}"
                out[ch] = entry
                continue
            if not isinstance(payload, dict):
                entry["error"] = f"unexpected json shape: {type(payload).__name__}"
                out[ch] = entry
                continue
            messages = payload.get("response", [])
            if not isinstance(messages, list):
                entry["error"] = "response field is not a list"
                out[ch] = entry
                continue
            for m in messages:
                if not isinstance(m, dict):
                    continue
                raw = m.get("data")
                if isinstance(raw, str):
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        data = raw
                else:
                    data = raw
                entry["messages"].append(
                    {
                        "name": m.get("name"),
                        "timestamp": m.get("timestamp"),
                        "data": data,
                    }
                )
            out[ch] = entry
    return {"ok": True, "channels": out}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
