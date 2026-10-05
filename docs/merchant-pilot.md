# First real merchant pilot — operator procedure

M5B [delivery context](delivery-context.md) is code/fixture only. No merchant activation
or shipping endpoint is added. Awin/TradeDoubler shipping has unknown scope; audited
CJ country shipping retains UNKNOWN tax. Item price != delivered total; market !=
destination; unknown != zero; reference FX != landed-cost ranking; watches remain
item-price based. Changed normalization revision `m5b-feed-v1` requires renewed
technical validation before a pilot resumes. Existing policy/cache review still applies.

M4E is fixture verified. No Awin/TradeDoubler/CJ merchant was activated live by this
milestone. Use one approved retailer/catalog/market first. Rights reviewed,
technically validated, activated, published and live verified are separate states.

The lifecycle is exactly:

candidate → pending MerchantProgram → explicit policy review/approval →
merchant-program-validate → passed technical validation → explicit activation →
guarded feed sync → materialization/search → independent live verification.

## Exact first-pilot steps

1. Apply `uv run alembic upgrade head`. Keep the selected network disabled and the
   pilot UUID absent from `FEED_PROGRAM_IDS`. Obtain actual advertiser/feed access
   and review allowed catalog storage, caching, tracking/history, refresh, affiliate
   display and attribution. Configure only approved credentials locally; never put
   them in JSON reports, review references, reasons or the command line.
2. Discover the account's available feeds and create a pending template:

   ```sh
   uv run python -m pricehunter.apps.admin merchant-candidates NETWORK
   uv run python -m pricehunter.apps.admin merchant-program-template NETWORK ADVERTISER_ID --feed-id FEED_ID > pending-program.json
   ```

   Replace `NETWORK` with `awin`, `tradedoubler` or `cj` and use actual IDs. Correct
   `domain`, catalog market, native currency, language and display name. Do not
   infer selling-market rights from advertiser domicile. Keep active/approved false
   and policy unreviewed. No automatic canonical Merchant linking occurs.
3. Import pending identity and retain the printed UUID/version:

   ```sh
   uv run python -m pricehunter.apps.admin merchant-program-import pending-program.json --dry-run
   uv run python -m pricehunter.apps.admin merchant-program-import pending-program.json --confirm
   uv run python -m pricehunter.apps.admin merchant-programs
   ```

4. Prepare `reviewed-policy.json` from the approved contract with reviewed=true,
   nonsecret review_reference, catalog_persistence_allowed=true and only actually
   granted tracking/history/refresh/affiliate permissions and cache age. Preview
   and approve using the current version (1 for a newly imported program):

   ```sh
   uv run python -m pricehunter.apps.admin merchant-program-review PROGRAM_UUID --expected-version 1 --policy-file reviewed-policy.json --reason "Pilot contract reviewed" --dry-run
   uv run python -m pricehunter.apps.admin merchant-program-review PROGRAM_UUID --expected-version 1 --policy-file reviewed-policy.json --reason "Pilot contract reviewed" --confirm
   ```

5. Validate while inactive; inspect the immutable report:

   ```sh
   uv run python -m pricehunter.apps.admin merchant-program-validate PROGRAM_UUID
   uv run python -m pricehunter.apps.admin merchant-program-validations PROGRAM_UUID
   uv run python -m pricehunter.apps.admin merchant-program-validation-show VALIDATION_UUID
   ```

   Require `status=passed`, correct current fingerprint and sensible row/quality
   metrics. Validation writes only evidence and grants no rights. A failed validation
   prints its safe report and exits unsuccessfully. Diagnose the source before retrying.
6. Activate explicitly with the current reviewed version (2 for the sequence above):

   ```sh
   uv run python -m pricehunter.apps.admin merchant-program-activate PROGRAM_UUID --expected-version 2 --reason "Reviewed and validated pilot" --dry-run
   uv run python -m pricehunter.apps.admin merchant-program-activate PROGRAM_UUID --expected-version 2 --reason "Reviewed and validated pilot" --confirm
   ```

   Latest matching validation must pass, match the adapter revision and be at most
   seven days old by default. No upstream version equality is required. A failed
   rerun prevents falling back to an older pass. Re-enable has the same gate.
7. Add only this UUID to `FEED_PROGRAM_IDS`, enable only its reviewed network and
   load that configuration into the intended application processes. Then:

   ```sh
   uv run python -m pricehunter.apps.admin merchant-program-check PROGRAM_UUID
   uv run python -m pricehunter.apps.admin feed-sync PROGRAM_UUID --dry-run
   uv run python -m pricehunter.apps.admin feed-sync PROGRAM_UUID --confirm
   uv run python -m pricehunter.apps.admin feed-diagnostics PROGRAM_UUID
   uv run python -m pricehunter.apps.admin feed-search PROGRAM_UUID "KNOWN PRODUCT"
   uv run python -m pricehunter.apps.admin coverage-report
   uv run python -m pricehunter.apps.admin coverage-product "KNOWN PRODUCT" --country BE
   ```

   Replace BE with the reviewed catalog market. Require a successful generation,
   expected active distinct count, no quality error and usable source links. Search
   materializes through the existing catalog according to the reviewed policy.
8. Independently check representative real products: native prices and currency,
   availability, identifiers, size/colour variants, destination retailer and actual
   affiliate/direct attribution. Check a permitted watch if tracking/history rights
   were granted. Record date, program/feed/market, validation ID, generation, review
   reference and nonsecret live evidence. Only now mark this pilot **live verified**.
9. Observe at least a subsequent guarded sync. Investigate rejected candidates using
   [feed quality](feed-quality.md); never enable a permanent bypass. For rollback,
   `merchant-program-disable PROGRAM_UUID --expected-version CURRENT_VERSION --reason
   "Pilot paused" --confirm` hides that program while preserving identity/history.

Changing a feed reference clears approval and invalidates validation by fingerprint.
Repeat rights review, technical validation and explicit activation. Cosmetic Merchant
renames preserve technical evidence. Canonical linking is a separate reviewed action;
pending watch notifications with an old Merchant UUID are cancelled after reassignment.
