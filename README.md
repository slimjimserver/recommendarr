# recommendarr

recommendarr builds recommendations from server-wide Plex watch history and exports
a Kometa-compatible TMDb text list.

## Docker Compose

1. Copy `config/config.yaml.example` to `config/config.yaml` and add your Plex
   and TMDb credentials. The `config` folder is mounted in the container at
   `/app`, so the program loads `/app/config.yaml`.
2. Copy `.env.example` to `.env`, replace `YOUR-GITHUB-USERNAME` in
   `RECOMMENDARR_IMAGE`, and on Linux set `PUID` and `PGID` to the results of
   `id -u` and `id -g`. The container uses these IDs when writing files to
   `data` and `kometa`, so they remain writable by your user.
3. The sample configuration is already set up for Docker. Keep these paths in
   `config/config.yaml`:

   ```yaml
   recommendations:
     export_location: /exports
     export_file: recommendarr-recommendations.txt
   database:
     file: /data/recommendarr.db
   schedule: "05:00|daily"
   ```

4. Pull the published image and start the scheduler:

   ```sh
   docker compose pull
   docker compose up -d
   ```

   The container runs once immediately, then remains running and waits for the
   next time defined by `schedule`.

   Future updates use `docker compose pull && docker compose up -d`, just like
   other registry-hosted services. The image is published by the GitHub Actions
   workflow whenever a change reaches `main`.

5. View the next scheduled time and run history:

   ```sh
   docker compose logs -f recommendarr
   ```

The generated list is written to `./kometa/recommendarr-recommendations.txt`.
Mount that same host directory in Kometa and set its `text_file` path to the
corresponding path inside the Kometa container.

Each execution is also written to a dated file in `./logs`.
The five newest run logs are retained automatically.

If `data` or `kometa` was created by an earlier root-run container, fix its
ownership once before starting the updated service:

```sh
sudo chown -R "$(id -u):$(id -g)" data kometa
```

## Publish to GitHub

The `.gitignore` keeps `config/config.yaml`, credentials, SQLite data, and generated
recommendation lists out of the repository. Create an empty repository on GitHub
(do not initialize it with a README, license, or `.gitignore`), then run:

```sh
git init
git add .
git commit -m "Initial recommendarr service"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/recommendarr.git
git push -u origin main
```

If Git asks for an identity before committing, set it once with
`git config --global user.name "Your Name"` and
`git config --global user.email "you@example.com"`.

The first push to `main` runs the included GitHub Actions workflow and publishes
`ghcr.io/YOUR-GITHUB-USERNAME/recommendarr:latest`. Make the resulting container
package public in its GitHub package settings to pull it without signing in. If
you keep it private, authenticate the Docker host to `ghcr.io` before pulling.
