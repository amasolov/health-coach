# Health Tracker Addon

Syncs health and fitness data from Garmin Connect and Hevy
into a TimescaleDB database, calculates performance metrics (CTL/ATL/TSB),
and provisions Grafana dashboards for visualization.

## Setup

1. Install the TimescaleDB and Grafana addons on your Home Assistant instance
2. Create a database called `health` in TimescaleDB
3. Configure this addon with your database credentials and API keys
4. (Optional) Generate a Grafana service account API key for dashboard provisioning

## Multi-User

Add additional users in the addon configuration. Each user needs their own
Garmin Connect and/or Hevy credentials. Data is isolated per user in the
database and filterable via the Grafana user selector dropdown.
