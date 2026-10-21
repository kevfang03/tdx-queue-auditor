"""``tdx demo``: the real audit, behind the real request guard, on the synthetic app.

``demo_settings`` copies the person's settings with the TDX part pointed at the synthetic app
on 127.0.0.1 and the data folder moved to a temporary one, so the browser profile, alert
state, desk memory and verdict cache of the demo never touch the real ones. Only
``llm-usage.json`` stays real: ``DemoLlmClient`` is built from the person's own settings, so
demo tokens count against the real daily budget.

The data class is ``synthetic`` here and nowhere else; ``tdx audit`` and ``tdx watch`` go
through ``run_audit``, which hard-codes ``institutional``.
"""

import copy
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel

from ops_suite.common.config import PathsSettings, Settings, TdxSettings
from ops_suite.common.llm import LlmClient, LlmResult
from ops_suite.common.scrub import DataClass, ScrubbedText
from ops_suite.tdx.alerts import alertable, open_findings
from ops_suite.tdx.demo.app import WorkMgmtServer
from ops_suite.tdx.watch import AuditRun, SummaryKind, SummaryRequest

DEMO_HEADER = (
    "Demo: synthetic TDX pages on 127.0.0.1 (no real data, nothing leaves this computer "
    "unless --send)."
)
DEMO_CARD_TITLE = "ops-suite DEMO · synthetic data"
DEMO_PREFIX = "DEMO: "
# --summary-kind delta: the pretend last post, and a ticket open then that is gone now (it is
# not on the synthetic dashboard, which is read in full, so it counts as cleared).
DEMO_DELTA_SINCE = timedelta(hours=4, minutes=30)
DEMO_CLEARED_ID = 1000090


class DemoLlmClient(LlmClient):
    """An LlmClient for the demo: synthetic text only, so ``llm.enabled`` is not needed.

    It passes ``force_enabled`` on every call and refuses any other data class, so it can
    never carry real ticket text past the switch.
    """

    def classify[T: BaseModel](
        self,
        text: ScrubbedText,
        *,
        system: str,
        output: type[T],
        data_class: DataClass,
        purpose: str,
        ref: str = "",
        force_enabled: bool = False,
    ) -> LlmResult[T]:
        if data_class != "synthetic":
            raise ValueError("the demo client takes synthetic data only")
        return super().classify(
            text,
            system=system,
            output=output,
            data_class=data_class,
            purpose=purpose,
            ref=ref,
            force_enabled=True,
        )


def demo_settings(
    settings: Settings, server: WorkMgmtServer, data_dir: Path, *, headless: bool
) -> Settings:
    """The person's settings with a default TDX section on the synthetic app.

    The TDX part is built from defaults, not copied: the person's own rules (staff names
    included), dashboard address, profile folder and ticket template stay out of the demo,
    and the expected counts do not depend on them. No pause between tickets on localhost.
    """
    tdx = TdxSettings(
        base_url=server.base_url,
        dashboard_url=server.frame("d=2"),
        headless=headless,
        feed_delay_seconds=0,
    )
    paths = PathsSettings(data_dir=data_dir, log_dir=settings.paths.log_dir)
    return settings.model_copy(update={"tdx": tdx, "paths": paths})


def demo_card(card: dict[str, object]) -> dict[str, object]:
    """The audit card, marked as a demo: a title line first, and ``DEMO:`` on the preview."""
    marked = copy.deepcopy(card)
    body = marked.get("body")
    if isinstance(body, list):
        body.insert(
            0,
            {
                "type": "TextBlock",
                "text": DEMO_CARD_TITLE,
                "wrap": True,
                "weight": "Bolder",
                "size": "Large",
                "color": "Attention",
            },
        )
    marked["fallbackText"] = DEMO_PREFIX + str(marked.get("fallbackText", "ops-suite"))
    return marked


def demo_summary_request(run: AuditRun, settings: Settings, kind: SummaryKind) -> SummaryRequest:
    """As if a summary slot were due now. ``full`` is the day's first post. ``delta`` pretends
    the last post was 4.5 hours ago, before the first two alertable findings and while
    #1000090 was still open, so the card shows new, cleared and open counts."""
    if kind == "full":
        return SummaryRequest(run.now)
    findings = alertable(
        open_findings(run.result.findings),
        alert_ready_to_close=settings.tdx.alerts.alert_ready_to_close,
    )
    keys = [(finding.ticket_id, finding.kind) for finding in findings]
    previous = frozenset(keys[2:]) | {(DEMO_CLEARED_ID, "client_waiting")}
    return SummaryRequest(
        run.now, kind="delta", since=run.now - DEMO_DELTA_SINCE, previous=previous
    )
