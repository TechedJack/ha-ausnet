DOMAIN = "ausnet_myhomeenergy"

# External statistic ID templates for the Energy dashboard.
# HA requires the format "{domain}:{unique_id}" for external statistics.
STAT_ID_IMPORT = "ausnet_myhomeenergy:ausnet_{nmi}_energy_import"
STAT_ID_EXPORT = "ausnet_myhomeenergy:ausnet_{nmi}_energy_export"
FRIENDLY_IMPORT = "AusNet {nmi} Energy Import"
FRIENDLY_EXPORT = "AusNet {nmi} Energy Export"

# Config-entry keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_NMI = "nmi"
CONF_SESSION_COOKIE = "session_cookie"

# How many days of history to backfill on initial setup
CONF_HISTORY_DAYS = "history_days"
DEFAULT_HISTORY_DAYS = 90

# Update interval (hours) – data is uploaded to the portal with ~1 day lag
UPDATE_INTERVAL_HOURS = 6
