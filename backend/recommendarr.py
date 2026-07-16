from __future__ import annotations
import argparse
import re
import sqlite3
import sys
import time
import traceback as traceback_module
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests
from plexapi.server import PlexServer
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import yaml


@dataclass(frozen=True)
class Settings:
    plex_url: str
    plex_token: str
    plex_library: str
    tmdb_credential: str
    days_back: int
    collection_name: str
    export_location: str
    export_file: str
    top_n: int
    database_file: str = "recommendarr.db"
    schedule: str | None = None

    @classmethod
    def from_yaml(cls, config_file):
        path = Path(config_file)
        if not path.is_file():
            raise FileNotFoundError(
                f"Configuration file not found: {path}. "
                "Copy config.yaml.example to config.yaml and fill in your credentials."
            )
        try:
            with path.open(encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}
        except yaml.YAMLError as error:
            raise ValueError(f"Invalid YAML in {path}: {error}") from error

        if not isinstance(config, dict):
            raise ValueError(f"Configuration in {path} must be a YAML mapping.")
        plex = config.get("plex", {})
        tmdb = config.get("tmdb", {})
        recommendations = config.get("recommendations", {})
        database = config.get("database", {})
        if not all(isinstance(section, dict) for section in (plex, tmdb, recommendations, database)):
            raise ValueError("The plex, tmdb, recommendations, and database sections must be mappings.")

        schedule = config.get("schedule")
        if schedule is not None and not isinstance(schedule, str):
            raise ValueError("schedule must be a string such as '05:00|daily'.")

        settings = cls(
            plex_url=str(plex.get("url", "")).strip(),
            plex_token=str(plex.get("token", "")).strip(),
            plex_library=str(plex.get("library", "Movies")).strip(),
            tmdb_credential=str(
                tmdb.get("read_access_token") or tmdb.get("api_key") or ""
            ).strip(),
            days_back=int(recommendations.get("days_to_look_back", 30)),
            collection_name=str(
                recommendations.get("collection_name", "recommendarr: Recommended Right Now")
            ).strip(),
            export_location=str(recommendations.get("export_location", ".")).strip(),
            export_file=str(
                recommendations.get("export_file", "recommendarr-recommendations.txt")
            ).strip(),
            top_n=int(recommendations.get("top_n", 50)),
            database_file=str(database.get("file", "recommendarr.db")).strip(),
            schedule=schedule.strip() if schedule else None,
        )
        missing = [
            name
            for name, value in (
                ("plex.url", settings.plex_url),
                ("plex.token", settings.plex_token),
                ("tmdb.api_key or tmdb.read_access_token", settings.tmdb_credential),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required configuration values: {', '.join(missing)}")
        if settings.days_back < 1:
            raise ValueError("recommendations.days_to_look_back must be at least 1.")
        if settings.top_n < 1:
            raise ValueError("recommendations.top_n must be at least 1.")
        if not settings.export_file:
            raise ValueError("recommendations.export_file cannot be empty.")
        if not settings.export_location:
            raise ValueError("recommendations.export_location cannot be empty.")
        return settings


@dataclass(frozen=True)
class Schedule:
    hour: int
    minute: int
    frequency: str
    weekday: int | None = None

    WEEKDAYS = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    @classmethod
    def parse(cls, value):
        match = re.fullmatch(
            r"(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)\|"
            r"(?P<frequency>daily|weekly\((?P<weekday>[a-zA-Z]+)\))",
            value.strip(),
        )
        if not match:
            raise ValueError(
                "Invalid schedule. Use HH:MM|daily or HH:MM|weekly(day), "
                "for example 05:00|weekly(sunday)."
            )
        frequency = "weekly" if match["frequency"].startswith("weekly") else "daily"
        weekday_name = match["weekday"]
        if frequency == "weekly" and weekday_name.lower() not in cls.WEEKDAYS:
            valid_days = ", ".join(cls.WEEKDAYS)
            raise ValueError(f"Invalid weekly day '{weekday_name}'. Use one of: {valid_days}.")
        return cls(
            hour=int(match["hour"]),
            minute=int(match["minute"]),
            frequency=frequency,
            weekday=cls.WEEKDAYS[weekday_name.lower()] if weekday_name else None,
        )

    def next_run(self, now):
        candidate = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if self.frequency == "daily":
            return candidate + timedelta(days=1) if candidate <= now else candidate

        days_until = (self.weekday - now.weekday()) % 7
        candidate += timedelta(days=days_until)
        return candidate + timedelta(days=7) if candidate <= now else candidate


class Database:
    def __init__(self, filename):
        # SQLite creates the database file itself, but not its parent
        # directory.  Create it so a fresh /app bind mount works without
        # manually creating /app/data first.
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(filename)
        # WAL allows reads and writes to coexist and the timeout prevents a
        # transient lock (for example, during a backup) from failing a run.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.setup()

    def setup(self):
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS movies (
                    plex_key TEXT PRIMARY KEY,
                    tmdb_id TEXT,
                    title TEXT
                );
                CREATE TABLE IF NOT EXISTS tmdb_cache (
                    source_tmdb_id TEXT,
                    recommended_tmdb_id TEXT,
                    PRIMARY KEY (source_tmdb_id, recommended_tmdb_id)
                );
                CREATE TABLE IF NOT EXISTS tmdb_cache_sources (
                    source_tmdb_id TEXT PRIMARY KEY,
                    fetched_at TIMESTAMP NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watch_history (
                    plex_key TEXT,
                    user TEXT,
                    watched_at TIMESTAMP,
                    UNIQUE(plex_key, user, watched_at)
                );
                CREATE INDEX IF NOT EXISTS idx_movies_tmdb_id ON movies(tmdb_id);
                CREATE INDEX IF NOT EXISTS idx_watch_history_plex_key ON watch_history(plex_key);
                """
            )

    def close(self):
        self.conn.close()


class RunLog:
    """Mirror one recommendation run to stdout/stderr and a timestamped file."""

    def __init__(self, directory, keep=5):
        self.directory = Path(directory)
        self.keep = keep

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        existing_logs = sorted(self.directory.glob("recommendarr-*.log"))
        for old_log in existing_logs[: max(0, len(existing_logs) - self.keep + 1)]:
            old_log.unlink()

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        self.path = self.directory / f"recommendarr-{timestamp}.log"
        self.file = self.path.open("w", encoding="utf-8")
        self.stdout, self.stderr = sys.stdout, sys.stderr
        sys.stdout = Tee(self.stdout, self.file)
        sys.stderr = Tee(self.stderr, self.file)
        print(f"recommendarr run started: {datetime.now():%Y-%m-%d %H:%M:%S}")
        return self

    def __exit__(self, exception_type, exception, traceback):
        if exception_type is None:
            print(f"recommendarr run finished: {datetime.now():%Y-%m-%d %H:%M:%S}")
        else:
            print(f"recommendarr run failed: {datetime.now():%Y-%m-%d %H:%M:%S}", file=self.file)
            traceback_module.print_exception(exception_type, exception, traceback, file=self.file)
        sys.stdout, sys.stderr = self.stdout, self.stderr
        self.file.close()


class Tee:
    """Write output to each wrapped stream."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


class RecommendarrMVP:
    def __init__(self, settings):
        self.settings = settings
        print("Connecting to Plex...")
        self.plex = PlexServer(settings.plex_url, settings.plex_token)
        self.movies_section = self.plex.library.section(settings.plex_library)
        self.db = Database(settings.database_file)
        self.http = requests.Session()
        self.http.headers.update({"accept": "application/json"})
        # A standard TMDB v3 API key is a short, opaque value and must be sent
        # as the api_key query parameter. A v4 Read Access Token is a JWT and
        # is sent as a Bearer token instead.
        self.tmdb_params = {}
        if settings.tmdb_credential.startswith("eyJ"):
            self.http.headers["Authorization"] = f"Bearer {settings.tmdb_credential}"
        else:
            self.tmdb_params["api_key"] = settings.tmdb_credential
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET",)),
            respect_retry_after_header=True,
        )
        self.http.mount("https://", HTTPAdapter(max_retries=retry))

    @staticmethod
    def extract_tmdb_id(guids):
        """Return a TMDB ID from a movie's Plex GUIDs, if one is available."""
        for guid in guids:
            if guid.id.startswith("tmdb://"):
                # Safe fallback for Python < 3.9 (avoiding removeprefix)
                return guid.id[7:]
        return None

    def sync_library(self):
        print("Syncing Plex library to database...")
        movies = self.movies_section.all()
        records = [
            (str(movie.ratingKey), tmdb_id, movie.title)
            for movie in movies
            if (tmdb_id := self.extract_tmdb_id(movie.guids))
        ]
        current_keys = {record[0] for record in records}
        with self.db.conn:
            self.db.conn.executemany(
                """
                INSERT INTO movies (plex_key, tmdb_id, title) VALUES (?, ?, ?)
                ON CONFLICT(plex_key) DO UPDATE SET
                    tmdb_id = excluded.tmdb_id,
                    title = excluded.title
                """,
                records,
            )
            stale_keys = [
                (plex_key,)
                for (plex_key,) in self.db.conn.execute("SELECT plex_key FROM movies")
                if plex_key not in current_keys
            ]
            self.db.conn.executemany("DELETE FROM movies WHERE plex_key = ?", stale_keys)
        print(
            f"Library sync complete. Indexed {len(records)} of {len(movies)} movies with TMDB IDs"
            f"; removed {len(stale_keys)} stale entries."
        )

    def get_recent_watches(self):
        cutoff_date = datetime.now() - timedelta(days=self.settings.days_back)
        print(f"Fetching server-wide watch history for the last {self.settings.days_back} days...")
        history = self.plex.history(
            mindate=cutoff_date, librarySectionID=self.movies_section.key
        )
        users = {account.id: account.name for account in self.plex.systemAccounts()}
        tmdb_by_plex_key = dict(
            self.db.conn.execute("SELECT plex_key, tmdb_id FROM movies")
        )

        watch_events = []
        history_rows = []
        now = datetime.now()
        for event in history:
            plex_key = str(event.ratingKey)
            user_name = users.get(getattr(event, "accountID", None), "Server Owner / Unknown")
            history_rows.append((plex_key, user_name, event.viewedAt))
            tmdb_id = tmdb_by_plex_key.get(plex_key)
            if not tmdb_id:
                continue

            days_ago = max(0, (now - event.viewedAt).days)
            weight = max(0.1, 1.0 - (days_ago / self.settings.days_back))
            watch_events.append({"tmdb_id": tmdb_id, "weight": weight})

        with self.db.conn:
            self.db.conn.executemany(
                "INSERT OR IGNORE INTO watch_history (plex_key, user, watched_at) VALUES (?, ?, ?)",
                history_rows,
            )
        print(f"Found {len(watch_events)} valid server-wide watch events and synced to database.")
        return watch_events

    def get_tmdb_recommendations(self, tmdb_id):
        """Return cached TMDB recommendations, fetching each seed movie at most once."""
        cached = self.db.conn.execute(
            "SELECT recommended_tmdb_id FROM tmdb_cache WHERE source_tmdb_id = ?", (tmdb_id,)
        ).fetchall()
        if cached:
            return [row[0] for row in cached]
        already_fetched = self.db.conn.execute(
            "SELECT 1 FROM tmdb_cache_sources WHERE source_tmdb_id = ?", (tmdb_id,)
        ).fetchone()
        if already_fetched:
            return []

        print(f"  [Cache miss] Fetching TMDB recommendations for ID: {tmdb_id}")
        try:
            response = self.http.get(
                f"https://api.themoviedb.org/3/movie/{tmdb_id}/recommendations",
                params=self.tmdb_params,
                timeout=20,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            print(f"  TMDB request failed for {tmdb_id}: {error}")
            return []

        recommendations = [str(item["id"]) for item in response.json().get("results", [])]
        with self.db.conn:
            self.db.conn.executemany(
                "INSERT OR IGNORE INTO tmdb_cache (source_tmdb_id, recommended_tmdb_id) VALUES (?, ?)",
                ((tmdb_id, recommendation) for recommendation in recommendations),
            )
            self.db.conn.execute(
                "INSERT OR REPLACE INTO tmdb_cache_sources (source_tmdb_id, fetched_at) VALUES (?, ?)",
                (tmdb_id, datetime.now()),
            )
        return recommendations

    def calculate_scores(self, watch_events):
        print("Calculating recommendations...")
        scores = defaultdict(float)
        consensus = defaultdict(int)
        # A movie can appear many times in the watch history. Fetch its cached
        # TMDB results once, while preserving the exact total weight and source
        # count that the per-event implementation produced.
        seed_weights = defaultdict(float)
        seed_event_counts = defaultdict(int)
        for event in watch_events:
            tmdb_id = event["tmdb_id"]
            seed_weights[tmdb_id] += event["weight"]
            seed_event_counts[tmdb_id] += 1

        for tmdb_id, weight in seed_weights.items():
            event_count = seed_event_counts[tmdb_id]
            for recommendation in self.get_tmdb_recommendations(tmdb_id):
                scores[recommendation] += weight
                consensus[recommendation] += event_count

        owned_movies = {
            tmdb_id: (plex_key, title)
            for plex_key, tmdb_id, title in self.db.conn.execute(
                "SELECT plex_key, tmdb_id, title FROM movies"
            )
        }
        candidates = [
            {
                "plex_key": owned_movies[tmdb_id][0],
                "tmdb_id": tmdb_id,
                "title": owned_movies[tmdb_id][1],
                "score": score + (consensus[tmdb_id] * 2.0),
                "consensus": consensus[tmdb_id],
            }
            for tmdb_id, score in scores.items()
            if tmdb_id in owned_movies
        ]
        return sorted(candidates, key=lambda candidate: candidate["score"], reverse=True)

    def export_recommendations(self, recommendations):
        """Write a Kometa text_file list without changing Plex collections directly."""
        top_recommendations = recommendations[: self.settings.top_n]
        output_path = Path(self.settings.export_location) / self.settings.export_file
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# recommendarr recommendations",
            f"# Intended Kometa collection: {self.settings.collection_name}",
            f"# Generated: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            "# Movie library entries use explicit TMDb IDs for Kometa's text_file builder.",
        ]
        lines.extend(
            f"tmdb:{recommendation['tmdb_id']}    # {recommendation['title']} | "
            f"score: {recommendation['score']:.2f} | "
            f"sources: {recommendation['consensus']}"
            for recommendation in top_recommendations
        )
        temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
        temporary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary_path.replace(output_path)
        print(
            f"Exported {len(top_recommendations)} recommendations to "
            f"'{output_path}' for Kometa."
        )
        print("\n--- recommendarr TOP 5 ---")
        for index, recommendation in enumerate(top_recommendations[:5], 1):
            print(
                f"{index}. {recommendation['title']} "
                f"(Score: {recommendation['score']:.2f} | "
                f"Supported by {recommendation['consensus']} sources)"
            )

    def run(self):
        try:
            self.sync_library()
            recommendations = self.calculate_scores(self.get_recent_watches())
            self.export_recommendations(recommendations)
            print("\nrecommendarr run complete!")
        finally:
            self.db.close()
            self.http.close()


def run_once(settings, log_directory):
    with RunLog(log_directory):
        RecommendarrMVP(settings).run()


def run_on_schedule(settings, schedule, log_directory):
    """Keep the process alive and run recommendarr at each scheduled time."""
    while True:
        next_run = schedule.next_run(datetime.now())
        print(f"Next recommendarr run scheduled for {next_run:%Y-%m-%d %H:%M}.")
        while (remaining := (next_run - datetime.now()).total_seconds()) > 0:
            # Short sleeps allow a clean stop and avoid a single long blocking wait.
            time.sleep(min(remaining, 60))
        run_once(settings, log_directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Plex recommendations from server watch history.")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml).",
    )
    parser.add_argument(
        "--schedule",
        help="Run continuously on HH:MM|daily or HH:MM|weekly(day), overriding config.yaml.",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run once immediately before waiting for the configured schedule.",
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="Directory where the five most recent run logs are kept (default: logs).",
    )
    arguments = parser.parse_args()
    settings = Settings.from_yaml(arguments.config)
    schedule_value = arguments.schedule or settings.schedule
    if schedule_value:
        if arguments.run_now:
            run_once(settings, arguments.log_dir)
        run_on_schedule(settings, Schedule.parse(schedule_value), arguments.log_dir)
    else:
        run_once(settings, arguments.log_dir)
