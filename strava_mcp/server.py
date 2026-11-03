"""Strava MCP server — exposes Strava API endpoints as tools for the coach agent.

Run as a subprocess via stdio transport; do not add any print() calls here
as stdout is reserved for the MCP protocol.
"""
import concurrent.futures
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.mcpserver import MCPServer, Context

from strava_mcp.strava_client import StravaClient

# Send logs to stderr only — stdout is the MCP protocol channel
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

mcp = MCPServer("strava-coach")
_client: StravaClient | None = None

# Fields kept from a detailed activity response.  Strava returns many fields
# that are irrelevant for coaching (map polylines, segment efforts, gear IDs,
# social counts, etc.) and can push a single activity past the tool-result
# truncation limit.  Keeping only coaching-relevant fields reduces a typical
# detailed activity from ~15 000 chars to ~2 000 chars.
_ACTIVITY_KEEP = {
    "id", "name", "type", "sport_type", "workout_type",
    "start_date", "start_date_local",
    "distance", "moving_time", "elapsed_time", "total_elevation_gain",
    "average_speed", "max_speed",
    "average_heartrate", "max_heartrate", "average_cadence",
    "suffer_score", "perceived_exertion", "calories", "description",
    "splits_metric",   # per-km pace/HR — primary coaching data
    "best_efforts",    # PRs at standard distances
    "pr_count", "achievement_count",
    "gear",
}

# Subset for summary-level activities from list_activities.
# Drops athlete objects, map polylines, latlng, upload IDs, social counts.
_ACTIVITY_SUMMARY_KEEP = {
    "id", "name", "type", "sport_type", "workout_type",
    "start_date", "start_date_local",
    "distance", "moving_time", "total_elevation_gain",
    "average_speed", "average_heartrate", "max_heartrate",
    "average_cadence", "suffer_score", "pr_count",
}


_WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Rain showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}


def _fetch_weather(lat: float, lng: float, start_date_local: str) -> dict | None:
    """Query Open-Meteo historical archive for weather at a run's start time/location.

    Returns a compact dict or None if the location is missing or the call fails.
    """
    try:
        dt = datetime.strptime(start_date_local[:19], "%Y-%m-%dT%H:%M:%S")
        date_str = dt.strftime("%Y-%m-%d")
        hour = dt.hour

        resp = httpx.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat,
                "longitude": lng,
                "start_date": date_str,
                "end_date": date_str,
                "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation,weather_code",
                "timezone": "auto",
                "wind_speed_unit": "kmh",
            },
            timeout=10,
        )
        resp.raise_for_status()
        hourly = resp.json().get("hourly", {})

        return {
            "temperature_c": hourly.get("temperature_2m", [None])[hour],
            "humidity_pct": hourly.get("relative_humidity_2m", [None])[hour],
            "wind_speed_kmh": hourly.get("wind_speed_10m", [None])[hour],
            "precipitation_mm": hourly.get("precipitation", [None])[hour],
            "conditions": _WMO_CODES.get(
                (hourly.get("weather_code") or [None])[hour], "Unknown"
            ),
        }
    except Exception:
        return None


# Fields kept per best_effort entry inside a detailed activity.
# Drops nested activity/athlete objects, redundant timestamps, resource_state.
_BEST_EFFORT_KEEP = {"name", "elapsed_time", "distance", "pr_rank"}


def get_client() -> StravaClient:
    global _client
    if _client is None:
        _client = StravaClient()
    return _client


@mcp.tool()
def get_current_timestamp() -> str:
    """Return the current Unix timestamp and human-readable UTC date.
    Always call this before computing date ranges for list_activities."""
    now = int(time.time())
    dt = datetime.utcnow()
    return json.dumps({
        "unix_timestamp": now,
        "current_date_utc": dt.strftime("%Y-%m-%d"),
        "current_year": dt.year,
    })


@mcp.tool()
def get_athlete() -> str:
    """Get the authenticated athlete's profile: name, location, weight, FTP, sex,
    measurement preference (metric/imperial), and follower counts."""
    return json.dumps(get_client().get_athlete())


@mcp.tool()
def get_athlete_stats() -> str:
    """Get the athlete's overall training statistics including:
    - recent_run_totals: distance, moving_time, elevation_gain for the last 4 weeks
    - ytd_run_totals: year-to-date totals
    - all_run_totals: lifetime totals
    All distances are in metres, times in seconds."""
    athlete = get_client().get_athlete()
    return json.dumps(get_client().get_athlete_stats(athlete["id"]))


@mcp.tool()
def get_athlete_zones() -> str:
    """Get the athlete's configured heart rate and power zones.
    Heart rate zones are defined by min/max bpm boundaries.
    Useful for zone-based training analysis."""
    return json.dumps(get_client().get_athlete_zones())


@mcp.tool()
def list_activities(
    per_page: int = 10,
    page: int = 1,
    after_timestamp: Optional[int] = None,
    before_timestamp: Optional[int] = None,
) -> str:
    """List the athlete's activities (summary level).

    Always call get_current_timestamp() first to get the current date and Unix time.
    Use the returned unix_timestamp as the anchor for all date-range calculations.
    For queries about the user's "latest", "current", or "today's" run, anchor
    after_timestamp to the current year to avoid returning last year's data.
    For trend or pattern analysis, use whatever date range the question requires.

    Each activity includes: id, name, type, sport_type, workout_type, start_date,
    start_date_local, distance (metres), moving_time (seconds),
    total_elevation_gain (metres), average_speed (m/s), average_heartrate,
    max_heartrate, average_cadence, suffer_score, pr_count.

    Use after_timestamp / before_timestamp (Unix epoch integers) to filter by date range.
    Use page to paginate results. per_page max is 200. Default is 10 to keep
    response size manageable; increase only when a broader history is needed.

    To get runs from the last 30 days, compute after_timestamp as (current Unix time - 2592000).
    """
    activities = get_client().list_activities(
        before=before_timestamp,
        after=after_timestamp,
        per_page=per_page,
        page=page,
    )
    filtered = [
        {k: v for k, v in a.items() if k in _ACTIVITY_SUMMARY_KEEP}
        for a in activities
    ]

    def _enrich(pair):
        raw, filt = pair
        latlng = raw.get("start_latlng")
        date_local = raw.get("start_date_local")
        if latlng and len(latlng) == 2 and date_local:
            weather = _fetch_weather(latlng[0], latlng[1], date_local)
            if weather:
                return {**filt, "weather": weather}
        return filt

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        enriched = list(pool.map(_enrich, zip(activities, filtered)))

    return json.dumps(enriched)


@mcp.tool()
def get_activity(activity_id: int) -> str:
    """Get coaching-relevant details of a specific activity by its Strava ID.

    Returns everything in list_activities PLUS: description, calories, perceived_exertion,
    splits_metric (per-km splits with pace and HR), best_efforts (PRs at standard distances),
    gear (shoe/bike used).

    Noisy fields (map polylines, segment efforts, splits_imperial, social counts) are
    stripped to keep the response compact. Use get_activity_laps for full lap data.

    Use this after list_activities to drill into a specific run for detailed analysis."""
    data = get_client().get_activity(activity_id)
    filtered = {k: v for k, v in data.items() if k in _ACTIVITY_KEEP}

    if filtered.get("best_efforts"):
        filtered["best_efforts"] = [
            {k: v for k, v in effort.items() if k in _BEST_EFFORT_KEEP}
            for effort in filtered["best_efforts"]
            if effort is not None and effort.get("pr_rank") is not None
        ]

    latlng = data.get("start_latlng")
    date_local = data.get("start_date_local")
    if latlng and len(latlng) == 2 and date_local:
        weather = _fetch_weather(latlng[0], latlng[1], date_local)
        if weather:
            filtered["weather"] = weather

    return json.dumps(filtered)


@mcp.tool()
def get_activity_laps(activity_id: int) -> str:
    """Get lap-by-lap data for an activity.

    Each lap includes: lap_index, distance, moving_time, elapsed_time, average_speed,
    max_speed, average_heartrate, max_heartrate, average_cadence, total_elevation_gain.

    Essential for analysing interval workouts, tempo runs, and paced efforts.
    Distances in metres, times in seconds, speed in m/s."""
    return json.dumps(get_client().get_activity_laps(activity_id))


@mcp.tool()
def get_activity_zones(activity_id: int) -> str:
    """Get heart rate and pace zone distribution for a specific activity.

    Shows what percentage of the run was spent in each HR/pace zone.
    Useful for determining aerobic vs anaerobic training load.

    Note: requires Strava Premium and a heart rate monitor. Returns an error dict
    if zone data is unavailable."""
    try:
        return json.dumps(get_client().get_activity_zones(activity_id))
    except Exception as e:
        return json.dumps(
            {
                "error": str(e),
                "note": "Zone data requires Strava Premium and heart rate data for this activity.",
            }
        )

@mcp.tool()
def get_gears(gear_id: int) -> str:
    """Get information about a specific piece of gear.

    Shows details about the gear, including brand, model, and usage statistics.
    Useful for determining gear performance and maintenance needs."""
    try:
        return json.dumps(get_client().get_gears(gear_id))
    except Exception as e:
        return json.dumps(
            {
                "error": str(e),
                "note": "Failed to retrieve gear information.",
            }
        )

if __name__ == "__main__":
    mcp.run()
