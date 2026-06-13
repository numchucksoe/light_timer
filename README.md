# Light Timer

A Home Assistant HACS custom integration that automatically turns managed lights
and switches off after a configurable countdown.

A single integration instance manages any number of light and switch entities,
each with its own timer duration, default suspension period, optional
notification service, and enable/disable state. All configuration is done
through the Home Assistant UI — no YAML editing required.

## Features

- Supports both `light.*` and `switch.*` entities
- Per-entity countdown timers with automatic shutoff
- Domain-appropriate service calls (`light.turn_off` / `switch.turn_off`)
- Retry-based shutoff with failure indication and optional notifications
- Live remaining-time sensor on the Overview dashboard (`H:MM:SS` / `M:SS` / `:SS`)
- Timer duration Number entity per managed entity for on-the-fly adjustment from the device page
- Cancel and suspend controls per entity
- Per-entity enable/disable plus a global master switch
- Restart recovery that re-arms timers for entities that are still on
- UI-driven add / edit / remove of managed entities
- Backward compatible — existing light-only configurations continue to work without migration

## Installation (HACS)

1. Add this repository as a custom repository in HACS (category: Integration).
2. Install the **Light Timer** integration through HACS.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   **Light Timer**.

## Configuration

Add managed entities (lights or switches) through the integration's options
flow. For each entity you can set:

- **Timer duration** (seconds, 1–86400, default 300)
- **Default suspension duration** (seconds, 1–86400, default 3600)
- **Notification service** (optional)
- **Enabled** (default true)

The entity selector shows both light and switch entities. The integration
validates that the selected entity's domain is supported before accepting it.

## Entities per Managed Light/Switch

| Entity | Type | Description |
|--------|------|-------------|
| Timer remaining | Sensor | Displays the countdown as a human-readable string (`1:01:01`, `5:00`, `:45`, or `0:00`) |
| Timer duration | Number | Adjust the timer duration (1–86400 s) directly from the device page — changes take effect on the next timer start |
| Enable | Switch | Enable or disable the timer for this entity |
| Cancel | Button | Cancel the currently running countdown |

The **Timer remaining** sensor reports a plain string (no `device_class` or
`unit_of_measurement`), so Home Assistant will not attempt numeric unit
conversion or record it in long-term statistics.

The **Timer duration** number entity persists changes immediately to both memory
and config entry options — no reload or restart needed. A running countdown is
not affected; the new duration applies on the next timer start.

## Supported Domains

| Domain | Service called on expiry |
|--------|--------------------------|
| `light` | `light.turn_off` |
| `switch` | `switch.turn_off` |

Adding support for additional domains in the future requires only appending to
the `SUPPORTED_DOMAINS` tuple in `const.py`.

## Contributers
- Charlie Buchanan (charlie@buchananfamily.org)
