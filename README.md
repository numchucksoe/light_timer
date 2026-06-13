# Light Timer

A Home Assistant HACS custom integration that automatically turns managed lights
off after a configurable countdown.

A single integration instance manages any number of lights, each with its own
timer duration, default suspension period, optional notification service, and
enable/disable state. All configuration is done through the Home Assistant UI —
no YAML editing required.

## Features

- Per-light countdown timers with automatic shutoff
- Retry-based shutoff with failure indication and optional notifications
- Live remaining-time sensor on the Overview dashboard (`H:MM:SS` / `M:SS` / `:SS`)
- Timer duration Number entity per light for on-the-fly adjustment from the device page
- Cancel and suspend controls per light
- Per-light enable/disable plus a global master switch
- Restart recovery that re-arms timers for lights that are still on
- UI-driven add / edit / remove of managed lights

## Installation (HACS)

1. Add this repository as a custom repository in HACS (category: Integration).
2. Install the **Light Timer** integration through HACS.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   **Light Timer**.

## Configuration

Add managed lights through the integration's options flow. For each light you
can set:

- **Timer duration** (seconds, 1–86400, default 300)
- **Default suspension duration** (seconds, 1–86400, default 3600)
- **Notification service** (optional)
- **Enabled** (default true)

## Entities per Managed Light

| Entity | Type | Description |
|--------|------|-------------|
| Timer remaining | Sensor | Displays the countdown as a human-readable string (`1:01:01`, `5:00`, `:45`, or `0:00`) |
| Timer duration | Number | Adjust the timer duration (1–86400 s) directly from the device page — changes take effect on the next timer start |
| Enable | Switch | Enable or disable the timer for this light |
| Cancel | Button | Cancel the currently running countdown |

The **Timer remaining** sensor reports a plain string (no `device_class` or
`unit_of_measurement`), so Home Assistant will not attempt numeric unit
conversion or record it in long-term statistics.

The **Timer duration** number entity persists changes immediately to both memory
and config entry options — no reload or restart needed. A running countdown is
not affected; the new duration applies on the next timer start.

## Contributers
- Charlie Buchanan (charlie@buchananfamily.org)
