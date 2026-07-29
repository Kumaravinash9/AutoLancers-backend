"""Normalising what the extension scraped — the parsing the browser deliberately doesn't do.

These cover the pure half of ``services.capture``: the string parsing, the dedupe, and the rule that
an LLM reading may only fill what the selectors left empty. ``store_posting`` needs a database and
is exercised by hand against a running backend (see the README's verification notes).
"""

from __future__ import annotations

import datetime as dt

from app.connectors.freelancer import JobPosting
from app.services.capture import (
    PARSE_KIND_BY_PAGE,
    SESSION_STATUSES,
    capture_hash,
    dedupe,
    fingerprint,
    item_from_card,
    match_llm_items,
    merge_llm_fields,
    parse_money,
    parse_relative_time,
    parse_work_type,
)

NOW = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.UTC)


class TestParseMoney:
    def test_range(self):
        assert parse_money("$500.00 - $1,000.00") == (500.0, 1000.0, "USD")

    def test_single_figure_fills_both_ends(self):
        # Reporting one figure as a minimum with no maximum would read as "unbounded" to the
        # budget filter, which is the opposite of what a fixed price means.
        assert parse_money("£750") == (750.0, 750.0, "GBP")

    def test_k_suffix(self):
        assert parse_money("$40K") == (40000.0, 40000.0, "USD")

    def test_no_money(self):
        assert parse_money("Hourly") == (None, None, None)
        assert parse_money(None) == (None, None, None)

    def test_symbol_becomes_iso_code(self):
        # "₹" would silently fail to match "INR" everywhere downstream.
        assert parse_money("₹5,000")[2] == "INR"


class TestParseRelativeTime:
    def test_hours_ago_resolves_against_the_client_clock(self):
        assert parse_relative_time("3 hours ago", NOW) == NOW - dt.timedelta(hours=3)

    def test_inside_a_longer_string(self):
        assert parse_relative_time("Posted 2 days ago", NOW) == NOW - dt.timedelta(days=2)

    def test_absent(self):
        assert parse_relative_time("yesterday", NOW) is None
        assert parse_relative_time(None, NOW) is None


class TestParseWorkType:
    def test_lowercase_because_scoring_compares_lowercase(self):
        # services.scoring._budget_floor tests `budget_type == "hourly"`. Uppercase here would pick
        # no floor and every job would pass the budget filter.
        assert parse_work_type("Hourly: $30.00-$50.00") == "hourly"
        assert parse_work_type("Fixed-price") == "fixed"

    def test_per_hour_shapes(self):
        assert parse_work_type("$45.00/hr") == "hourly"

    def test_unknown_is_none_not_a_guess(self):
        assert parse_work_type("$500.00") is None
        assert parse_work_type(None) is None


def card(**overrides):
    base = {
        "external_id": "~021111111111111111111",
        "url": "https://www.upwork.com/jobs/~021111111111111111111",
        "title": "Build a Django API",
        "description": "We need an API for our internal tooling. " * 3,
        "description_complete": False,
        "budget": "$500.00 - $1,000.00",
        "proposals": 12,
        "posted": "3 hours ago",
        "skills": ["Django", "Python"],
    }
    base.update(overrides)
    return base


class TestItemFromCard:
    def test_maps_a_card_onto_columns(self):
        item = item_from_card("upwork", card(), NOW)
        assert item is not None
        assert item.posting.external_id == "~021111111111111111111"
        assert (item.posting.budget_min, item.posting.budget_max) == (500.0, 1000.0)
        assert item.posting.currency == "USD"
        assert item.posting.bid_count == 12
        assert item.posting.posted_at == NOW - dt.timedelta(hours=3)
        assert item.posting.skills_listed == ["Django", "Python"]

    def test_no_id_is_dropped(self):
        # Without the marketplace's id there is nothing to dedupe the next sighting against, so this
        # row would arrive as a brand-new project on every single collection.
        assert item_from_card("upwork", card(external_id=""), NOW) is None

    def test_gaps_name_what_is_missing(self):
        item = item_from_card("upwork", card(budget=None, posted=None), NOW)
        assert "min_budget" in item.gaps
        assert "posted_at" in item.gaps
        # The card's own budget string carries no "/hr", so the type is genuinely unknown here.
        assert "work_type" in item.gaps

    def test_a_found_field_is_not_a_gap(self):
        # The gap list is what decides whether an LLM call gets paid for. JobPosting spells these
        # `budget_min`/`budget_type` while the table spells them `min_budget`/`work_type`, and
        # checking the column name against the attribute read every posting as missing both — so
        # every page bought a model call it did not need.
        item = item_from_card("upwork", card(), NOW)
        assert "min_budget" not in item.gaps
        assert "description" not in item.gaps
        assert "posted_at" not in item.gaps
        assert "skills" not in item.gaps

    def test_page_key_is_recorded_without_changing_identity(self):
        item = item_from_card("upwork", {**card(), "page_key": "best_matches"}, NOW)
        assert item.bid_information["source_page"] == "best_matches"


class TestDedupe:
    def test_same_job_on_two_pages_is_one_item(self):
        # Best matches and Most recent show the same postings; two sightings must not become two
        # projects.
        kept, dropped = dedupe(
            [
                item_from_card("upwork", card(), NOW),
                item_from_card("upwork", card(), NOW),
            ]
        )
        assert len(kept) == 1
        assert dropped == 1

    def test_keeps_the_richer_sighting(self):
        thin = item_from_card("upwork", card(budget=None, skills=[], description=""), NOW)
        rich = item_from_card("upwork", card(), NOW)
        kept, _ = dedupe([thin, rich])
        assert kept[0].posting.budget_min == 500.0

    def test_order_does_not_decide(self):
        thin = item_from_card("upwork", card(budget=None, skills=[], description=""), NOW)
        rich = item_from_card("upwork", card(), NOW)
        assert dedupe([rich, thin])[0][0].posting.budget_min == 500.0

    def test_different_ids_are_kept(self):
        kept, dropped = dedupe(
            [
                item_from_card("upwork", card(), NOW),
                item_from_card("upwork", card(external_id="~0222"), NOW),
            ]
        )
        assert len(kept) == 2
        assert dropped == 0


class TestMergeLlmFields:
    def test_fills_a_gap(self):
        item = item_from_card("upwork", card(budget=None), NOW)
        filled = merge_llm_fields(item, {"min_budget": 800, "max_budget": 1200}, NOW)
        assert filled == 2
        assert item.posting.budget_min == 800.0

    def test_never_overwrites_a_selector_hit(self):
        # A model asked to read a page it can mostly see will restate a title slightly differently.
        # Letting that win would make the same job's title flicker between collections.
        item = item_from_card("upwork", card(), NOW)
        merge_llm_fields(item, {"title": "Build A Django Api (Urgent)", "min_budget": 99}, NOW)
        assert item.posting.title == "Build a Django API"
        assert item.posting.budget_min == 500.0

    def test_a_null_reading_fills_nothing(self):
        item = item_from_card("upwork", card(budget=None), NOW)
        assert merge_llm_fields(item, {"min_budget": None, "work_type": None}, NOW) == 0

    def test_work_type_from_the_model_is_lowercased(self):
        item = item_from_card("upwork", card(budget=None), NOW)
        merge_llm_fields(item, {"work_type": "HOURLY"}, NOW)
        assert item.posting.budget_type == "hourly"

    def test_unlisted_keys_are_ignored(self):
        # An allowlist, so a new key in the LLM schema cannot quietly start writing to a column
        # nobody reviewed.
        item = item_from_card("upwork", card(), NOW)
        merge_llm_fields(item, {"status": "CLOSED", "discovery_method": "API_POLL"}, NOW)
        assert item.bid_information.get("status") is None

    def test_competition_counts_land_in_bid_information(self):
        item = item_from_card("upwork", card(), NOW)
        merge_llm_fields(item, {"interviewing": 3, "connects_required": 16}, NOW)
        assert item.bid_information["interviewing"] == 3
        assert item.bid_information["connects_required"] == 16

    def test_gaps_shrink_after_a_fill(self):
        item = item_from_card("upwork", card(budget=None, posted=None), NOW)
        before = len(item.gaps)
        merge_llm_fields(item, {"min_budget": 800, "posted_at": "2 days ago"}, NOW)
        assert len(item.gaps) < before


class TestMatchLlmItems:
    def test_matched_on_title_because_ids_live_in_hrefs(self):
        items = [item_from_card("upwork", card(budget=None), NOW)]
        filled, unmatched = match_llm_items(
            items, [{"title": "build a django api", "min_budget": 900}], NOW
        )
        assert filled == 1
        assert unmatched == 0
        assert items[0].posting.budget_min == 900.0

    def test_an_invented_item_is_dropped_not_stored(self):
        # No link means no id, and no id means nothing to dedupe against next time — so a job the
        # model describes that no scraped row matches cannot become a project.
        items = [item_from_card("upwork", card(), NOW)]
        filled, unmatched = match_llm_items(items, [{"title": "A job nobody scraped"}], NOW)
        assert (filled, unmatched) == (0, 1)

    def test_truncated_title_still_lands(self):
        long_title = "Senior Django engineer wanted for a long running data platform project"
        items = [item_from_card("upwork", card(title=long_title, budget=None), NOW)]
        filled, unmatched = match_llm_items(
            items, [{"title": long_title[:52], "min_budget": 4000}], NOW
        )
        assert unmatched == 0
        assert filled == 1


class TestCaptureHash:
    """The fingerprint for pages kept whole — contracts, proposals, orders, room lists."""

    def test_same_rows_hash_the_same(self):
        # An unchanged re-collection must bump `times_seen`, not file another copy. Otherwise
        # "accumulate" means the table is unreadable after a hundred runs.
        rows = [{"title": "A contract", "url": "https://x/1"}]
        assert capture_hash(rows) == capture_hash(list(rows))

    def test_changed_rows_hash_differently(self):
        # A changed page is a new row, and that history is the whole point of keeping them.
        assert capture_hash([{"title": "a"}]) != capture_hash([{"title": "b"}])

    def test_key_order_does_not_matter(self):
        assert capture_hash([{"a": 1, "b": 2}]) == capture_hash([{"b": 2, "a": 1}])

    def test_empty_is_stable(self):
        assert capture_hash([]) == capture_hash(None)


class TestParseKindByPage:
    def test_orders_and_contracts_read_as_contracts(self):
        assert PARSE_KIND_BY_PAGE["contracts"] == "contracts"
        assert PARSE_KIND_BY_PAGE["fvr_orders"] == "contracts"
        assert PARSE_KIND_BY_PAGE["pph_orders"] == "contracts"

    def test_proposals_read_as_proposals(self):
        assert PARSE_KIND_BY_PAGE["pph_proposals"] == "proposals"

    def test_message_rooms_are_deliberately_absent(self):
        # Two-party data: half of it belongs to someone who never agreed to any of this. Sending
        # those previews to a model is a further step than reading them, and not one taken by
        # default.
        assert "messages" not in PARSE_KIND_BY_PAGE
        assert "fvr_inbox" not in PARSE_KIND_BY_PAGE


class TestSessionStatuses:
    """What the extension reports about a wall, mapped onto what gets stored."""

    def test_the_three_states(self):
        assert SESSION_STATUSES["ok"] == "OK"
        assert SESSION_STATUSES["signed_out"] == "SIGNED_OUT"
        assert SESSION_STATUSES["blocked"] == "BLOCKED"

    def test_an_unknown_value_is_not_a_problem(self):
        # Inventing a "signed out" from a value we don't understand would send someone off to fix a
        # session that is working fine.
        assert SESSION_STATUSES.get("nonsense", "OK") == "OK"


class TestFingerprint:
    def test_matches_the_pipeline_so_a_re_sighting_is_not_an_edit(self):
        from app.services.pipeline import _fingerprint

        posting = JobPosting(
            platform="upwork",
            external_id="~021",
            title="t",
            description="d",
            url="u",
            skills_listed=["b", "a"],
            budget_min=1.0,
            budget_max=2.0,
            bid_count=3,
        )
        assert fingerprint(posting) == _fingerprint(posting)

    def test_skill_order_does_not_change_it(self):
        one = JobPosting(
            platform="upwork", external_id="~1", title="t", description="d", url="",
            skills_listed=["a", "b"],
        )
        two = JobPosting(
            platform="upwork", external_id="~1", title="t", description="d", url="",
            skills_listed=["b", "a"],
        )
        assert fingerprint(one) == fingerprint(two)
