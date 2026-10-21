"""One pass over a mail source: classify, report what a person sent, remember what was seen."""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ops_suite.mail.classify import MailRules, classify
from ops_suite.mail.report import ReportWriter
from ops_suite.mail.source import MailSource
from ops_suite.mail.state import SeenState

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScanResult:
    scanned: int  # messages fetched (parsed successfully)
    flagged: int
    skipped: dict[str, int]  # reason -> count, for unflagged messages
    duplicates: int  # already in the seen-state
    rejected: int
    report_path: Path | None  # None on a dry run
    # Dry runs only: (received time, message id) of each message that would be reported.
    dry_run_flagged: tuple[tuple[datetime, str], ...] = ()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_scan(
    source: MailSource,
    rules: MailRules,
    state: SeenState,
    report: ReportWriter,
    *,
    dry_run: bool = False,
    now: Callable[[], datetime] = _utc_now,
) -> ScanResult:
    """Scan every message the source offers.

    A dry run classifies and counts only: no report rows, no state changes, no acknowledge,
    no purge.
    """
    scanned = flagged = duplicates = 0
    skipped: Counter[str] = Counter()
    dry_run_flagged: list[tuple[datetime, str]] = []
    seen_this_run: set[str] = set()

    try:
        for message in source.fetch():
            scanned += 1
            if state.has(message.id) or message.id in seen_this_run:
                duplicates += 1
                log.debug("duplicate %s in %s", message.id, message.source_ref)
                if not dry_run:
                    source.acknowledge(message)
                continue
            seen_this_run.add(message.id)

            verdict = classify(message, rules)
            log.debug("%s in %s: %s", message.id, message.source_ref, verdict.reason)
            if verdict.flagged:
                flagged += 1
            else:
                skipped[verdict.reason] += 1

            if dry_run:
                if verdict.flagged:
                    dry_run_flagged.append((message.received_at, message.id))
                continue
            if verdict.flagged:
                report.append(message, now())
            state.add(message.id, now())
            source.acknowledge(message)
    finally:
        # Also after a failure, so messages already reported are not reported again.
        if not dry_run:
            state.save()

    if not dry_run:
        purge = getattr(source, "purge_expired", None)
        if callable(purge):
            purge()

    result = ScanResult(
        scanned=scanned,
        flagged=flagged,
        skipped=dict(skipped),
        duplicates=duplicates,
        rejected=source.rejected,
        report_path=None if dry_run else report.path,
        dry_run_flagged=tuple(dry_run_flagged),
    )
    log.info(
        "scan finished: scanned %d, flagged %d, skipped %d, duplicates %d, rejected %d",
        result.scanned,
        result.flagged,
        sum(result.skipped.values()),
        result.duplicates,
        result.rejected,
        extra={
            "scanned": result.scanned,
            "flagged": result.flagged,
            "skipped": result.skipped,
            "duplicates": result.duplicates,
            "rejected": result.rejected,
            "dry_run": dry_run,
        },
    )
    return result
