# recommendarr

recommendarr creates a Kometa-compatible list of movies from recent Plex watch
history. It runs on a schedule, stores its cache locally, and writes the current
recommendations to a file that Kometa can use for a collection.

## Prerequisites

- Docker Engine with Docker Compose
- A Plex server URL and Plex token
- A TMDb API v3 key or v4 Read Access Token
- Kometa, if you want to turn the exported list into a Plex collection

## Setup

1. Create your private configuration file:

   ```sh
   cp config/config.yaml.example config/config.yaml
   ```

   Alternatively, the container copies the example to `config/config.yaml` on
   its first start. It will never overwrite an existing configuration file.

2. Edit `config/config.yaml` and replace the Plex and TMDb placeholder values.
   Keep the Docker paths shown below unless you intentionally change the Compose
   mounts:

   ```yaml
   recommendations:
     export_location: /app/exports
     export_file: recommendarr.txt
   database:
     file: /app/data/recommendarr.db
   schedule: "23:30|daily"
   ```

   `schedule` supports `HH:MM|daily` or `HH:MM|weekly(day)`, for example
   `05:00|weekly(sunday)`. Remove `schedule` to run once and exit.

3. Pull and start the service:

   ```sh
   # Ensure the host user that runs Docker owns the bind-mounted directory.
   sudo chown -R "$(id -u):$(id -g)" config

   docker compose pull
   docker compose up -d
   ```

   The service runs immediately once, then waits for the configured schedule.

## Kometa setup

recommendarr writes its output to:

```text
./config/exports/recommendarr.txt
```

Mount that host folder into your Kometa container, then reference the mounted
file with Kometa's `text_file` builder. The file uses explicit TMDb IDs and is
rewritten atomically after every run.

## Day-to-day commands

```sh
# View service output and the next scheduled run.
docker compose logs -f recommendarr

# Download the newest published image and recreate the service.
docker compose pull && docker compose up -d

# Stop the service without removing its cache or exports.
docker compose down
```

Runtime files are kept under `config/`:

- `config/data/` — SQLite cache and watch history
- `config/exports/` — Kometa recommendation list
- `config/logs/` — the five newest detailed run logs

These paths, along with `config/config.yaml`, are excluded from Git.

The container runs as UID/GID `1000:1000`. Recommendarr creates missing
`data`, `exports`, and `logs` directories under `config/`. Do not add separate
nested bind mounts for them: if a source directory is missing, Docker creates
it as `root`, preventing Recommendarr from writing to it.

## Updating settings

After editing `config/config.yaml`, restart the container to apply the change:

```sh
docker compose up -d --force-recreate
```
