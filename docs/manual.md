# LanProjectsControlPanel manual

The full reference. For the short version, see the [README](../README.md).

A port belongs to an address, not to the whole machine, and Linux gives you a whole range of loopback
addresses (127.0.0.1 to 127.255.255.254). So this tool gives each project its own address and starts it with
`./dev up --bind=<address>`. Every project keeps its usual ports (80, 443, 5432, ...) and none of them collide.
One Caddy proxy on 127.0.0.1 serves them by name:

```
https://nan.localhost   ─┐                          ┌─►  127.0.0.2   nan   (./dev up --bind=127.0.0.2)   :80 :5432
https://blog.localhost  ─┴─►  proxy 127.0.0.1:443  ─┴─►  127.0.0.3   blog  (./dev up --bind=127.0.0.3)   :80 :5432

https://dev.example.com  ───────────────────────────►  192.168.1.20  a project with real hostnames and its
                                                                       own TLS, bound to the LAN address
```

- **No DNS, no hosts file, no sudo.** `*.localhost` always resolves to 127.0.0.1, where the proxy is.
- **Projects stay standalone.** A project only learns `--bind=IP` and ignores it unless it's given, so anyone
  who clones it without this tool runs it exactly as before.
- **Databases don't collide either**, because every port of a project moves to its address.
- **Projects with real hostnames and their own certificates** (for example, ones you test on a phone over the
  LAN) are bound to the LAN address and left out of the proxy. They keep their own TLS and renewal.

Why not label-based proxies (Traefik, caddy-docker-proxy)? They need every project's compose file changed
(labels, a shared external network), which breaks it for anyone who clones it without the proxy, and they don't
help with database ports.

## Requirements

Linux, Docker with Compose, Python 3.11+, `ss`. No packages to install: the tool is the Python standard
library, and Caddy runs as a container. (macOS has only 127.0.0.1 unless an admin adds loopback aliases.)

The optional [control panel](#control-panel) window also needs GTK 3 for Python, which most Linux desktops
already have (Debian/Ubuntu: `python3-gi` and `gir1.2-gtk-3.0`). Its tray icon uses XApp, which Linux Mint has
(`gir1.2-xapp-1.0`); without it, the window works as usual.

## Quick start

```bash
git clone https://github.com/AntzCode/LanProjectsControlPanel.git
cd LanProjectsControlPanel

./dev add nan  ~/Projects/NaNPhone        # registers it in projects.toml (git-ignored)
./dev add st   ~/Projects/ScriptureTips --bind lan --port 443 --no-proxy --url https://dev.scripturetips.com

./dev up            # every registered site; or ./dev up nan st
./dev ls            # address, URL and state of each
./dev down nan      # one site; ./dev down stops all of them (and the proxy)
```

`./dev up` gives each project a free address from 127.0.0.2-127.0.0.99 the first time and remembers it (an
address where anything already listens is skipped, since a project owns its whole address), runs
the project's `./dev up --bind=<address>`, waits until it answers, then starts or reloads the proxy. The proxy
holds 127.0.0.1:80/443 only while a proxied site is running.

A project has to accept `--bind` first. See [Making a project compatible](#making-a-project-compatible).

## Commands

| Command | What it does |
|---|---|
| `./dev up [site...]` | Start sites (all registered when none are named), then update the proxy |
| `./dev down [site...]` | Stop sites (all when none are named). The proxy stops when nothing needs it |
| `./dev restart [site...]` | Stop then start |
| `./dev ls` | Registered sites, their addresses, URLs and whether they are running |
| `./dev logs <site>` | Follow a site's logs (runs its `logs` command, default `./dev logs`) |
| `./dev open <site> [--app A] [--list]` | Open it: runs its `./dev launch [--app=A]`, or opens its URL when it has no launchers. `--list` shows its launchers |
| `./dev add <name> <dir> [options]` | Register a project: `--bind auto\|lan\|IP`, `--port N`, `--host H`, `--no-proxy`, `--url U`, `--up CMD`, `--down CMD` |
| `./dev rm <name>` | Unregister (does not stop it) |
| `./dev proxy up\|down\|reload\|status` | Control the proxy directly |
| `./dev trust` | Export Caddy's local CA certificate (see HTTPS) |
| `./dev doctor` | Check Docker, the registry, project folders, and anything blocking ports |
| `./dev ui [--tray] [--install]` | Open the control panel window, or with `--tray` put its icon in the panel's tray; `--install` adds it to the application menu (and with `--tray`, starts the icon at login) |

`./dev down <site>` always runs the site's stop command; `./dev down` with no names stops only sites that answer.

## Control panel

`./dev ui` opens a small window (450 px wide) with one row per registered project:

```
●  nan                            12.3%   1.4 GB  [open]
●  st                              0.8%   0.3 GB  [site] [data]
```

- **The dot** is green when the project's web port is published and answers, red when its last start from the
  panel failed (or the port is published but nothing answers), and translucent black when it's stopped.
  Click it to start or stop the project: the panel runs `./dev up <name>` or `./dev down <name>`, one at a time,
  and the dot pulses meanwhile. Hover over the name for the reason, URL and address.
- **CPU and memory** are summed over the project's running containers, as `docker stats` reports them (CPU in
  percent of one core, memory in GB), and refresh every few seconds. A project's containers are the ones compose
  started from its folder (or a folder inside it), so a stopped project can still show usage (its test
  containers, for example).
- **Launchers**: round icons between the name and the CPU/memory figures, one per launcher the project lists
  with `./dev config.launchers` (a JSON list of `{"app": ..., "title": ..., "class": ...}`, the default first;
  see the skill in `skills/lpcp-docker-projects`). The icon's color and glyph show what it opens, so you can
  tell them apart before reading the tooltip (the launcher's title):

  | class | icon | opens |
  |---|---|---|
  | `web` (default) | blue globe | the site itself in the browser |
  | `mail` | orange envelope | a mail catcher or webmail |
  | `db` | purple cylinder | a database or data browser |
  | `logs` | teal lines | logs, docs, anything you read |
  | `config` | amber gear | settings or an admin area |
  | `tool` | grey wrench | anything else, such as a shell or a dev tool |

  A class this version doesn't know is drawn as a tool. A click runs `./dev open <name> --app <app>`, which runs
  the project's `./dev launch --app=<app>` with the `LPCP_*` variables set. A project without launchers gets one
  `web` icon for its URL. The icons work while the project runs (dimmed otherwise) and are right-aligned, so
  every row's last icon sits against the CPU figure. The list is read again when the project's `./dev` changes; `./dev open <name> --list`
  shows each launcher's class.
- The window has no status bar. The dot shows how a start or stop went (red if it failed), and a failed start,
  stop or launch is also sent as a desktop notification. A note appears under the list only when the panel
  can't read the projects at all (docker unreachable, a broken `projects.toml`), and goes away when it can. Each
  start/stop is logged to `.state/logs/<name>.log` (in this repo's folder, next to `projects.toml`; git-ignored).
- Edits to `projects.toml` show up without restarting the panel. Closing the window leaves every project as it is.
  Opening it again while it's open brings the existing window to the front, or minimizes it if it's already in
  front (hides it, in tray mode).
- **Launched from a panel**, the window opens (or moves, if it's already open) just beside the panel, centered
  under the launcher icon: the pointer is on the icon when you click it, and it's outside the screen's work area.
  Launched from a menu or a terminal, it stays where the window manager puts it. On a monitor whose work area GTK
  doesn't know (any but the primary one, on X11), a launch within 64 px of an edge counts as a panel click; top and
  bottom win in a corner.

### Tray icon (Linux Mint)

`./dev ui --tray` puts an LPCP icon in the panel's tray (the XApp Status Applet, on Mint's panels by default)
and keeps the window hidden until you need it:

- **Left click** shows the window dropping from the icon, like a menu, and hides it again when it's in front.
  The tray reports the icon's exact position and which side the panel is on, so this is exact on every monitor.
- **Right click**: Open, or Quit LPCP (quits the panel; projects keep running).
- **Panic: Ctrl+Alt+left click** kills the registered project using the most CPU, for when the machine suddenly
  struggles. It measures CPU fresh (about 2 seconds), then `docker kill`s that project's containers: no graceful
  stop, so it works under load. The containers are stopped, not removed, so `./dev up <name>` brings it back. It
  kills nothing if no project uses 5% CPU or more, and never touches containers outside the registry. The window
  stays as it is; the result appears as a desktop notification, and every panic
  is logged to `.state/logs/panic.log` (and the killed project's own log). Tray only.
- Closing the window hides it; the icon stays. While it's hidden the panel doesn't call docker at all.
- `./dev ui --tray --install` adds the menu launcher and starts the icon at login
  (`~/.config/autostart/lpcp.desktop`). A plain `./dev ui --install` removes that autostart entry again.
- With no XApp tray (another desktop, or the applet removed), `--tray` says so and opens the window instead.

### Launcher

To launch it from the desktop without the tray, run `./dev ui --install` once. It writes `~/.local/share/applications/lpcp.desktop`
(with this repo's path), which puts **LPCP** in the application menu under Programming. On MATE, right-click it
there and choose **Add this launcher to panel**. Run `--install` again if you move the repo.

`projects.toml` lives in the repo folder and is not committed (it holds this machine's paths).
`./dev add` writes it; you can also edit it. [`projects.example.toml`](../projects.example.toml) shows every option:

```toml
[settings]                                     # all optional, defaults shown
domain = "localhost"                           # proxied sites are <name>.localhost
https = true                                   # Caddy's local CA; false = plain http
http_port = 80
https_port = 443
bind = "127.0.0.1"                             # the proxy's own address
address_range = ["127.0.0.2", "127.0.0.99"]
wait = 60                                      # seconds to wait for a started project to answer

[projects.nan]
dir = "~/Projects/NaNPhone"
# bind  = "auto"                  # auto (assigned once, remembered in .state/), "lan", or an IP
# port  = 80                      # its web port on that address: what the proxy forwards to
# host  = "nan.localhost"
# proxy = true
# url   = "..."                   # shown by ls/open instead of the derived URL
# up    = "./dev up --bind={bind}"  # {bind} {host} {url} {name} are filled in
# down  = "./dev down"
# logs  = "./dev logs"
```

`bind = "lan"` uses this machine's LAN address (the source address of its default route), looked up each time,
so it follows DHCP changes. When it starts a project, `./dev` also sets `LPCP_PROJECT`, `LPCP_BIND`, `LPCP_HOST`
and `LPCP_URL` in the project's environment, so an app can build absolute URLs and CORS origins from `LPCP_URL`.

## Making a project compatible

The contract is small, and a project that follows it still runs on its own without this tool:

1. **Plain `./dev up` publishes on 127.0.0.1**: this machine only, always. In compose, every published port is
   `"${BIND:-127.0.0.1}:<port>:<port>"`. Keep the port numbers; never publish a port without an address
   (`"80:80"` means every address).
2. `./dev up --bind=IP` (also `--bind IP`) publishes the same ports on IP instead. This is what lpcp calls.
3. `./dev up --lan` publishes on every address (0.0.0.0), for phones and tablets. It's the only way to get
   0.0.0.0. `--bind` and `--lan` together is an error.
4. `./dev up --host=NAME` (also `--host NAME`) makes the project answer to NAME: its web server's site name,
   base URL, allowed hosts and certificate. It combines with any of the above. `./dev up --host phuppi.loc` is
   reachable at `http://phuppi.loc` once `/etc/hosts` has `127.0.0.1 phuppi.loc` (or the `--bind` address).
   `*.localhost` names need no entry.
5. `./dev down` stops it (and `./dev logs` follows its logs, optionally).
6. Optional: read `LPCP_URL` as the app's public URL when it is set.
7. **The registered port is the front door.** lpcp's proxy sends `https://<name>.localhost` to the one port the
   project is registered with (`--port`, default 80), so that port must route every path the browser uses: the
   app, object storage (presigned URLs point at the site's own address), API paths, websockets. A port that
   serves only the app's container loses the routes a reverse proxy in front of it handles (uploads return 200
   but store nothing). If the project has a router (Caddy, nginx), it should own that port over plain HTTP, with
   the same routes as its HTTPS site, trusting the proxy's `X-Forwarded-Proto` and `X-Forwarded-Host` so URLs
   the app builds are `https://`. A project that can't do this registers with `--port 443 --no-proxy` instead
   and its own certificate must be trusted.

If a project published ports on every address before, plain `./dev up` becomes this-machine-only, and
`./dev up --lan` gives the old network access. If it accepts `--host`, register it with
`--up './dev up --bind={bind} --host={host}'`, so it answers to its `<name>.localhost` name behind the proxy.

[`examples/dev`](../examples/dev) and [`examples/compose.yaml`](../examples/compose.yaml) are a complete, runnable reference (a web server and a postgres).
A project with no `./dev` can be registered with a custom command:
`./dev add blog ~/blog --up 'BIND={bind} docker compose up -d --wait' --down 'docker compose stop'`.

A project bound to a network address (`bind = "lan"`) is reachable from the LAN. Keep its private ports
(database, internal API) on a loopback address rather than following `--bind`; ScriptureTips, for example, keeps
them on 127.0.0.1 or moves them with its own `--local-bind`.

### Agent skill

[`skills/lpcp-docker-projects/SKILL.md`](../skills/lpcp-docker-projects/SKILL.md) teaches a coding agent (Claude Code) how to make a project
compatible, step by step, and how to check it did not break standalone use. Install it once per developer,
after cloning:

```bash
# symlink (stays current with the repo)
ln -s "$PWD/skills/lpcp-docker-projects" ~/.claude/skills/lpcp-docker-projects
# or copy
cp -r skills/lpcp-docker-projects ~/.claude/skills/
```

## HTTPS

Proxied sites use HTTPS with certificates from Caddy's own local CA, which your browser doesn't trust until you
tell it to. `./dev trust` exports the CA certificate (`.state/lpcp-root.crt`):

```bash
curl --cacert .state/lpcp-root.crt https://nan.localhost      # verified, no system changes
```

Trusting it in a browser or OS is a one-time import of that file, which differs by browser and OS (Firefox and
Chrome on Linux keep their own stores). Set `https = false` in `[settings]` for plain `http://` sites.
The CA persists in the `lpcp-caddy-data` Docker volume across restarts. Projects with `proxy = false` handle
their own TLS.

## Limits

- **Anything listening on 0.0.0.0 blocks everyone.** A service on every address (a project started with
  `--lan`, for example) holds that port on 127.0.0.2, 127.0.0.3 and the LAN address too. `./dev up` refuses and
  names it; `./dev doctor` lists them. Restart it with `--bind` instead.
- **The proxy needs 127.0.0.1:80 and :443.** If something holds them, `./dev up` says what, still starts your
  projects, and prints their direct addresses. Or set `http_port`/`https_port` in `[settings]` (then Caddy's
  plain-HTTP redirect points at the default HTTPS port; use the `https://` URL that `./dev ls` shows).
- **`*.localhost` only works on this machine.** Other devices can't resolve it. For phones and tablets, bind the
  project to the LAN address and give it real hostnames with public DNS, as ScriptureTips does.
- **Linux-first.** macOS has only 127.0.0.1 without admin rights.
- "Running" means something answers on the project's address and web port.
- The proxy container runs with host networking so it can reach every 127.0.0.N.

## Tests

```bash
python3 -m unittest discover -s tests
```

Covers the registry, address assignment, command templating, URLs, Caddyfile generation, and the control
panel's logic (reading docker's output, project states, the launcher). Docker, GTK and a display are not needed.

Verified by hand on Docker 29 / Compose 5 (2026-11-17), alongside a running ScriptureTips stack:
- Two copies of `examples/` ran together on 127.0.0.2 and 127.0.0.3. Each published the same web port and
  5432, while ScriptureTips held 127.0.0.1:5432.
- Both answered through the proxy at `p1.localhost` and `p2.localhost` over HTTPS, verified against the exported
  CA, and both databases accepted connections.
- `./dev down p1` removed only p1's route. A restarted project kept its address. `./dev down` removed the proxy.
- With ScriptureTips started with `--lan` (0.0.0.0), `./dev up` refused and named its container.
- ScriptureTips' own `./dev up --bind=192.168.88.10` was verified in its repo (Let's Encrypt certificates, e2e
  tests).

- The control panel ran under MATE's window manager (marco) on a virtual display, started from its launcher's
  command line with no terminal. Real mouse clicks started and stopped two projects. A click while busy was
  ignored. A blocked start turned red with the reason, and clicking it again started the project once unblocked.
  Closing the window left the projects running, and a second launch raised the first window. The window is listed
  as a normal 450 px window with a title bar and close button. With a stand-in dock window as the panel, each
  launch put the window against a top or bottom panel, centered on the pointer and kept on screen.
- Tray mode ran natively on a private D-Bus session with a stand-in for Mint's tray applet, sending the
  applet's own ButtonPress/ButtonRelease calls. It started hidden, made no docker calls while hidden, and
  dropped from the icon's position (clamped at the screen edge). A second click hid it, the close button hid it
  without quitting, a menu launch handed over and toggled it, and Quit LPCP ended it. Without a tray host it
  fell back to the window.
- Panic: with one scratch project spinning a CPU core and one idle, Ctrl+Alt (held through the X server) plus a
  tray click killed only the busy one, kept the window hidden, sent the notification and logged it. A second
  panic killed nothing and said why. Marco passes Ctrl+Alt clicks through to a panel (tested with a dock window).

Not tested: browsers, phones, OSes other than Linux, trusting the CA system-wide, the panel on a real MATE
panel and window list.

## License

Copyright (C) 2026 AntzCode. Licensed under the
[GNU Affero General Public License v3.0](../LICENSE) (`AGPL-3.0-only`).
