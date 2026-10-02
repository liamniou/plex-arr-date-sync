#!/usr/bin/env python3
"""Sync Plex "Added At" dates with Radarr/Sonarr added dates.

Why: when Radarr/Sonarr upgrades a file, Plex sees a "new" file and bumps the
item's addedAt to now, so upgraded content wrongly shows up in "Recently Added".

This tool rewrites (and locks) Plex addedAt so it reflects a stable date:
  * Movies      -> Radarr movie "added" date
  * TV shows    -> Sonarr series "added" date
  * TV seasons  -> Sonarr series "added" date
  * TV episodes -> the episode's original air date (originallyAvailableAt)

Matching is done by external GUID (tmdb/imdb/tvdb) first, then by file path.
Locking the addedAt field prevents future library scans / upgrades from
overwriting it again.
"""

from __future__ import annotations

import os
import sys
import logging
from datetime import datetime, timezone

import requests
from plexapi.server import PlexServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("plex-arr-date-sync")


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return val


PLEX_URL = env("PLEX_URL", required=True)
PLEX_TOKEN = env("PLEX_TOKEN", required=True)
PLEX_MOVIE_LIBRARY = env("PLEX_MOVIE_LIBRARY", "Films")
PLEX_TV_LIBRARY = env("PLEX_TV_LIBRARY", "TV")

RADARR_URL = (env("RADARR_URL") or "").rstrip("/")
RADARR_API_KEY = env("RADARR_API_KEY")
SONARR_URL = (env("SONARR_URL") or "").rstrip("/")
SONARR_API_KEY = env("SONARR_API_KEY")

DRY_RUN = (env("DRY_RUN", "false") or "false").lower() in ("1", "true", "yes")
# Only rewrite when the existing date differs from the target by more than this
# many seconds, to avoid pointless writes.
TOLERANCE_SECONDS = int(env("TOLERANCE_SECONDS", "86400"))

HTTP_TIMEOUT = 60


def parse_arr_date(value: str | None) -> datetime | None:
    """Parse an *arr ISO8601 timestamp into an aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Radarr/Sonarr use 0001-01-01 as a "never" sentinel.
    if dt.year < 1900:
        return None
    return dt


def to_epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def current_added_epoch(item) -> int | None:
    if not getattr(item, "addedAt", None):
        return None
    return int(item.addedAt.timestamp())


def set_added_at(item, target_epoch: int, label: str) -> bool:
    """Lock the item's addedAt to target_epoch. Returns True if changed."""
    current = current_added_epoch(item)
    if current is not None and abs(current - target_epoch) <= TOLERANCE_SECONDS:
        return False

    new_str = datetime.fromtimestamp(target_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
    if DRY_RUN:
        log.info("[dry-run] would set %s addedAt -> %s", label, new_str)
        return True

    try:
        item.edit(**{"addedAt.value": target_epoch, "addedAt.locked": 1})
        log.info("set %s addedAt -> %s", label, new_str)
        return True
    except Exception as exc:  # noqa: BLE001 - keep going on per-item errors
        log.error("failed to set %s addedAt: %s", label, exc)
        return False


def arr_get(base_url: str, api_key: str, path: str):
    resp = requests.get(
        f"{base_url}{path}",
        headers={"X-Api-Key": api_key},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def guid_keys(item) -> list[str]:
    """Return normalized external guids for a Plex item, e.g. 'tmdb:603'."""
    keys: list[str] = []
    for g in getattr(item, "guids", []) or []:
        gid = g.id  # e.g. 'imdb://tt0133093', 'tmdb://603', 'tvdb://1234'
        if "://" not in gid:
            continue
        provider, value = gid.split("://", 1)
        keys.append(f"{provider.lower()}:{value}")
    return keys


def item_file_paths(item) -> list[str]:
    paths: list[str] = []
    for media in getattr(item, "media", []) or []:
        for part in getattr(media, "parts", []) or []:
            if part.file:
                paths.append(part.file)
    return paths


def sync_movies(plex: PlexServer) -> None:
    if not (RADARR_URL and RADARR_API_KEY):
        log.info("Radarr not configured, skipping movies")
        return

    log.info("Fetching movies from Radarr (%s)", RADARR_URL)
    movies = arr_get(RADARR_URL, RADARR_API_KEY, "/api/v3/movie")
    log.info("Radarr returned %d movies", len(movies))

    by_guid: dict[str, dict] = {}
    by_path: dict[str, dict] = {}
    by_basename: dict[str, dict] = {}
    for m in movies:
        added = parse_arr_date(m.get("added"))
        if not added:
            continue
        rec = {"added": added, "title": m.get("title")}
        if m.get("tmdbId"):
            by_guid[f"tmdb:{m['tmdbId']}"] = rec
        if m.get("imdbId"):
            by_guid[f"imdb:{m['imdbId']}"] = rec
        mf = m.get("movieFile") or {}
        if mf.get("path"):
            by_path[mf["path"]] = rec
            by_basename[os.path.basename(mf["path"])] = rec

    try:
        section = plex.library.section(PLEX_MOVIE_LIBRARY)
    except Exception as exc:  # noqa: BLE001
        log.error("Plex movie library '%s' not found: %s", PLEX_MOVIE_LIBRARY, exc)
        return

    changed = matched = 0
    for movie in section.all():
        rec = None
        for key in guid_keys(movie):
            if key in by_guid:
                rec = by_guid[key]
                break
        if rec is None:
            for p in item_file_paths(movie):
                rec = by_path.get(p) or by_basename.get(os.path.basename(p))
                if rec:
                    break
        if rec is None:
            continue
        matched += 1
        if set_added_at(movie, to_epoch(rec["added"]), f"movie '{movie.title}'"):
            changed += 1

    log.info("Movies: matched=%d changed=%d", matched, changed)


def sync_series(plex: PlexServer) -> None:
    if not (SONARR_URL and SONARR_API_KEY):
        log.info("Sonarr not configured, skipping series")
        return

    log.info("Fetching series from Sonarr (%s)", SONARR_URL)
    series = arr_get(SONARR_URL, SONARR_API_KEY, "/api/v3/series")
    log.info("Sonarr returned %d series", len(series))

    by_guid: dict[str, dict] = {}
    by_path: dict[str, dict] = {}
    for s in series:
        added = parse_arr_date(s.get("added"))
        if not added:
            continue
        rec = {"added": added, "title": s.get("title")}
        if s.get("tvdbId"):
            by_guid[f"tvdb:{s['tvdbId']}"] = rec
        if s.get("tmdbId"):
            by_guid[f"tmdb:{s['tmdbId']}"] = rec
        if s.get("imdbId"):
            by_guid[f"imdb:{s['imdbId']}"] = rec
        if s.get("path"):
            by_path[s["path"]] = rec

    try:
        section = plex.library.section(PLEX_TV_LIBRARY)
    except Exception as exc:  # noqa: BLE001
        log.error("Plex TV library '%s' not found: %s", PLEX_TV_LIBRARY, exc)
        return

    matched = show_changed = season_changed = ep_changed = ep_skipped = 0
    for show in section.all():
        rec = None
        for key in guid_keys(show):
            if key in by_guid:
                rec = by_guid[key]
                break
        if rec is None:
            loc = getattr(show, "locations", []) or []
            for path in loc:
                if path in by_path:
                    rec = by_path[path]
                    break
        if rec is None:
            continue
        matched += 1
        series_epoch = to_epoch(rec["added"])

        if set_added_at(show, series_epoch, f"show '{show.title}'"):
            show_changed += 1

        for season in show.seasons():
            if set_added_at(season, series_epoch, f"{show.title} S{season.seasonNumber:02d}"):
                season_changed += 1

        for episode in show.episodes():
            air = getattr(episode, "originallyAvailableAt", None)
            if not air:
                ep_skipped += 1
                continue
            label = f"{show.title} S{(episode.seasonNumber or 0):02d}E{(episode.episodeNumber or 0):02d}"
            if set_added_at(episode, to_epoch(air), label):
                ep_changed += 1

    log.info(
        "Series: matched=%d shows_changed=%d seasons_changed=%d episodes_changed=%d episodes_skipped=%d",
        matched, show_changed, season_changed, ep_changed, ep_skipped,
    )


def main() -> None:
    log.info("Connecting to Plex at %s (dry_run=%s)", PLEX_URL, DRY_RUN)
    try:
        plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not connect to Plex: %s", exc)
        sys.exit(1)

    sync_movies(plex)
    sync_series(plex)
    log.info("Done")


if __name__ == "__main__":
    main()
