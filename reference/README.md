# Reference snapshots

These files preserve the related local work in this cloud repository. The snapshots are historical, with their owning source repositories and revisions recorded in the [source index](../docs/SOURCE_WORK_INDEX.md).

- [Complete HA prototype source](ha-prototype-source/) contains the separate combined-integration checkout, including its locally modified and untracked exploratory work.
- [HA prototype documents](ha-prototype-docs/) and [exploratory tests](ha-exploratory-tests/) make the key evidence easy to find.
- [Standalone Rivian schedule harness draft](standalone-harness-draft/) is experimental and must be audited against the [implementation brief](../docs/RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md) before use.
- [Riviamigo schedule-read patch](riviamigo-upstream-schedule-read.patch) preserves changes that were local-only in a separate worktree.

The prototype source is under `reference/` to prevent its historical GitHub/Forgejo release workflows from activating in this repository. It is not a new deployment or proof of physical charging control. Do not put credentials, exact schedule snapshots, or raw API logs in this tree.
