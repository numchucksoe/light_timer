"""Constants for the Light Timer integration.

Centralises the integration domain, the option keys used in the config entry
``options`` payload (see the design's "Config Entry Shape"), the default values
applied when a managed light is added through the options flow, the valid range
shared by the duration/suspension validators, and the list of entity platforms
the integration forwards setup to.
"""

from __future__ import annotations

from typing import Final

# Integration domain (matches manifest.json "domain"). (Req 8.1)
DOMAIN: Final = "light_timer"

# --- Option keys ----------------------------------------------------------
# Top-level keys in entry.options.
CONF_LIGHTS: Final = "lights"
CONF_GLOBALLY_ENABLED: Final = "globally_enabled"

# Per-managed-light keys (entries inside the "lights" list).
CONF_LIGHT_ENTITY_ID: Final = "light_entity_id"
CONF_TIMER_DURATION: Final = "timer_duration"
CONF_DEFAULT_SUSPENSION: Final = "default_suspension"
CONF_NOTIFICATION_SERVICE: Final = "notification_service"
CONF_ENABLED: Final = "enabled"

# --- Default values -------------------------------------------------------
# Defaults shown/applied when adding a managed light. (Req 9.2)
DEFAULT_TIMER_DURATION: Final = 300
DEFAULT_SUSPENSION: Final = 3600
DEFAULT_ENABLED: Final = True
DEFAULT_GLOBALLY_ENABLED: Final = True

# --- Validation bounds ----------------------------------------------------
# Shared inclusive range for duration and suspension inputs (integers only).
MIN_DURATION_SECONDS: Final = 1
MAX_DURATION_SECONDS: Final = 86400

# --- Platforms ------------------------------------------------------------
# Entity platforms the integration forwards config entry setup to.
PLATFORMS: Final = ["sensor", "switch", "button", "number"]
