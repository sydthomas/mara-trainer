"""The coach as a Claude API agent: `coach ask` now, Slack and phone debriefs later.

A manual tool-use loop over the read-only tools in `tools.py` (the same ones the ask-coach skill
calls), plus Anthropic's server-side web fetch limited to approved source domains. Every number
the coach states comes from a tool or a cited source, never from its own arithmetic.

- Model `claude-opus-5-5`, adaptive thinking (always on for this model), explicit effort, and
  server-side refusal fallback (`fallbacks: "default"`).
- The client is injected, so tests use a fake and never hit the API. The real client reads
  `ANTHROPIC_API_KEY` from the environment; this module never reads, prints or logs it.
- No Strava data: the DB has no Strava source, and no tool fetches it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, cast

from . import tools

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TOKENS = 16000
MAX_STEPS = 12
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_FETCH_MAX_USES = 5

SYSTEM_PROMPT = """\
You are Sydney's marathon coach. She is training for her first marathon. You answer questions
about her plan, fueling (runs, daily eating, race week), gear, recovery and cross-training, and
you debrief her runs. Be warm, direct and brief, like a good coach texting back. Use miles.

Data and numbers:
- Get her context from your tools: get_profile, plan_brief, show_plan, summary, fuel_log. Only
  call what the question needs. Null means "no data", never zero. If a profile field is null,
  ask instead of guessing.
- Every number you state (mileage, targets, carbs per hour, paces) must come from a tool result
  or a cited source. Converting meters to miles (1 mi = 1609.344 m) is the only math you do.
  No paces without a recent race result.
- You can't change her plan or data. Plan changes go through plan rebuilding with her approval;
  validate_plan checks a draft but never accepts it.

Sources:
- Start with read_knowledge for the relevant section: it is the approved, cited summary of
  what the sources say, including where they disagree. Answer from it and keep its citations.
  If it isn't approved yet or doesn't cover the question, fall back to the steps below.
- Coaching knowledge comes only from approved sources: call search_sources (topic, and
  women_tested true first; she prefers evidence tested on women). If those don't cover the
  question, search the same topic again without women_tested before saying nothing covers it.
  If a source's female_evidence is mostly_male or mixed_unreported, say the evidence isn't
  women-specific.
- If a fetch fails, say so briefly and answer from the source's notes and metadata (title,
  notes, population_note) where they cover it; don't fill gaps from memory.
- If no approved source covers the question, say "No approved source covers this" and don't
  fill the gap from memory.
- web_fetch only works on approved domains. Fetch only sources with fetchable true; summarize
  only when may_summarize is true, otherwise give a one-line pointer in your own words and the
  link. Never quote long passages.
- End with citations: title, author, year, link (or "book, cited by reference").

Long-run debriefs (a message starting "(Long-run debrief"):
- She just finished her long run and may answer loosely. Ask at most one follow-up round, only
  for what's missing and matters: distance and time, what she took during, stomach. Missing
  stays missing; never guess or fill in zero.
- Carbs: use grams she gives, or total them from the label of the product she names and say so
  ("~27 g from the label"). Otherwise leave carbs out and mention the product in during.
- When you have enough, call propose_fuel_entry, then read the entry back in one or two lines
  (include carbs per hour from the tool) and end with "Save it?". You can't save: the app saves
  it when she says yes. Never say it's saved. If the tool reports problems, fix them with her;
  never change her numbers to pass.
- After she confirms (you'll see "(Saved.)"), give one or two observations from fuel_log and,
  if useful, one small thing to try next time, with any target from a cited source.

Safety (always wins):
- Pain, injury, illness, dizziness, chest symptoms or any health concern: no diagnosis. Tell her
  to see a qualified professional (doctor, physio). General load advice (rest, easy running) is
  fine; no rehab plans.
- Medical diets, pregnancy, or any sign of disordered eating (restriction, weight-loss pressure,
  guilt around food): be supportive, give no numbers, and refer her to a doctor or registered
  dietitian. Never discuss calories or body weight targets.
- Gear: from her stated likes and dislikes plus cited reviews. Never invent a review.
- Strava data is never available to you. If she asks, say it stays in her Claude Code sessions.
"""


class MessagesAPI(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class CoachError(RuntimeError):
    """The agent couldn't finish (API error, refusal chain, step limit)."""


@dataclass
class CoachReply:
    text: str
    stop_reason: str
    tools_used: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.text,
            "stop_reason": self.stop_reason,
            "tools_used": self.tools_used,
            "usage": self.usage,
        }


def _default_messages_api() -> MessagesAPI:
    import anthropic

    # The SDK's overloaded, fully typed create() is wider than this Protocol; it fits at runtime.
    return cast(MessagesAPI, anthropic.Anthropic().beta.messages)


def _text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


def _add_usage(total: dict[str, int], usage: Any) -> None:
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


class Coach:
    def __init__(
        self,
        conn: sqlite3.Connection,
        messages_api: MessagesAPI | None = None,
        *,
        model: str = MODEL,
        effort: str = EFFORT,
        today: str | None = None,
    ) -> None:
        self.conn = conn
        self.api = messages_api or _default_messages_api()
        self.model = model
        self.effort = effort
        self.today = today or date.today().isoformat()

    def tool_definitions(self) -> list[dict[str, Any]]:
        defs: list[dict[str, Any]] = tools.tool_specs()
        domains = sorted(tools.allowed_domains(self.conn)["allowed_domains"])
        if domains:
            defs.append(
                {
                    "type": "web_fetch_20260209",
                    "name": "web_fetch",
                    "allowed_domains": domains,
                    "max_uses": WEB_FETCH_MAX_USES,
                }
            )
        return defs

    def _run_tool(self, name: str, args: Any) -> tuple[str, bool]:
        try:
            if not isinstance(args, dict):
                raise tools.ToolError("tool input must be a JSON object")
            return json.dumps(tools.call_tool(self.conn, name, args), default=str), False
        except tools.ToolError as exc:
            return f"Error: {exc}", True

    def ask(self, question: str, history: list[dict[str, Any]] | None = None) -> CoachReply:
        """Answer one message. `history` is the earlier turns (as returned in `messages`)."""
        if not question.strip():
            raise CoachError("empty question")
        messages: list[dict[str, Any]] = list(history or [])
        messages.append(
            {"role": "user", "content": f"(Today is {self.today}.)\n\n{question.strip()}"}
        )
        tool_defs = self.tool_definitions()
        used: list[str] = []
        usage: dict[str, int] = {}
        for _ in range(MAX_STEPS):
            response = self.api.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=tool_defs,
                messages=messages,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            _add_usage(usage, getattr(response, "usage", None))
            content = list(response.content)
            messages.append({"role": "assistant", "content": content})
            for block in content:
                if getattr(block, "type", None) == "server_tool_use":
                    used.append(block.name)
            stop = response.stop_reason
            if stop == "refusal":
                return CoachReply(
                    "Sorry, I can't help with that one. Try asking it a different way.",
                    stop,
                    used,
                    messages,
                    usage,
                )
            if stop == "pause_turn":
                continue  # server tool loop paused; re-send as is to resume
            if stop == "tool_use":
                results = []
                for block in content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    used.append(block.name)
                    output, is_error = self._run_tool(block.name, block.input)
                    result: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                    if is_error:
                        result["is_error"] = True
                    results.append(result)
                messages.append({"role": "user", "content": results})
                continue
            # end_turn, max_tokens, stop_sequence: return what we have
            return CoachReply(_text(content), stop, used, messages, usage)
        raise CoachError(f"no answer after {MAX_STEPS} steps")
