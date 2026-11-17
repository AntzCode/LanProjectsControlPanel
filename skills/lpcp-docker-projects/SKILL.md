---
name: lpcp-docker-projects
description: Make a docker/compose project compatible with LanProjectsControlPanel (lpcp), so several projects can run at once on localhost without port conflicts. Use when asked to make a project run side by side with others, to fix localhost port collisions between projects, to add a `-p`/`--port` option to a project's `./dev`, to register a project with the control panel, or when you see a repo with a `./dev up` script and fixed published ports.
---

# Making a docker project work with LanProjectsControlPanel (lpcp)

lpcp is a separate repo with its own `./dev`. It keeps a registry of projects. `./dev up [site...]`
in the control panel starts each project by running **that project's** `./dev up -p <port>` on a port
lpcp chose, and a Caddy proxy then serves `https://<site>.localhost`. A project only has to accept
`-p`. Everything else is lpcp's job.

The rule that matters most: **the project must keep working on its own.** A colleague who clones it
and runs `./dev up` without lpcp must get exactly what they got before.

## The contract

1. `./dev up -p PORT` (long form `--port PORT`) publishes the project's web entry point on
   `127.0.0.1:PORT`. With no `-p`, nothing changes: same behaviour, same default port.
2. The published port comes from a variable with the old port as its default:
   `ports: ["127.0.0.1:${WEB_PORT:-3000}:3000"]` (use the project's real container port and default).
3. `./dev down` stops the project.
4. `./dev logs` follows its logs (optional; lpcp's `./dev logs <site>` calls it).
5. Optional: when `LPCP_URL` is set (lpcp sets `LPCP_URL`, `LPCP_HOST`, `LPCP_PORT`, `LPCP_PROJECT`
   when it starts a project), use it as the app's public base URL and for CORS/allowed origins, since
   the site is reached at `https://<site>.localhost`, not at the port.

## Steps

1. **Read first.** Open the project's `./dev` (or whatever starts it) and its compose file. Find every
   published port. Note the web entry point and its default host port.
2. **Compose:** change only the web service's port line to `"127.0.0.1:${WEB_PORT:-<old port>}:<container port>"`.
   Keep the old default exactly. Do not remove `ports:`, rename services, or add networks or labels.
3. **`./dev`:** add the option parsing below to the existing `up` handling; keep everything else it does.
   If the project has no `./dev`, don't invent one unless asked: register it with a custom command instead
   (step 5).
4. **Other published ports** (database, mail UI, API, debug ports) will still collide with another project
   that publishes the same ones. Tell the user which they are. Fixes, in order of preference: don't publish
   them (use `docker compose exec`), or offset them from the web port (`${DB_PORT:-5432}` and have `./dev`
   set `DB_PORT=$((PORT+1))` when `-p` is given). Only change what the user agrees to.
5. **Register** (from the lpcp repo): `./dev add <name> <path-to-project>`. For a project with no `./dev`:
   `./dev add blog ~/Projects/blog --up 'WEB_PORT={port} docker compose up -d' --down 'docker compose stop'`.
   `{port}`, `{host}`, `{url}` and `{name}` are filled in. `./dev ls` shows the result.
6. **Verify** (see below). Report what you ran and saw.

## Option parsing for a bash `./dev`

Put it after `cd "$(dirname "$0")"` and after the command word has been read, before the compose call:

```bash
# -p / --port PORT: move the web service to another host port. No -p: nothing changes.
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--port) export WEB_PORT="${2:?$1 needs a port number}"; shift 2 ;;
    *) break ;;
  esac
done
```

A full reference script and compose file are in `examples/` in the lpcp repo. If the project's `./dev`
already handles other flags (for example `up --lan`), merge into its existing parsing instead of pasting this
loop, and keep those flags working in any order.

## Verify

Do all of these. Nothing may be left running afterwards.

1. **Unchanged standalone:** `./dev up` with no options. The service answers on its old default port.
   `./dev down`.
2. **Moved:** `./dev up -p 8199` answers on `127.0.0.1:8199`, and nothing is on the old port.
   `./dev down`.
3. **Two at once:** with the control panel, register the project and one other, run `./dev up` there,
   and `curl -k https://<name>.localhost` for each (the proxy may be on non-default ports: check
   `./dev ls` for the URL). If the machine's 80/443 are busy, `./dev proxy up` names what holds them;
   stop that or set `http_port`/`https_port` in the lpcp `projects.toml`.
4. `git diff` in the project should show only the port line and the `-p` parsing.

## Don't

- Don't remove or rename a project's default ports, and don't make `-p` required.
- Don't add `external` networks, labels, or any dependency on lpcp to the project's compose file. A
  project must start on a machine that has never heard of lpcp.
- Don't point projects at each other's ports or at `host.docker.internal`; lpcp does the routing.
- Don't edit a real project without being asked. Propose the diff first when the user only asked how it works.
- Don't stage or commit for the user.

## Doesn't fit

A project that terminates TLS itself on real hostnames (its own Caddy/nginx on 80/443 with certificates
for named domains) can't sit behind lpcp, because the origin changes with the port. Leave it on its own
setup, and if it must run beside lpcp, give it another loopback address (e.g. `127.0.0.2`) if it supports
a bind setting, or run it on its own.
