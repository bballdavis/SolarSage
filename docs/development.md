# Development

## Repository map

| Path | Purpose |
| --- | --- |
| `custom_components/solar_ev_planner/` | The single installable Home Assistant integration |
| `custom_components/solar_ev_planner/core/` | Pure Python forecasting and planning |
| `custom_components/solar_ev_planner/rivian/` | Bundled Rivian schedule control, API adapter and ownership journal |
| `custom_components/solar_ev_planner/www/` | Dashboard card and SolarSage logo |
| `docs/` | Documentation source and public images |
| `website/` | Docusaurus configuration, theme and locked dependencies |
| `tools/` | Local checks, snapshots, replay and attended tests |
| `tests/` | Home Assistant, planner, controller and tool tests against fakes |

## Python checks

Use Python 3.14. From the repository root:

```sh
uv venv --python 3.14
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/ruff check custom_components tests tools scripts
.venv/bin/ruff format --check custom_components tests tools scripts
.venv/bin/python -m pytest tests -q
```

The suite uses simulated vehicles and blocks external network connections. Do not change tests to contact a real car. Add regression tests when changing planning rules, authentication, ownership, restoration or command execution.

## Documentation

Use Node.js 22 or later:

```sh
cd website
npm ci
npm start
npm run build
```

Edit Markdown in `docs/`. Docusaurus reads it directly; there is no generated copy to keep synchronized. Use `website/sidebars.js` when adding a guide. Production builds fail on broken internal links. Markdown is parsed as Markdown, so ordinary configuration examples do not need MDX escaping. Mermaid diagrams are supported.

GitHub Pages builds the same content with [the Pages workflow](https://github.com/bballdavis/SolarSage/blob/main/.github/workflows/docs.yml) on pushes to `main`. Pull requests build without deploying.

## Compatibility

Keep the internal domain `solar_ev_planner`, existing unique IDs, journal keys, card tag and websocket routes stable. Public branding does not require a domain migration. Rivian entries are distinguished by `vehicle_id` in entry data; planner entries have site/car subentries. Platform dispatch and dashboard lookup must handle both entry types.

Before opening a pull request, describe the behavior change and relevant validation, and remove local recordings, tokens and identifiable screenshots. See [privacy](privacy.md).
