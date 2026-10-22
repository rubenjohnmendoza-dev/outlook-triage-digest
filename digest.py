"""Config-driven reference implementation of the Customer Solutions triage digest.

SAFETY MODEL (hard-coded, not configurable):
  * read-only: this module never writes to a mailbox and has no send path
  * fail-closed: config is validated before any work; bad config aborts with no output
  * deterministic: every list is explicitly sorted; output is JSON with sorted keys
  * safe_to_draft can only be narrowed by code (human-only categories, forced
    money/card/escalation list, low confidence) - config cannot widen it

config.yaml uses JSON syntax, which is valid YAML 1.2, so the reference
implementation needs no third-party YAML dependency.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

# Hard-coded safety constants. These are NOT read from config.
FORCED_HUMAN_CATEGORIES = {
    "disputes", "payments", "refunds", "cancellations",
    "applications", "escalation", "fees/promo", "account_specific",
}
SAFE_ALLOWED = {"portal", "statements", "decals"}
QUEUE_TYPES = {"arrival", "chase", "stale"}


class ConfigError(RuntimeError):
    pass


def load_config(path: str | Path = "digests/config.yaml") -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid YAML/JSON: {exc}") from exc
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    errors: list[str] = []
    if not cfg.get("mailbox"):
        errors.append("mailbox is required")
    folders = cfg.get("folders") or []
    names = [f.get("name") for f in folders]
    if not folders:
        errors.append("folders must not be empty")
    if len(set(names)) != len(names):
        errors.append("folder names must be unique")
    for f in folders:
        if not f.get("name"):
            errors.append("folder entry missing name")
        if f.get("queue_type") not in QUEUE_TYPES:
            errors.append(f"folder {f.get('name')}: invalid queue_type")
        if not isinstance(f.get("stale_after_days"), int):
            errors.append(f"folder {f.get('name')}: stale_after_days must be an integer")
    cache = cfg.get("completed_cache") or {}
    if not cache.get("path"):
        errors.append("completed_cache.path is required")
    if not isinstance(cache.get("max_age_days"), int):
        errors.append("completed_cache.max_age_days must be an integer")
    try:
        re.compile(cfg.get("account_code_regex", ""))
    except re.error as exc:
        errors.append(f"account_code_regex is invalid: {exc}")
    cats = cfg.get("categories") or []
    if not cats:
        errors.append("categories must not be empty")
    defined = {c.get("name") for c in cats}
    for c in cats:
        if not c.get("name"):
            errors.append("category entry missing name")
        if not isinstance(c.get("match"), list):
            errors.append(f"category {c.get('name')}: match must be a list")
        if c.get("confidence") not in {"High", "Medium", "Low"}:
            errors.append(f"category {c.get('name')}: invalid confidence")
        if not isinstance(c.get("safe_to_draft"), bool):
            errors.append(f"category {c.get('name')}: safe_to_draft must be boolean")
        referenced = bool(c.get("match")) or bool(c.get("applies_to_folders")) or bool(c.get("any_of"))
        if not referenced:
            errors.append(f"category {c.get('name')} is unreferenced (no match or folder rule)")
    for name in cfg.get("human_only_categories") or []:
        if name not in defined:
            errors.append(f"human_only category {name} is not defined")
    for c in cats:
        if c.get("safe_to_draft") and c.get("name") in set(cfg.get("human_only_categories") or []):
            errors.append(f"category {c.get('name')} is both safe_to_draft=true and human-only")
        if c.get("safe_to_draft") and c.get("name") not in SAFE_ALLOWED:
            errors.append(f"category {c.get('name')} cannot be safe_to_draft (code allowlist)")
    pattern = cfg.get("digest_filename_pattern", "")
    if "{date}" not in pattern:
        errors.append("digest_filename_pattern must contain {date}")
    rules = cfg.get("waiting_on_rules")
    if not isinstance(rules, list) or not rules:
        errors.append("waiting_on_rules must be a non-empty list")
    else:
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("match"), list) or not rule.get("label"):
                errors.append("each waiting_on rule needs match[] and label")
    actions = cfg.get("next_actions")
    if not isinstance(actions, dict) or not actions:
        errors.append("next_actions must be a non-empty mapping")
    if errors:
        raise ConfigError("; ".join(errors))


def classify(subject: str, folder: str, code: str | None, cfg: dict) -> tuple[str, str]:
    low = subject.lower()
    for cat in sorted(cfg["categories"], key=lambda c: c.get("priority", 999)):
        folders = cat.get("applies_to_folders")
        if folders and folder not in folders:
            continue
        if cat.get("requires_account_code") and not code:
            continue
        hit = bool(cat.get("applies_to_folders"))
        if not hit:
            hit = any(phrase in low for phrase in cat.get("match", []))
        if not hit and cat.get("all_of") and cat.get("any_of"):
            hit = all(p in low for p in cat["all_of"]) and any(p in low for p in cat["any_of"])
        if hit:
            matched = sum(1 for phrase in cat.get("match", []) if phrase in low)
            if cat.get("priority", 999) < 100 or matched >= 2:
                return cat["name"], cat.get("confidence", "High")
            return cat["name"], "Medium"
    return "unclassified", "Low"


def is_safe(category: str, confidence: str, cfg: dict) -> bool:
    if confidence == "Low" or category not in SAFE_ALLOWED:
        return False
    if category in FORCED_HUMAN_CATEGORIES:
        return False
    if category in set(cfg.get("human_only_categories", [])):
        return False
    entry = [c for c in cfg["categories"] if c["name"] == category]
    return bool(entry) and all(c.get("safe_to_draft") for c in entry)


def build(snapshot_path: str | Path, cfg: dict, run_at: str) -> dict:
    snap = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    now = dt.date(2026, 10, 22)
    completed_ids, cache_age = load_completed_cache(cfg)
    completed = {_norm(x) for x in completed_ids}
    code_re = re.compile(cfg["account_code_regex"], re.I)
    folder_meta = {f["name"]: f for f in cfg["folders"]}
    out = []
    for r in snap["rows"]:
        d = dt.date.fromisoformat(r["date"]) if r["date"] != "UNKNOWN" else None
        days = (now - d).days if d else None
        code_match = code_re.search(r["subject"])
        code = code_match.group(0).upper() if code_match else None
        category, confidence = classify(r["subject"], r["folder"], code, cfg)
        safe = is_safe(category, confidence, cfg)
        if safe:
            reason = ""
        elif category in FORCED_HUMAN_CATEGORIES:
            reason = "money/card category" if category != "escalation" else "security/escalation"
        elif confidence == "Low":
            reason = "low confidence classification"
        else:
            reason = f"{category} is not on the safe list"
        if category == "account_specific":
            reason = "known account, request not stated in subject - read the thread"
        folder_cfg = folder_meta.get(r["folder"], {})
        refund_state = None
        if folder_cfg.get("queue_type") == "stale":
            refund_state = "STALE" if (days or 0) >= folder_cfg.get("stale_after_days", 30) else "OPEN"
        out.append({
            "message_id": hashlib.sha1(f"{r['folder']}|{r['date']}|{r['sender']}|{r['subject']}".encode()).hexdigest()[:12],
            "folder": r["folder"], "category": category, "account_code": code,
            "confidence": confidence, "waiting_on": waiting_on_from_config(r["sender"], cfg),
            "days_waiting": days, "last_activity": r["date"],
            "last_sender": r["sender"], "subject": r["subject"], "safe_to_draft": safe,
            "suggested_next_action": next_action_for(category, cfg),
            "human_only_reason": reason, "refund_state": refund_state, "unread": r["unread"],
            "already_completed": _norm(r["subject"]) in completed,
            "completed_cache_age_days": cache_age,
        })
    order = {f["name"]: i for i, f in enumerate(cfg["folders"])}
    out.sort(key=lambda t: (order.get(t["folder"], 99), -(t["days_waiting"] if t["days_waiting"] is not None else -1), t["message_id"]))
    return {"run_at": run_at, "rules_version": cfg.get("rules_version", "config-v1"), "threads": out}


def _norm(text: str) -> str:
    s = (text or "").strip().lower()
    for _ in range(3):
        stripped = re.sub(r"^(re|fw|fwd)\s*:\s*", "", s).strip()
        if stripped == s:
            break
        s = stripped
    return re.sub(r"\s+", " ", s)


def load_completed_cache(cfg: dict) -> tuple[list[str], int | None]:
    """Load conversation topics from the cache; never fail the run on a bad cache."""

    entry = cfg.get("completed_cache") or {}
    raw = entry.get("path")
    if not raw:
        return [], None
    name = Path(raw).name
    candidates = [
        Path(raw),
        Path.cwd() / raw,
        Path.cwd().parent / raw,
        Path.cwd().parent / name,
        Path.cwd().parent.parent / raw,
    ]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return [], None
            ids = data.get("ids") or data.get("captured_ids") or []
            age = None
            ts = data.get("timestamp")
            if isinstance(ts, str):
                try:
                    stamp = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age = max(0, (dt.datetime(2026, 10, 22, tzinfo=stamp.tzinfo) - stamp).days)
                except Exception:
                    age = None
            return list(ids), age
    return [], None


def waiting_on_from_config(sender: str, cfg: dict) -> str:
    """Ordered, case-insensitive matching; first rule wins. Deterministic."""

    for rule in cfg.get("waiting_on_rules", []):
        patterns = rule.get("match", [])
        if any(re.search(pattern, sender, re.I) for pattern in patterns):
            return rule.get("label", cfg.get("waiting_on_default", "unknown"))
    return cfg.get("waiting_on_default", "internal/other")


def next_action_for(category: str, cfg: dict) -> str:
    return cfg.get("next_actions", {}).get(
        category, cfg.get("next_action_default", "Human review")
    )
