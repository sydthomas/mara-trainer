"""`coach` entry point: the core CLI plus coach commands. The core never imports this package."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from mara_trainer.cli import _conn, _fail, _out, app

from . import agent, evals, evals_runner, knowledge, profile, slack_bot, sources, tools
from .env import load_env

source_app = typer.Typer(help="Source registry: vetted references the coach may cite.")
app.add_typer(source_app, name="source")
eval_app = typer.Typer(help="Eval set: score coach answers against the rubric checks.")
app.add_typer(eval_app, name="eval")


@source_app.command("add")
def source_add(
    slug: Annotated[str, typer.Option(help="lowercase-kebab id, e.g. issn-caffeine-2021")],
    title: Annotated[str, typer.Option(help="Title of the work")],
    author: Annotated[str, typer.Option(help="Organization or authors")],
    kind: Annotated[str, typer.Option(help=", ".join(sorted(sources.KINDS)))],
    topics: Annotated[str, typer.Option(help="Comma list: " + ",".join(sorted(sources.TOPICS)))],
    license_: Annotated[str, typer.Option("--license", help=", ".join(sorted(sources.LICENSES)))],
    trust_reason: Annotated[str, typer.Option(help="Why it's trusted: credential, peer review")],
    url: Annotated[str | None, typer.Option(help="https URL (omit for books)")] = None,
    year: Annotated[int | None, typer.Option(help="Publication year")] = None,
    notes: Annotated[str, typer.Option(help="Short notes in your own words")] = "",
    link_only: Annotated[
        bool, typer.Option("--link-only", help="Cite and link only; never fetch its domain")
    ] = False,
) -> None:
    """Add one source as `proposed`. It isn't cited until approved."""
    raw = {
        "slug": slug,
        "title": title,
        "author": author,
        "kind": kind,
        "url": url,
        "topics": [t.strip() for t in topics.split(",") if t.strip()],
        "license": license_,
        "trust_reason": trust_reason,
        "year": year,
        "notes": notes,
        "fetchable": not link_only,
    }
    try:
        src = sources.parse_source(raw)
        sources.add_source(_conn(), src)
    except sources.SourceError as exc:
        _fail("invalid source", problems=exc.problems)
    _out({"ok": True, "source": src.to_dict()})


@source_app.command("import")
def source_import(
    file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Seed JSON, version 1")
    ] = sources.DEFAULT_SEED,
    update: Annotated[
        bool, typer.Option("--update", help="Refresh metadata of existing slugs (keeps approval)")
    ] = False,
) -> None:
    """Add every source in a seed file as `proposed`. Existing slugs are skipped unless --update."""
    try:
        batch = sources.load_seed(file)
        result = sources.add_many(_conn(), batch, update=update)
    except sources.SourceError as exc:
        _fail("invalid seed file", problems=exc.problems)
    _out({"ok": True, **result})


@source_app.command("list")
def source_list(
    status: Annotated[str | None, typer.Option(help="proposed, approved or retired")] = None,
    topic: Annotated[str | None, typer.Option(help="Filter by topic")] = None,
    women_tested: Annotated[
        bool,
        typer.Option("--women-tested", help="Only female_specific or sex_reported evidence"),
    ] = False,
) -> None:
    """List sources (all statuses unless filtered)."""
    try:
        rows = sources.list_sources(_conn(), status=status, topic=topic, women_tested=women_tested)
    except sources.SourceError as exc:
        _fail(str(exc))
    _out({"count": len(rows), "sources": [s.to_dict() for s in rows]})


@source_app.command("approve")
def source_approve(
    slugs: Annotated[list[str], typer.Argument(help="One or more slugs")],
    by: Annotated[str, typer.Option(help="Name of the person approving")],
) -> None:
    """Approve sources so the coach may cite them. Needs a named approver."""
    try:
        result = sources.approve(_conn(), slugs, by)
    except sources.SourceError as exc:
        _fail(str(exc))
    _out({"ok": True, **result})


@source_app.command("retire")
def source_retire(slugs: Annotated[list[str], typer.Argument(help="One or more slugs")]) -> None:
    """Stop citing sources. Rows are kept so past citations stay explainable."""
    try:
        result = sources.retire(_conn(), slugs)
    except sources.SourceError as exc:
        _fail(str(exc))
    _out({"ok": True, **result})


@source_app.command("domains")
def source_domains() -> None:
    """Domains the coach may fetch from at answer time (approved, non-link-only sources)."""
    _out({"allowed_domains": sources.approved_domains(_conn())})


knowledge_app = typer.Typer(help="Knowledge base: cited topic summaries the coach reads.")
app.add_typer(knowledge_app, name="knowledge")


@knowledge_app.command("list")
def knowledge_list() -> None:
    """Every section with its state (draft, approved, changed_since_approval)."""
    try:
        sections = knowledge.load_all(tools.KNOWLEDGE_DIR)
    except knowledge.KnowledgeError as exc:
        _fail(str(exc), problems=exc.problems)
    written = {s.name for s in sections}
    _out(
        {
            "sections": [s.summary() for s in sections],
            "not_written": [n for n in knowledge.SECTIONS if n not in written],
        }
    )


@knowledge_app.command("check")
def knowledge_check(
    names: Annotated[list[str] | None, typer.Argument(help="Sections (default: all)")] = None,
) -> None:
    """Check sections against the format rules and the approved sources. Exit 3 on problems."""
    approved = tools.approved_slugs(_conn())
    report: dict[str, list[str]] = {}
    try:
        targets = (
            [knowledge.load(n, tools.KNOWLEDGE_DIR) for n in names]
            if names
            else knowledge.load_all(tools.KNOWLEDGE_DIR)
        )
    except knowledge.KnowledgeError as exc:
        _fail(str(exc), problems=exc.problems)
    for section in targets:
        report[section.name] = knowledge.check(section, approved)
    ok = not any(report.values())
    typer.echo(json.dumps({"ok": ok, "problems": report}, indent=2))
    if not ok:
        raise typer.Exit(3)


@knowledge_app.command("approve")
def knowledge_approve(
    names: Annotated[list[str], typer.Argument(help="One or more sections")],
    by: Annotated[str, typer.Option(help="Name of the person approving")],
) -> None:
    """Approve sections so the coach can read them. Each must pass `knowledge check` first."""
    approved = tools.approved_slugs(_conn())
    done = []
    for name in names:
        try:
            done.append(knowledge.approve(name, by, approved, tools.KNOWLEDGE_DIR).summary())
        except knowledge.KnowledgeError as exc:
            _fail(f"{name}: not approved", problems=exc.problems, approved=done)
    _out({"ok": True, "approved": done})


profile_app = typer.Typer(help="Runner profile: diet, allergies, shoes, injury history, notes.")
app.add_typer(profile_app, name="profile")


def _split(value: str | None) -> list[str] | None:
    """Comma list -> items. An empty string means "none" (empty list); omitted means no change."""
    return None if value is None else [v.strip() for v in value.split(",") if v.strip()]


@profile_app.command("set")
def profile_set(
    diet: Annotated[str | None, typer.Option(help="Diet in your words, e.g. vegetarian")] = None,
    allergies: Annotated[
        str | None, typer.Option(help="Comma list of allergies/foods avoided ('' = none)")
    ] = None,
    shoe_likes: Annotated[
        str | None, typer.Option(help="Comma list of shoes or traits liked")
    ] = None,
    shoe_dislikes: Annotated[
        str | None, typer.Option(help="Comma list of shoes or traits disliked")
    ] = None,
    current_shoes: Annotated[str | None, typer.Option(help="Comma list of shoes in use")] = None,
    injury_history: Annotated[str | None, typer.Option(help="Past injuries, free text")] = None,
    notes: Annotated[str | None, typer.Option(help="Anything else the coach should know")] = None,
    clear: Annotated[
        list[str] | None, typer.Option("--clear", help="Field to reset to null (repeatable)")
    ] = None,
) -> None:
    """Update only the given profile fields. Omitted fields keep their value."""
    raw: dict[str, object] = {
        k: v
        for k, v in {
            "diet": diet,
            "allergies": _split(allergies),
            "shoe_likes": _split(shoe_likes),
            "shoe_dislikes": _split(shoe_dislikes),
            "current_shoes": _split(current_shoes),
            "injury_history": injury_history,
            "notes": notes,
        }.items()
        if v is not None
    }
    both = sorted(set(raw) & set(clear or []))
    if both:
        _fail(f"can't set and clear the same field: {', '.join(both)}")
    raw.update(dict.fromkeys(clear or []))
    try:
        data = profile.update_profile(_conn(), raw)
    except profile.ProfileError as exc:
        _fail("invalid profile update", problems=exc.problems)
    _out({"ok": True, "profile": data})


@profile_app.command("show")
def profile_show() -> None:
    """Print the runner profile. Unset fields are null."""
    _out({"profile": profile.get_profile(_conn())})


tools_app = typer.Typer(help="Coach tools: read-only views of the core for the coach agent.")
app.add_typer(tools_app, name="tools")


@tools_app.command("list")
def tools_list() -> None:
    """Print every tool's name, description and JSON input schema."""
    _out({"tools": tools.tool_specs()})


@tools_app.command("call")
def tools_call(
    name: Annotated[str, typer.Argument(help="Tool name from `coach tools list`")],
    args: Annotated[str, typer.Option("--args", help="JSON object of arguments")] = "{}",
) -> None:
    """Run one tool and print its JSON result. Exit 1 on bad input or a tool error."""
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError as exc:
        _fail(f"--args is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        _fail("--args must be a JSON object")
    try:
        _out(tools.call_tool(_conn(), name, parsed))
    except tools.ToolError as exc:
        _fail(str(exc), tool=name)


@eval_app.command("run")
def eval_run(
    answers: Annotated[
        Path, typer.Option(exists=True, dir_okay=False, help="JSON object {case_id: answer_text}")
    ],
    evals_file: Annotated[
        Path, typer.Option("--evals", exists=True, dir_okay=False, help="Eval set JSON")
    ] = evals.DEFAULT_PATH,
) -> None:
    """Score saved answers offline. Exit 0 if nothing failed, 3 if any case failed."""
    try:
        cases = evals.load_eval_set(evals_file)
        answerer = evals_runner.MappingAnswerer(evals_runner.load_answers(answers))
    except evals.EvalSetError as exc:
        _fail("invalid eval input", problems=exc.problems)
    report = evals_runner.run_eval(cases, answerer)
    _out(report)
    if not report["ok"]:
        raise typer.Exit(3)


EVAL_RUNS_DIR = Path("data/eval-runs")  # gitignored: answers can include the runner's own data


@eval_app.command("live")
def eval_live(
    only: Annotated[list[str] | None, typer.Option(help="Case id (repeatable)")] = None,
    category: Annotated[str | None, typer.Option(help="Only this category")] = None,
    effort: Annotated[str, typer.Option(help="low, medium or high")] = agent.EFFORT,
    evals_file: Annotated[
        Path, typer.Option("--evals", exists=True, dir_okay=False, help="Eval set JSON")
    ] = evals.DEFAULT_PATH,
    out: Annotated[Path, typer.Option(help="Where run reports are saved")] = EVAL_RUNS_DIR,
) -> None:
    """Ask the live coach every selected case (costs API money), score, save the full report and
    compare with the previous run. Prints a summary without answers. Exit 3 if any case failed."""
    if effort not in ("low", "medium", "high"):
        _fail("--effort must be low, medium or high")
    try:
        cases = evals_runner.select_cases(evals.load_eval_set(evals_file), only, category)
    except evals.EvalSetError as exc:
        _fail("invalid eval input", problems=exc.problems)
    if not cases:
        _fail("no cases match --only/--category")
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _fail("ANTHROPIC_API_KEY is not set; add it to .env (see .env.example)")
    answerer = evals_runner.CoachAnswerer(agent.Coach(_conn(), effort=effort))
    report = evals_runner.live_report(evals_runner.run_eval(cases, answerer), answerer)
    report["run"] = {"effort": effort, "model": agent.MODEL, "cases": [c.id for c in cases]}
    path = evals_runner.save_run(report, out, datetime.now().strftime("%Y%m%d-%H%M%S"))
    previous = evals_runner.latest_run(out, before=path)
    changes = (
        evals_runner.compare_runs(json.loads(previous.read_text(encoding="utf-8")), report)
        if previous
        else None
    )
    _out(
        {
            "ok": report["ok"],
            "totals": report["totals"],
            "by_category": report["by_category"],
            "failed": [
                {"id": r["id"], "reasons": r["reasons"]}
                for r in report["results"]
                if r["status"] in ("fail", "missing")
            ],
            "usage": report["usage"],
            "errors": report["errors"],
            "saved": str(path),
            "compared_with": str(previous) if previous else None,
            "changes": changes,
        }
    )
    if not report["ok"]:
        raise typer.Exit(3)


slack_app = typer.Typer(help="Slack: chat with the coach by DM (Socket Mode).")
app.add_typer(slack_app, name="slack")


@slack_app.command("run")
def slack_run() -> None:
    """Start the Slack bot (Ctrl-C to stop). Needs SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
    SLACK_ALLOWED_USER_ID and ANTHROPIC_API_KEY in .env; see docs/slack-setup.md."""
    load_env()
    env = dict(os.environ)
    setup = slack_bot.setup_mode(env)
    missing = slack_bot.missing_env(env, slack_bot.SETUP_ENV if setup else slack_bot.REQUIRED_ENV)
    if missing:
        _fail("missing settings in .env (see docs/slack-setup.md)", missing=missing)
    _conn().close()  # create/upgrade the DB before the first message
    status = (
        "Setup mode: DM the bot to get your member ID; Ctrl-C to stop"
        if setup
        else "Slack bot running; Ctrl-C to stop"
    )
    typer.echo(json.dumps({"ok": True, "status": status}), err=True)
    slack_bot.run(dict(os.environ))


@app.command("ask")
def ask(
    question: Annotated[str, typer.Argument(help="Your question for the coach")],
    effort: Annotated[str, typer.Option(help="low, medium or high")] = agent.EFFORT,
) -> None:
    """Ask the coach (Claude API). Needs ANTHROPIC_API_KEY in .env; tools are read-only."""
    import anthropic

    if effort not in ("low", "medium", "high"):
        _fail("--effort must be low, medium or high")
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _fail("ANTHROPIC_API_KEY is not set; add it to .env (see .env.example)")
    try:
        reply = agent.Coach(_conn(), effort=effort).ask(question)
    except agent.CoachError as exc:
        _fail(str(exc))
    except anthropic.AuthenticationError:
        _fail("the API key was rejected; check ANTHROPIC_API_KEY in .env")
    except anthropic.RateLimitError:
        _fail("rate limited by the API; try again in a minute")
    except anthropic.APIStatusError as exc:
        _fail(f"API error {exc.status_code}", request_id=getattr(exc, "request_id", None))
    except anthropic.APIConnectionError:
        _fail("couldn't reach the API; check your connection")
    _out(reply.to_dict())
