# AusNet myHomeEnergy – Home Assistant Integration

Backfill your **AusNet smart meter** energy data into Home Assistant's Energy dashboard by importing a NEM12 CSV file downloaded from [myhomeenergy.ausnet.com.au](https://myhomeenergy.ausnet.com.au).

> **Note:** This integration currently requires a manual CSV download. Automatic retrieval from the myhomeenergy portal is on the roadmap (see [TODO](#todo)).

## Supported data

| Channel | Description |
|---------|-------------|
| E1 | Grid import (electricity consumed from the grid) |
| E2 | Grid export (solar feed-in) |

Interval lengths of **15, 30, and 60 minutes** are all supported.

## Installation

### HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add this repository URL and select category **Integration**.
3. Search for **AusNet myHomeEnergy** and install it.
4. Restart Home Assistant.

### Manual

1. Copy the `custom_components/ausnet_myhomeenergy` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

## Getting your NEM12 file

1. Log in to [myhomeenergy.ausnet.com.au](https://myhomeenergy.ausnet.com.au).
2. Navigate to **My Data** → **Download** and select **NEM12** format.
3. Choose your desired date range and download the CSV.
4. Copy the file to your HA host, e.g. `/config/www/ausnet_nem12.csv`.

## Usage

Call the service **`ausnet_myhomeenergy.import_csv`** from **Developer Tools → Services** or an automation:

```yaml
service: ausnet_myhomeenergy.import_csv
data:
  file_path: /config/www/ausnet_nem12.csv
  timezone: Australia/Melbourne   # see below for other states
  channel: E1                     # E1 = import, E2 = export (solar feed-in)
  # nmi_override: "6123456789"   # optional – only needed if the NMI in the CSV is wrong
```

### Timezone by state

| State / Territory | Timezone string |
|-------------------|-----------------|
| VIC, NSW, TAS, ACT | `Australia/Melbourne` or `Australia/Sydney` |
| QLD | `Australia/Brisbane` |
| SA | `Australia/Adelaide` |
| WA | `Australia/Perth` |
| NT | `Australia/Darwin` |

### Importing solar feed-in (E2)

Run the service a second time with `channel: E2` to also import your export data.

### Statistics ID

The integration writes external statistics with the following IDs:

| Channel | Statistic ID |
|---------|-------------|
| E1 | `ausnet_myhomeenergy:ausnet_<NMI>_energy_import` |
| E2 | `ausnet_myhomeenergy:ausnet_<NMI>_energy_export` |

Add these to your Energy dashboard under **Settings → Energy**.

## Configuration reference

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `file_path` | ✅ | — | Absolute path to the NEM12 CSV on the HA host |
| `timezone` | ❌ | `Australia/Melbourne` | IANA timezone for the meter's local time |
| `channel` | ❌ | `E1` | `E1` (import) or `E2` (export/solar) |
| `nmi_override` | ❌ | *(from CSV)* | Override the NMI detected in the CSV |

## TODO

- [ ] **Automatic data retrieval from myhomeenergy.ausnet.com.au** — add a config flow to store credentials and download the NEM12 file automatically, removing the need for a manual download.
- [ ] Scheduled polling to keep statistics up to date.
- [ ] Support for additional NEM12 channels beyond E1/E2.

## Troubleshooting

- Check **Settings → System → Logs** and filter for `ausnet_myhomeenergy` to see import progress and any errors.
- The statistics may take a few minutes to appear in the Energy dashboard after import.
- If you re-import the same date range, existing statistics are overwritten with the new values.

## Contributing

Pull requests are welcome. Please open an issue first to discuss significant changes.

## Licence

MIT
