# Customer Solutions triage digest

**Reference implementation, not maintained - fork and adapt.**

## Safety model (lead)

- **Read-only.** It never sends, drafts, replies, forwards, files, moves, deletes, marks read,
  categorises, flags, or changes rules. There is no send path and no import of any mail-sending
  code.
- **Fail closed.** `validate_config()` runs before any work. Bad config aborts with no output.
  Threads that cannot be classified stay `unclassified` with `Low` confidence and
  `safe_to_draft=false`.
- **`safe_to_draft` can only be narrowed by config.** Code enforces a hard allowlist
  (`portal`, `statements`, `decals`), a forced human-only set (money/card/escalation), and exclusion
  of Low-confidence threads. Config may set `safe_to_draft: false` anywhere; it cannot widen the
  allowlist.
- **Deterministic.** All lists are explicitly sorted (folder order, then days waiting descending,
  then message id) and JSON is written with sorted keys.

## Config keys

`config.example.yaml` uses JSON syntax, which is valid YAML 1.2, so no YAML dependency is needed.

| Key | Meaning |
| --- | --- |
| `mailbox` | Mailbox address this config describes |
| `rules_version` | Free-text label written into the output |
| `folders` | `{name, queue_type, stale_after_days}`; `queue_type` is `arrival`, `chase` or `stale` |
| `completed_folder` | Folder used for de-duplication |
| `completed_cache` | `{path, max_age_days}` for cached conversation ids |
| `account_code_regex` | Regex used to extract account codes |
| `categories` | Ordered rules: `{name, match[], confidence, safe_to_draft, priority}` plus optional `all_of`, `any_of`, `applies_to_folders`, `requires_account_code` |
| `human_only_categories` | Categories forced to `safe_to_draft=false` |
| `waiting_on_rules` | Ordered `{match[], label}` rules, first match wins |
| `waiting_on_default` | Label used when no waiting-on rule matches |
| `next_actions` | Per-category suggested action strings |
| `next_action_default` | Action used for categories without an entry |
| `llm_residue` | `{enabled: false}` - no LLM is wired in |
| `output_dir`, `digest_filename_pattern` | Output location and filename pattern (`{date}` required) |

## Adapting to another mailbox

1. Copy `config.example.yaml` to `config.yaml` and change `mailbox`.
2. Update `folders` to the target mailbox's names and queue types.
3. Replace the `categories` phrases. Keep `safe_to_draft: true` only for portal/statements/decals
   style categories; everything else false.
4. Point `completed_cache.path` at the target cache and build a snapshot.
5. Replace the mailbox-specific mapping blocks with your own:
   - `waiting_on_rules` - ordered sender patterns and labels (credit team, support queue,
     account manager, bounce, customer).
   - `next_actions` / `next_action_default` - your playbook strings per category.
6. Run `validate_config()` first; it must pass before any output is written.

## Usage

```python
from digest import load_config, build
cfg = load_config("config.yaml")
doc = build("snapshot.json", cfg, run_at="2026-10-22T10:00:00+11:00")
```

## Licence

MIT - see `LICENSE`.
