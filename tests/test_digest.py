from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import digest


REPO_ROOT = Path(__file__).resolve().parents[1]


def make_config() -> dict:
    return {
        "mailbox": "customersolutions@example.test",
        "rules_version": "test-rules",
        "folders": [
            {"name": "Inbox", "queue_type": "arrival", "stale_after_days": 14},
            {"name": "Refunds", "queue_type": "stale", "stale_after_days": 30},
        ],
        "completed_folder": "Completed",
        "completed_cache": {"path": "completed.json", "max_age_days": 7},
        "account_code_regex": r"\b(?:UF|UP|UC|C0)\d{4}\b",
        "categories": [
            {
                "name": "payments",
                "match": [],
                "all_of": ["manual"],
                "any_of": ["voucher", "transaction"],
                "confidence": "High",
                "safe_to_draft": False,
                "priority": 10,
            },
            {
                "name": "refunds",
                "match": [],
                "applies_to_folders": ["Refunds"],
                "requires_account_code": True,
                "confidence": "High",
                "safe_to_draft": False,
                "priority": 30,
            },
            {
                "name": "disputes",
                "match": ["dispute"],
                "confidence": "High",
                "safe_to_draft": False,
                "priority": 102,
            },
            {
                "name": "portal",
                "match": ["portal", "access"],
                "confidence": "High",
                "safe_to_draft": True,
                "priority": 104,
            },
            {
                "name": "statements",
                "match": ["statement"],
                "confidence": "High",
                "safe_to_draft": True,
                "priority": 105,
            },
        ],
        "human_only_categories": ["payments", "refunds", "disputes"],
        "waiting_on_rules": [
            {"match": ["credit"], "label": "credit team"},
            {"match": ["support"], "label": "support queue"},
            {"match": ["manager"], "label": "account manager"},
            {"match": ["@"], "label": "customer"},
        ],
        "waiting_on_default": "internal/other",
        "next_actions": {
            "payments": "Reconcile payment",
            "refunds": "Review refund",
            "disputes": "Collect evidence",
            "portal": "Verify account",
            "statements": "Send statement",
        },
        "next_action_default": "Human review",
        "output_dir": "digests",
        "digest_filename_pattern": "{date}-triage",
    }


def snapshot() -> dict:
    return {
        "rows": [
            {
                "folder": "Inbox",
                "date": "2026-10-20",
                "sender": "credit@example.com",
                "subject": "RE: CAN'T ACCESS PORTAL - UF1234",
                "unread": False,
            },
            {
                "folder": "Inbox",
                "date": "2026-10-10",
                "sender": "support@example.com",
                "subject": "Manual voucher payment",
                "unread": True,
            },
            {
                "folder": "Refunds",
                "date": "2026-06-01",
                "sender": "manager@example.com",
                "subject": "Transaction report UF9999",
                "unread": False,
            },
            {
                "folder": "Inbox",
                "date": "UNKNOWN",
                "sender": "customer@example.com",
                "subject": "General hello",
                "unread": False,
            },
        ]
    }


class DigestTests(unittest.TestCase):
    def test_example_config_valid(self) -> None:
        cfg = digest.load_config(REPO_ROOT / "config.example.yaml")
        self.assertEqual(cfg["rules_version"], "R1,R2,R3,R4a,R5,R6,R7,R8")

    def test_missing_mailbox_rejected(self) -> None:
        cfg = make_config()
        cfg.pop("mailbox")
        with self.assertRaisesRegex(digest.ConfigError, "mailbox is required"):
            digest.validate_config(cfg)

    def test_duplicate_folders_rejected(self) -> None:
        cfg = make_config()
        cfg["folders"].append(dict(cfg["folders"][0]))
        with self.assertRaisesRegex(digest.ConfigError, "folder names must be unique"):
            digest.validate_config(cfg)

    def test_invalid_queue_type_rejected(self) -> None:
        cfg = make_config()
        cfg["folders"][0]["queue_type"] = "archive"
        with self.assertRaisesRegex(digest.ConfigError, "invalid queue_type"):
            digest.validate_config(cfg)

    def test_invalid_account_regex_rejected(self) -> None:
        cfg = make_config()
        cfg["account_code_regex"] = "["
        with self.assertRaisesRegex(digest.ConfigError, "account_code_regex is invalid"):
            digest.validate_config(cfg)

    def test_no_categories_rejected(self) -> None:
        cfg = make_config()
        cfg["categories"] = []
        with self.assertRaisesRegex(digest.ConfigError, "categories must not be empty"):
            digest.validate_config(cfg)

    def test_invalid_confidence_rejected(self) -> None:
        cfg = make_config()
        cfg["categories"][0]["confidence"] = "Certain"
        with self.assertRaisesRegex(digest.ConfigError, "invalid confidence"):
            digest.validate_config(cfg)

    def test_safe_category_outside_allowlist_rejected(self) -> None:
        cfg = make_config()
        cfg["categories"].append(
            {
                "name": "newsletters",
                "match": ["newsletter"],
                "confidence": "High",
                "safe_to_draft": True,
                "priority": 120,
            }
        )
        with self.assertRaisesRegex(digest.ConfigError, "code allowlist"):
            digest.validate_config(cfg)

    def test_unknown_human_only_category_rejected(self) -> None:
        cfg = make_config()
        cfg["human_only_categories"].append("not-defined")
        with self.assertRaisesRegex(digest.ConfigError, "not defined"):
            digest.validate_config(cfg)

    def test_filename_pattern_requires_date(self) -> None:
        cfg = make_config()
        cfg["digest_filename_pattern"] = "latest-triage"
        with self.assertRaisesRegex(digest.ConfigError, "must contain \\{date\\}"):
            digest.validate_config(cfg)

    def test_ordered_all_of_rule_matches(self) -> None:
        cfg = make_config()
        category, confidence = digest.classify(
            "Manual card transaction", "Inbox", None, cfg
        )
        self.assertEqual((category, confidence), ("payments", "High"))

    def test_low_priority_single_keyword_is_medium(self) -> None:
        cfg = make_config()
        category, confidence = digest.classify("Portal login", "Inbox", None, cfg)
        self.assertEqual((category, confidence), ("portal", "Medium"))

    def test_unmatched_subject_is_unclassified(self) -> None:
        cfg = make_config()
        category, confidence = digest.classify("General hello", "Inbox", None, cfg)
        self.assertEqual((category, confidence), ("unclassified", "Low"))

    def test_low_confidence_never_safe(self) -> None:
        self.assertFalse(digest.is_safe("portal", "Low", make_config()))

    def test_forced_human_category_never_safe(self) -> None:
        cfg = make_config()
        self.assertFalse(digest.is_safe("payments", "High", cfg))
        self.assertFalse(digest.is_safe("statements", "High", {**cfg, "human_only_categories": ["statements"]}))

    def test_waiting_on_first_rule_wins(self) -> None:
        cfg = make_config()
        self.assertEqual(
            digest.waiting_on_from_config("credit-support@example.com", cfg),
            "credit team",
        )
        self.assertEqual(
            digest.waiting_on_from_config("someone@example.com", cfg),
            "customer",
        )

    def test_build_is_deterministic_and_flags_completed(self) -> None:
        cfg = make_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            snapshot_path = temp / "snapshot.json"
            cache_path = temp / "completed.json"
            snapshot_path.write_text(json.dumps(snapshot()), encoding="utf-8")
            cache_path.write_text(
                json.dumps(
                    {
                        "ids": ["CAN'T ACCESS PORTAL - UF1234"],
                        "timestamp": "2026-10-20T10:00:00+11:00",
                    }
                ),
                encoding="utf-8",
            )
            cfg["completed_cache"]["path"] = str(cache_path)

            first = digest.build(snapshot_path, cfg, "2026-10-22T10:00:00+11:00")
            second = digest.build(snapshot_path, cfg, "2026-10-22T10:00:00+11:00")

        self.assertEqual(first, second)
        self.assertEqual(
            [thread["folder"] for thread in first["threads"]],
            ["Inbox", "Inbox", "Inbox", "Refunds"],
        )
        self.assertEqual(first["threads"][0]["category"], "payments")
        self.assertTrue(
            next(
                thread["already_completed"]
                for thread in first["threads"]
                if thread["category"] == "portal"
            )
        )
        refund = next(
            thread for thread in first["threads"] if thread["folder"] == "Refunds"
        )
        self.assertEqual(refund["refund_state"], "STALE")
        self.assertEqual(first["threads"][0]["completed_cache_age_days"], 1)

    def test_build_marks_low_confidence_unknown_human_only(self) -> None:
        cfg = make_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot()), encoding="utf-8")
            result = digest.build(snapshot_path, cfg, "2026-10-22T10:00:00+11:00")

        unknown = next(
            thread
            for thread in result["threads"]
            if thread["subject"] == "General hello"
        )
        self.assertEqual(unknown["category"], "unclassified")
        self.assertEqual(unknown["confidence"], "Low")
        self.assertFalse(unknown["safe_to_draft"])
        self.assertEqual(unknown["human_only_reason"], "low confidence classification")


if __name__ == "__main__":
    unittest.main()
