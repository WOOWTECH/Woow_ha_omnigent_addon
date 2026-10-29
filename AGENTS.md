# AGENTS.md

Rules for every agent and person who changes this repository. The full
release mechanism is described in the Woow_HA_App_Store README (section
"版本策略與同步") and in ADR-0005 of the WOOW addon pipeline.

1. **``main`` only changes through a pull request.** A ruleset blocks direct
   pushes, force pushes and deleting the branch, and nobody can bypass it.
   Push a branch, open a PR, wait for the required checks, then merge.
2. **A PR that changes an addon version is a release.** Merging it publishes
   that version to the WOOW App Store. It must pass every required check; the
   checks are the approval, there is no human review step. Every PR also
   runs `Upgrade comparison / compare` (pipeline #89): it compares each addon
   whose version the PR changes with the latest Stable Release. It becomes a
   required check once it is added to the ruleset (pipeline #120); treat a
   failure as blocking already. A PR that changes no version passes it in
   seconds. The result is in the job summary. To re-run it, use "Re-run all
   jobs", not "Re-run failed jobs": a re-run judges its own build, and an
   earlier attempt's SBOM is not used.
3. **Breaking changes must be declared in `upgrade-notes.yaml`** next to the
   addon's `config.yaml` (here `omnigent/upgrade-notes.yaml`). Create the
   file only when the upgrade comparison asks for it: it blocks a breaking
   change without a matching entry, and a new fixable Critical or High
   vulnerability that is not accepted. For a breaking change the report
   prints a ready-to-paste entry; add the migration path. `migration` and
   `reason` must not be empty.

   ```yaml
   "0.1.16":                      # the new version, quoted
     breaking:
       - change: option-removed   # change and key exactly as the report shows
         key: base_url
         migration: Add an env_vars entry OMNIGENT_ACCOUNTS_BASE_URL with the public URL.
     accepted_vulnerabilities:
       - id: CVE-2026-1234        # id and package exactly as the report shows
         package: openssl
         reason: Upstream base image not updated yet; replaced in 0.1.17.
   ```

   The change names, which sections count and how the entries appear on the
   GitHub Release: the Woow_HA_App_Store README, section
   "升版比對與 `upgrade-notes.yaml`". The code wins on any difference:
   `.addon-sync/upgrade_check.py` (change names, when the check blocks) and
   the comment above `REBUILD` in `.addon-sync/addon_sync.py` (file format,
   version order, Release notes).
