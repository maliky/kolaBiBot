# AGENT – Codex Playbook

## 1. Quick Repository Map
- `kolabi/runtime/kola/`: active runtime shell and transitional order-management logic that still drives the sacred head/tail path.
- `archives/runtime/`: archived runners and historical entrypoints kept only for behavioural reference.
- `kolabi/bargain/`: direct exchange CLI and smoke-test surface.
- `Orders/*.tsv`: source of truth for scripted orders; grammar uses mixed French/English comments.
- `tests/`: active regression suite for the current code path.

## 2. Priorities From Product Notes
1. Finish platform-agnostic core before introducing async.
2. Persist price history via DB-agnostic layer (SQLAlchemy, start with SQLite, keep path to Postgres/etc.).
3. Build deterministic test harness that replays synthetic-yet-plausible markets for conditional orders.
4. Preserve TSV order grammar + bilingual comments when migrating features.

## 3. Environment & Tooling
- Python version is forced by `.python-version` (`kola`). If pyenv lacks it, either install via `pyenv install` or temporarily override with `PYENV_VERSION=system`. Document any change.
- Required packages are split: legacy uses `setup.py`, new code needs the pinned deps in `requirements.txt` **plus** `python-binance`, `responses`, `sqlalchemy`, etc. Verify before running CI.
- Tests: start with `pytest tests/exchanges -q` (new stack) to avoid long legacy suites. Legacy tests rely on BitMEX dummy objects and may assume data in `Orders/`.
- `run.sh` only wires `.env`; it does **not** start services. Use `python -m kolabi.bot run ...` for TSV strategies and `python -m kolabi.bot run-once ...` for one compatibility-vocabulary order pair.

## 4. Security / Secrets
- Real API keys live under `kolaBiBot/kola/secrets.py` in plain text. Rotate/remove before sharing builds. Favor env vars + `.env` (already referenced in `run.sh`).

## 5. Suggested First Working Example
1. Implement a minimal engine in `kolabi/` that reads one TSV, drives price/time conditions, and submits orders through `ExchangeABC`.
2. Use `DummyBitMEX` (ported or wrapped) so integration tests run offline.
3. Record every tick/order event through SQLAlchemy models (start with SQLite file).
4. Expose it via the active bot CLI (`python -m kolabi.bot run --strategy Orders/demo_ada.org ...`) so the strategy path stays single and obvious.

## 6. Style & Notes
- Keep comments bilingual when touching legacy sections.
- Use British English as the working language for code, docs, and reports unless the user explicitly asks otherwise.
- Preserve the existing Org formatting style in repo docs such as `README.org`, `DEV.org`, and `MANUAL.org`; extend the local pattern instead of reformatting the whole file.
- For `.org` documentation, do not hard-wrap lines at 80 columns; keep original long lines and let the user manage wrapping.
- Treat comment lines starting with `# >` in Org files as operator notes for Codex; satisfy them when the surrounding task touches that section, and then remove or replace them with the concrete result.
- Final summaries for the user should be formatted as org-mode bullets/headings done with - and simple *.
- Avoid non-legally-safe glyphs (no arrows like  →, “, fancy quotes, en/em dashes, – etc.); stick to plain ASCII.
- When unsure, inspect `notes.org` and `CODEX-CONTEX.org` for historical intent before deleting/refactoring logic.

This codebase intentionally uses human-level metaphors. Do not mechanically replace them with literal names.

Metaphors are allowed when they name a black box for human complexity, orchestration, market behavior, or strategic agency. Literal names are preferred for typed states, payloads, events, commands, and pure transition functions.

Keep intentional metaphors: =Chronos= =Bargain= =Dragon= =head= =tail= =hook= =flying= =flapping= =market= =MarketAuditor=

Keep Isis narrow: it consumes already targeted strategy events, updates/replaces StrategyState, delegates pair lifecycle semantics to step_pair(), and emits ordered intents only. Pair-name resolution, deduplication, precedence, pending-identity timeout, dependency activation, RuntimeCommand translation, and exchange execution belong outside Isis, mainly to Chronos or Ogun.


Welcome, let's Swing and Jazz  !
