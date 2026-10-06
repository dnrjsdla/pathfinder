"""Senses: one file each, <= 150 lines, adapters outside the core budget (blueprint §6).
Each exports OPTIONS, TRUST, CONDITIONS, KINDS and either `poll(options, now)` (with
CADENCE_S) or `build(fields, now)` for a push sense - see `signals.load`."""
