DOMAIN = "ausnet_myhomeenergy"

# External statistic ID templates for the Energy dashboard.
# HA requires the format "{domain}:{unique_id}" for external statistics.
STAT_ID_IMPORT = "ausnet_myhomeenergy:ausnet_{nmi}_energy_import"
STAT_ID_EXPORT = "ausnet_myhomeenergy:ausnet_{nmi}_energy_export"
FRIENDLY_IMPORT = "AusNet {nmi} Energy Import"
FRIENDLY_EXPORT = "AusNet {nmi} Energy Export"
