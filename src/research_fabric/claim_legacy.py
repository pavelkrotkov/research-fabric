"""Compatibility for retained pre-identity packets and positional grounding reports.

Migration is explicit: an original published ledger proves record order and
source bytes before host acceptance assigns IDs. Only that saved mapping and
original packet state authorize an old report. Reaccepting a shortened packet
cannot create this proof. Report normalization consumes this persisted migration proof; normal host
acceptance deliberately does not produce it.
"""

from ._source_adapter import ADAPTERS
from .claims import _claims, accept_packet, claim_id_for, require, source_file_revision, stable_revision


def migrate_packet(packet, worker, original_ledger, *, source_dir, adapters=ADAPTERS):
    """Only retained original order, records and source digests authorize positional legacy reports."""
    claims = _claims(packet)
    rows = [row for row in original_ledger if str(row.get("claim_id", "")).startswith(f"c-{worker}-")]
    ids = [claim_id_for(worker, ordinal) for ordinal in range(1, len(claims) + 1)]
    require([row.get("claim_id") for row in rows] == ids, "legacy packet order/count differs from original ledger")
    for row, claim in zip(rows, claims):
        _legacy_record(row, claim, source_dir)
    accepted = accept_packet(packet, worker, source_dir=source_dir, adapters=adapters)
    accepted.update(
        legacy_claim_id_map=dict(zip(ids, ids)),
        legacy_original_state=accepted["packet_state_revision"],
        legacy_ledger_revision=stable_revision(rows),
    )
    return accepted


def _legacy_record(row, claim, source_dir):
    fields = ("claim", "excerpt", "locator", "stance", "source_file")
    require(
        all(row.get(field) == claim.get(field) for field in fields), "legacy packet records differ from original ledger"
    )
    require(
        row.get("source_revision") == source_file_revision(source_dir, claim.get("source_file", "")),
        "legacy migration requires source revision from the retained original source ledger",
    )
