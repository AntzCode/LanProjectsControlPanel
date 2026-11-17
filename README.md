# LanProjectsControlPanel

Run several docker projects on one machine at the same time, without fighting over ports.

Each project keeps its own `./dev` and its own ports, and stays fully standalone. This tool
registers your projects, starts each on a port it picks, and puts one Caddy proxy in front so you
open `https://nan.localhost` instead of remembering `localhost:8101`.

```
https://nan.localhost   ─┐
https://blog.localhost   ├─►  proxy (80/443)  ─►  127.0.0.1:8100  nan   (./dev up -p 8100)
https://shop.localhost  ─┘                        127.0.0.1:8101  blog  (./dev up -p 8101)
                                                  127.0.0.1:8102  shop  (./dev up -p 8102)
```

Why not label-based proxies (Traefik, caddy-docker-proxy)? They need every project's compose file
changed (labels, a shared external network), which breaks it for anyone who clones it without the
proxy. Here a project only learns one option, `-p PORT`, and ignores it unless it's given.

## Requirements

Linux (or anything with bash, Docker and `ss`), Docker with Compose, Python 3.11+. No packages to install:
the tool is the Python standard library, and Caddy runs as a container.

## Quick start

```bash
git clone https://github.com/AntzCode/LanProjectsControlPanel.git
cd LanProjectsControlPanel

./dev add nan  ~/Projects/NaNPhone       # registers it in projects.toml (git-ignored)
./dev add blog ~/Projects/blog --up 'WEB_PORT={port} docker compose up -d' --down 'docker compose stop'

./dev up            # every registered site; or ./dev up nan blog
./dev ls            # URL, port and state of each
./dev down nan      # one site; ./dev down stops all of them and the proxy
```

`./dev up` assigns each project a free port from 8100-8999 the first time and remembers it, starts the
project, then starts or reloads the proxy. The proxy holds ports 80 and 443 only while at least one site
is running.

A project has to accept `-p` first. See [Making a project compatible](#making-a-project-compatible).

## Commands

| Command | What it does |
|---|---|
| `./dev up [site...]` | Start sites (all registered when none are named), then update the proxy |
| `./dev down [site...]` | Stop sites (all when none are named). The proxy stops when nothing is left |
| `./dev restart [site...]` | Stop then start |
| `./dev ls` | Registered sites, their URLs, ports and whether they are running |
| `./dev logs <site>` | Follow a site's logs (runs its `logs` command, default `./dev logs`) |
| `./dev open <site>` | Open it in the browser |
| `./dev add <name> <dir> [--host H] [--up CMD] [--down CMD]` | Register a project |
| `./dev rm <name>` | Unregister (does not stop it) |
| `./dev proxy up\|down\|reload\|status` | Control the proxy directly |
| `./dev trust` | Export Caddy's local CA certificate (see HTTPS) |
| `./dev doctor` | Check Docker, the registry, project folders, and who holds ports 80/443 |

## The registry

`projects.toml` lives in the repo folder and is not committed (it holds this machine's paths).
`./dev add` writes it; you can also edit it. `projects.example.toml` shows every option:

```toml
[settings]                     # all optional, defaults shown
domain = "localhost"           # sites are <name>.localhost
https = true                   # Caddy's local CA; false = plain http
http_port = 80
https_port = 443
bind = "127.0.0.1"
port_range = [8100, 8999]
wait = 60                      # seconds to wait for a started project's port to open

[projects.nan]
dir = "~/Projects/NaNPhone"
# host = "nan.localhost"       # optional overrides
# port = 8101                  # otherwise assigned once and remembered in .state/
# up   = "./dev up -p {port}"  # {port} {host} {url} {name} are filled in
# down = "./dev down"
# logs = "./dev logs"
```

When it starts a project, `./dev` also sets `LPCP_PROJECT`, `LPCP_PORT`, `LPCP_HOST` and `LPCP_URL` in the
project's environment, so an app can build absolute URLs and CORS origins from `LPCP_URL`.

## Making a project compatible

The contract is small, and a project that follows it still runs exactly as before without this tool:

1. `./dev up -p PORT` (also `--port`) publishes the web entry point on `127.0.0.1:PORT`. With no `-p`,
   nothing changes: same behaviour, same default port.
2. In compose, take the host port from a variable that defaults to the old port:
   `ports: ["127.0.0.1:${WEB_PORT:-3000}:3000"]`.
3. `./dev down` stops it (and `./dev logs` follows its logs, optionally).
4. Optional: read `LPCP_URL` as the app's public URL when it is set.

`examples/dev` and `examples/compose.yaml` are a complete, runnable reference. Other published ports
(databases, mail UIs, APIs) still collide between projects that publish the same ones: don't publish them,
or offset them from `-p`. A project with no `./dev` can be registered with a custom `--up` command.

### Agent skill

`skills/lpcp-docker-projects/SKILL.md` teaches a coding agent (Claude Code) how to make a project
compatible, step by step, and how to check it did not break standalone use. It is meant to be installed
once per developer, after cloning:

```bash
# symlink (stays current with the repo)
ln -s "$PWD/skills/lpcp-docker-projects" ~/.claude/skills/lpcp-docker-projects
# or copy
cp -r skills/lpcp-docker-projects ~/.claude/skills/
```

## HTTPS

By default sites use HTTPS with certificates from Caddy's own local CA, which your browser doesn't trust
until you tell it to. `./dev trust` exports the CA certificate (`.state/lpcp-root.crt`):

```bash
curl --cacert .state/lpcp-root.crt https://nan.localhost      # verified, no system changes
```

Trusting it in a browser or OS is a one-time import of that file, which differs by browser and OS (Firefox and
Chrome on Linux keep their own stores). Set `https = false` in `[settings]` for plain `http://` sites.
The CA persists in the `lpcp-caddy-data` Docker volume across restarts.

`*.localhost` resolves to 127.0.0.1 on its own in browsers and `curl` (RFC 6761), so no hosts-file entries
are needed.

## Limits

- **Ports 80 and 443 must be free** when the proxy starts. If something holds them (another project's own
  Caddy, for example), `./dev up` says what holds them, still starts your projects, and prints their direct
  `http://127.0.0.1:PORT` addresses. Fix it by stopping that thing, or set `http_port`, `https_port` and `bind`
  in `[settings]`. Linux accepts every address in 127.0.0.0/8, so `bind = "127.0.0.2"` works beside a service
  bound to 127.0.0.1 (tested). It does not help when the other service listens on `0.0.0.0` (all addresses),
  which also covers 127.0.0.2; `./dev doctor` shows who holds what.
- **With non-default `http_port`/`https_port`**, Caddy's plain-HTTP redirect points at the default HTTPS
  port. Use the `https://` URL that `./dev ls` shows.
- **Not for projects that terminate TLS themselves on real hostnames** (their own Caddy on 80/443 with
  certificates for named domains): the origin changes with the port. Leave those on their own setup.
- **Database and other non-web ports** are not handled; see above.
- **LAN access** (other devices) isn't set up: sites are on the loopback address only.
- "Running" means something is listening on the project's port. If another process takes a remembered port
  while a project is stopped, `./dev up` will think the project is already running.
- The proxy container runs with host networking so it can reach ports a project binds on `127.0.0.1` only.

## Tests

```bash
python3 -m unittest discover -s tests
```

Covers the registry, port assignment, command templating and Caddyfile generation. Docker is not needed.

Verified by hand on Docker 29 / Compose 5 (2026-11-15): two projects that both default to port 3000 (the second
fails to start standalone) ran together under `./dev up` on ports 8100 and 8101 and answered at
`p1.localhost` and `p2.localhost` over HTTPS, verified against the exported CA; `./dev down p1` removed only
p1's route; a port conflict on 80 was reported with the holder's name; `./dev down` removed the proxy; `bind = "127.0.0.2"`
ran the proxy beside a separate listener on the same port at 127.0.0.1 (on alternate ports, not on 80/443).
Not tested: browsers, OSes other than Linux, trusting the CA system-wide.

## License

Copyright (C) 2026 AntzCode. Licensed under the
[GNU Affero General Public License v3.0](LICENSE) (`AGPL-3.0-only`).
