# Releasing SolarSage

## Automated checks

Pull requests run lint, formatting, the Python test suite, documentation builds, HACS validation and Home Assistant manifest validation. Review the results before merging. The HACS repository contains one integration directory; the Rivian controller is bundled inside it.

A version tag triggers a release archive containing only the installable `solar_ev_planner` directory contents. The archive is staged as a workflow artifact for inspection; publishing a GitHub Release is a maintainer action. Keep the tag and manifest version aligned.

## GitHub Pages setup

In repository **Settings → Pages → Build and deployment**, choose **GitHub Actions**. The workflow builds `website/` from `docs/` and deploys only successful `main` builds using the `github-pages` environment. It uses `GITHUB_TOKEN`, with no personal token or deployment key.

The configured URL is `https://bballdavis.github.io/SolarSage/`. If the repository is renamed, update `organizationName`, `projectName`, `url` and `baseUrl` in `website/docusaurus.config.js`, and update README and manifest links. The SolarSage product name does not require renaming the GitHub repository.

## Before publishing

1. Pass Python checks, the full fake-system suite and the production documentation build.
2. Review the release artifact: one integration, bundled controller and logo, no tools, local mappings, credentials, recordings or workspace files.
3. Review [privacy](privacy.md), including Git history and older release assets. Secret detection does not detect every identifying string.
4. Confirm the MIT license and the provenance of any added assets/dependencies.
5. Retain the experimental status until the staged real-system tests have been completed and recorded.
6. Test HACS custom-repository installation on a clean supported Home Assistant instance, then test the documented upgrade path from standalone Rivian control.

## HACS catalog submission

Passing local checks and preparing the repository do not mean it has been accepted into a marketplace. Local integration brand icons are included in `custom_components/solar_ev_planner/brand/`. Confirm they pass validation, and follow the current [HACS publication requirements](https://www.hacs.dev/docs/publish/integration/). Submit only after installation and real-system commissioning evidence are available.

## Release notes

Describe changed behavior, minimum supported Home Assistant version, installation/upgrade steps, known limitations and validation evidence. Call out the standalone Rivian upgrade process and service namespace change in the first combined release. Never recommend deleting an ownership journal as an upgrade shortcut.
