"""Evidence reports retain provenance and safely render only requested details."""

import hashlib
import json
import re
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

import pytest

from uav_debugger import import_bytes, import_file
from uav_debugger.analysis import Selection
from uav_debugger.report import build_markdown_report

FIXTURE = Path(__file__).parent / "fixtures" / "telemetry-gap.tlog"
EPOCH_US = 1_700_000_000_000_000
JSON_FENCE = re.compile(r"(?ms)^(`{3,})json\n(.*?)^\1[ \t]*$")


@pytest.fixture
def session():
    return import_file(FIXTURE)


def _blocks(report):
    matches = list(JSON_FENCE.finditer(report))
    assert len(matches) >= 4, "Report must expose its provenance, selection, coverage and issues"
    return [json.loads(match.group(2)) for match in matches]


def test_report_keeps_input_provenance_and_exact_selection_with_requested_details(session):
    selection = Selection(
        sources=((1, 1),),
        message_ids=(30,),
        start_us=EPOCH_US + 1_000_000,
        end_us=EPOCH_US + 5_000_000,
    )

    report = build_markdown_report(session, selection, selected_indices=(2, 8))
    provenance, filters, coverage, issues, *details = _blocks(report)

    assert provenance == {
        "application": {"name": "UAV Debugger", "version": version("uav-debugger")},
        "source_name": session.source_name,
        "sha256": hashlib.sha256(session.raw_bytes).hexdigest(),
        "size_bytes": len(session.raw_bytes),
        "profile": session.profile,
        "dialect": "common",
        "decoder_version": session.decoder_version,
    }
    assert filters == {
        "sources": [[1, 1]],
        "message_ids": [30],
        "start_us": EPOCH_US + 1_000_000,
        "end_us": EPOCH_US + 5_000_000,
        "bounds": "inclusive",
        "relative_origin_us": EPOCH_US,
    }
    assert coverage["traversal"] == "complete"
    assert coverage["consumed_bytes"] == len(session.raw_bytes)
    assert coverage["remaining_bytes"] == 0
    assert coverage["imported_record_count"] == coverage["imported_decoded_count"] == 12
    assert coverage["filtered_record_count"] == coverage["filtered_decoded_count"] == 2
    assert coverage["imported_opaque_count"] == coverage["filtered_opaque_count"] == 0
    assert coverage["explicit_detail_record_count"] == 2
    assert coverage["import_issue_count"] == coverage["reported_issue_count"] == len(session.issues)
    assert coverage["omitted_issue_count"] == 0
    assert len(issues) == len(session.issues)
    assert {detail["index"] for detail in details} == {2, 8}
    for detail in details:
        original = session.records[detail["index"]]
        assert detail["offset"] == original.offset
        assert detail["frame_offset"] == original.frame_offset
        assert detail["end_offset"] == original.end_offset
        assert detail["timestamp_us"] == original.timestamp_us
        assert detail["raw_frame_hex"] == original.raw_frame.hex()
        assert detail["fields"] == dict(original.fields)
        assert detail["checksum_status"] == "valid"
    assert "clock" in report.lower()
    assert "synchron" in report.lower()
    assert "packet loss" in report.lower()


def test_default_export_does_not_include_unrequested_message_details(session):
    report = build_markdown_report(session, Selection(sources=((1, 1),), message_ids=(30,)))
    _, _, coverage, _, *details = _blocks(report)

    assert coverage["filtered_record_count"] == 2
    assert coverage["explicit_detail_record_count"] == 0
    assert details == []
    assert "raw_frame_hex" not in report
    assert "rollspeed" not in report


def test_observed_gap_summary_keeps_both_endpoints_with_only_one_detailed_record(session):
    selection = Selection(sources=((1, 1),), message_ids=(30,))

    report = build_markdown_report(session, selection, selected_indices=(2,))
    _, _, coverage, _, detail = _blocks(report)

    assert coverage["observation_intervals"] == {
        "total_count": 1,
        "established_count": 1,
        "unavailable_count": 0,
        "longest": {
            "delta_us": 4_000_000,
            "previous_index": 2,
            "index": 8,
            "system_id": 1,
            "component_id": 1,
            "message_id": 30,
        },
    }
    assert detail["index"] == 2
    assert coverage["explicit_detail_record_count"] == 1
    assert "Observation interval summary" in report


@pytest.mark.parametrize("indices", [(1,), (999,), (2, 2), (-1,)])
def test_details_outside_active_selection_unknown_or_duplicate_are_rejected(session, indices):
    selection = Selection(sources=((1, 1),), message_ids=(30,))

    with pytest.raises(ValueError):
        build_markdown_report(session, selection, selected_indices=indices)


@pytest.mark.parametrize(
    "selection, key",
    [
        (Selection(sources=()), "sources"),
        (Selection(message_ids=()), "message_ids"),
    ],
)
def test_explicit_empty_selection_is_preserved_in_report_not_replaced_with_all(
    session, selection, key
):
    _, filters, coverage, issues, *details = _blocks(build_markdown_report(session, selection))

    assert filters[key] == []
    assert coverage["imported_record_count"] == 12
    assert coverage["filtered_record_count"] == 0
    assert coverage["explicit_detail_record_count"] == 0
    assert len(issues) == len(session.issues)
    assert details == []


def test_partial_import_limitations_survive_a_filter_with_no_records(session):
    damaged_bytes = session.raw_bytes[:-1]
    partial = import_bytes(damaged_bytes, source_name="truncated.tlog")

    report = build_markdown_report(partial, Selection(sources=()))
    provenance, _, coverage, issues, *details = _blocks(report)

    assert provenance["sha256"] == hashlib.sha256(damaged_bytes).hexdigest()
    assert coverage["traversal"] == "stopped"
    assert coverage["consumed_bytes"] == 384
    assert coverage["remaining_bytes"] == len(damaged_bytes) - 384
    assert coverage["imported_record_count"] == 11
    assert coverage["filtered_record_count"] == 0
    assert details == []
    assert issues[-1]["code"] == "incomplete_frame"
    assert issues[-1]["record_index"] == 11
    assert issues[-1]["offset"] >= coverage["consumed_bytes"]
    assert coverage["import_issue_count"] == len(partial.issues)


def test_empty_input_report_retains_empty_status_and_unknown_relative_origin():
    result = import_bytes(b"", source_name="empty.tlog")

    _, filters, coverage, issues, *details = _blocks(build_markdown_report(result, Selection()))

    assert coverage["traversal"] == "empty"
    assert filters["relative_origin_us"] is None
    assert coverage["imported_record_count"] == coverage["filtered_record_count"] == 0
    assert [issue["code"] for issue in issues] == ["empty_input"]
    assert details == []


def test_opaque_record_details_never_claim_decoding_or_checksum_validation():
    frame = bytes.fromhex("fd 01 00 00 00 03 01 ff ff ff 00 00 00")
    result = import_bytes(EPOCH_US.to_bytes(8, "big") + frame)

    _, _, coverage, issues, detail = _blocks(
        build_markdown_report(result, Selection(), selected_indices=(0,))
    )

    assert coverage["traversal"] == "complete"
    assert coverage["imported_record_count"] == coverage["filtered_record_count"] == 1
    assert coverage["imported_opaque_count"] == coverage["filtered_opaque_count"] == 1
    assert coverage["imported_decoded_count"] == coverage["filtered_decoded_count"] == 0
    assert detail["fields"] is None
    assert detail["message_name"] is None
    assert detail["checksum_status"] == "unverified"
    assert detail["raw_frame_hex"] == frame.hex()
    assert issues[0]["code"] == "unknown_message"


def test_regression_issue_is_retained_even_when_its_record_is_outside_the_filter(session):
    chunks = [
        timestamp.to_bytes(8, "big") + session.records[index].raw_frame
        for index, timestamp in ((2, 100), (3, 90), (2, 110))
    ]
    result = import_bytes(b"".join(chunks))

    _, _, coverage, issues = _blocks(
        build_markdown_report(result, Selection(sources=((1, 1),), start_us=100))
    )

    assert coverage["filtered_record_count"] == 2
    assert [(issue["code"], issue["record_index"]) for issue in issues] == [
        ("timestamp_regression", 1)
    ]
    assert coverage["observation_intervals"] == {
        "total_count": 1,
        "established_count": 0,
        "unavailable_count": 1,
        "longest": None,
    }


def test_issue_output_limit_discloses_total_displayed_and_omitted_counts(session):
    first_record = EPOCH_US.to_bytes(8, "big") + session.records[0].raw_frame
    result = import_bytes(first_record * 250)

    _, _, coverage, issues = _blocks(build_markdown_report(result, Selection()))

    assert coverage["import_issue_count"] == 249
    assert coverage["reported_issue_count"] == len(issues) == 100
    assert coverage["omitted_issue_count"] == 149
    assert [issue["record_index"] for issue in issues] == list(range(1, 101))
    assert len(result.issues) == 249


def test_issue_limit_preserves_the_stop_reason_after_many_warnings_outside_the_filter(session):
    first_record = EPOCH_US.to_bytes(8, "big") + session.records[0].raw_frame
    result = import_bytes(first_record * 106 + b"\x00")

    _, _, coverage, issues = _blocks(build_markdown_report(result, Selection(sources=())))

    assert coverage["traversal"] == "stopped"
    assert coverage["filtered_record_count"] == 0
    assert coverage["import_issue_count"] == 106
    assert coverage["reported_issue_count"] == len(issues) == 100
    assert coverage["omitted_issue_count"] == 6
    assert [issue["record_index"] for issue in issues[:-1]] == list(range(1, 100))
    assert all(issue["severity"] == "warning" for issue in issues[:-1])
    assert issues[-1]["code"] == "incomplete_timestamp"
    assert issues[-1]["severity"] == "error"
    assert issues[-1]["record_index"] == 106
    assert issues[-1]["offset"] == 106 * len(first_record)


def test_external_strings_cannot_inject_markdown_links_html_headings_or_fences(session):
    hostile = "[click](https://attacker.invalid)\n# Invented finding\n`````\n<script>&"
    record = replace(session.records[0], fields={"text": hostile}, message_name=hostile)
    issue = replace(session.issues[0], message=hostile)
    result = replace(session, source_name=hostile, records=(record,), issues=(issue,))

    report = build_markdown_report(result, Selection(), selected_indices=(0,))
    provenance, _, _, issues, detail = _blocks(report)
    prose = JSON_FENCE.sub("", report)

    assert provenance["source_name"] == hostile
    assert issues[0]["message"] == hostile
    assert detail["message_name"] == hostile
    assert detail["fields"]["text"] == hostile
    assert "attacker.invalid" not in prose
    assert "Invented finding" not in prose
    assert "<script>" not in report
    assert "\n# Invented finding" not in report
    assert record.fields == {"text": hostile}


def test_report_preserves_large_integer_timestamps_in_filters_and_details(session):
    timestamp = 2**64 - 1
    result = import_bytes(timestamp.to_bytes(8, "big") + session.records[0].raw_frame)
    selection = Selection(start_us=timestamp, end_us=timestamp)

    _, filters, _, _, detail = _blocks(
        build_markdown_report(result, selection, selected_indices=(0,))
    )

    assert filters["start_us"] == filters["end_us"] == filters["relative_origin_us"] == timestamp
    assert detail["timestamp_us"] == timestamp


def test_non_finite_measurements_are_explicit_and_distinct_from_observed_strings(session):
    fields = {
        "missing": float("nan"),
        "positive": float("inf"),
        "negative": float("-inf"),
        "literal_text": "nan",
        "samples": (1.0, float("nan")),
    }
    record = replace(session.records[0], fields=fields)
    result = replace(session, records=(record,))

    report = build_markdown_report(result, Selection(), selected_indices=(0,))
    *_, detail = _blocks(report)

    assert detail["fields"] == {
        "missing": {"non_finite_float": "nan"},
        "positive": {"non_finite_float": "inf"},
        "negative": {"non_finite_float": "-inf"},
        "literal_text": "nan",
        "samples": [1.0, {"non_finite_float": "nan"}],
    }
    for match in JSON_FENCE.finditer(report):
        json.loads(
            match.group(2),
            parse_constant=lambda value: pytest.fail(f"Invalid JSON numeric token: {value}"),
        )
    assert record.fields["missing"] is fields["missing"]
