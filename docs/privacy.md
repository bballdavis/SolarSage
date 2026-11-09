# Privacy and diagnostic data

SolarSage runs its planner inside Home Assistant. It reads the entities you configure and uses recorder history for household demand. Tesla commands use your configured Tesla integration; Rivian schedule control communicates with Rivian through the Rivian client library. Solcast forecasts come from your separately configured Solcast integration. These services have their own accounts and privacy policies.

## Stored locally

- Planner mappings, settings, model memory, command state and spending estimates are stored in Home Assistant configuration storage.
- Rivian session tokens are stored in the config entry. The password is used during authentication and is not stored by SolarSage.
- The Rivian ownership journal contains the original schedules and can contain their locations. Keep it private and backed up until ownership is safely released.
- Data tools write household snapshots, forecast records and replay output under ignored `tools/out/`. Local connection and mapping files are ignored too.

## Before sharing

Diagnostics redact known sensitive keys, but entity IDs, device labels, custom strings, logs and screenshots can still identify a household. Review every attachment. Do not publish Home Assistant backups, `.storage`, raw API responses, private keys, access tokens, location pins, or tool recordings.

The public example configuration uses generic vehicle names and example hosts. Existing users retain their locally stored mappings; examples do not rename your vehicles or entities.

## Repository publication

A clean current checkout does not remove data from older commits, branches, releases or caches. Before changing repository visibility, inspect the full Git history and any release assets. If credentials were ever committed, revoke/rotate them; deleting a file alone is insufficient. History rewriting should be coordinated separately with collaborators.
