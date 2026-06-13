# Changelog

All notable changes to the Light Timer integration will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.4] - 2026-06-13

### Added

- Support for `switch.*` entities alongside `light.*` entities
- `SUPPORTED_DOMAINS` constant in `const.py` for centralized domain configuration
- Entity selector now shows both light and switch entities in the options flow
- Domain-appropriate service dispatch (`switch.turn_off` for switches, `light.turn_off` for lights)
- Validation error `"not_supported_domain"` for entities outside supported domains
- Property-based tests (Hypothesis) for 8 correctness properties
- Integration tests for backward compatibility and end-to-end switch entity flow
- MIT license
- CHANGELOG.md

### Changed

- Config flow validation uses `SUPPORTED_DOMAINS` membership check instead of hardcoded `"light."` prefix
- Coordinator derives turn-off service domain dynamically from entity ID instead of hardcoded `_LIGHT_DOMAIN`
- Notification messages use domain-neutral phrasing ("failed to turn off" without domain-specific nouns)
- README updated to reflect switch entity support

### Backward Compatibility

- Existing config entries with only `light.*` entities load without migration
- The `light_entity_id` config key is preserved (used for both light and switch entities)
- Helper entity unique IDs remain in `{entry_id}_{entity_id}_{suffix}` format
- Service names (`light_timer.cancel`, `light_timer.suspend`) are unchanged

## [1.0.3] - 2026-06-13

### Added

- Number platform with `LightTimerDurationNumber` entity for on-the-fly timer duration adjustment
- Cascading time display format for remaining-time sensor (`H:MM:SS` / `M:SS` / `:SS`)

### Changed

- Timer remaining sensor reports formatted time string instead of numeric seconds

## [1.0.2] - 2026-06-10

### Added

- Clean up stale device registry entries when a managed entity is removed

### Fixed

- Always clean up device registry associations on entity removal (previously could leave orphaned devices)

## [1.0.1] - 2026-06-10

### Added

- Clean up stale entity registry entries on config reload
- Simplified entity cleanup logic with clearer unique_id matching

## [1.0.0] - 2026-06-08

### Added

- Initial release
- Per-light countdown timers with automatic shutoff
- Retry-based shutoff with failure indication and optional notifications
- Live remaining-time sensor entity per managed light
- Cancel and suspend button entities per managed light
- Per-light enable/disable switch entity plus global master switch
- Restart recovery that re-arms timers for lights still on after HA restart
- UI-driven configuration flow (add / edit / remove managed lights)
- Configurable timer duration (1–86400 seconds)
- Configurable default suspension duration (1–86400 seconds)
- Optional notification service per managed light
- Single integration instance managing multiple lights
- HACS-compatible custom component structure
