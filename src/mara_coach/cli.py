"""`coach` entry point: the core CLI plus coach commands. The core never imports this package."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from mara_trainer.cli import _conn, _fail, _out, app

from . import evals, evals_runner, sources

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
