# HASS Solar Rivian

This Apache-2.0 fork of Rivian (Unofficial) adds conservative solar-surplus
charging for a Rivian or Tesla on a shared Wall Connector.

New installations start in shadow mode. Rivian charging requires an unplugged,
transactional compatibility probe that restores the complete original schedule
list before the controller is allowed to write a renewable charging lease.

See the repository README for prerequisites, entity mappings, safety boundaries,
configuration, recovery behavior, and known limitations.
