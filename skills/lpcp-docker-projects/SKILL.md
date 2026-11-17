---
name: lpcp-docker-projects
description: Make a docker/compose project compatible with LanProjectsControlPanel (lpcp), so several projects can run at once on one machine without port conflicts. Use when asked to make a project run side by side with others, to fix port collisions between local projects (web, database or other ports), to add a `--bind` option to a project's `./dev`, to register a project with the control panel, or when you see a repo with a `./dev up` script and ports published on 127.0.0.1 or 0.0.0.0.
---

# Making a docker project work with LanProjectsControlPanel (lpcp)

lpcp is a separate repo with its own `./dev`. It keeps a registry of projects and gives each one its own
address (127.0.0.2, 127.0.0.3, ..., or the LAN address). `./dev up [site...]` in the control panel starts each
project by running **that project's** `./dev up --bind=<address>`. Each project keeps its usual port numbers,
and they don't collide because a port belongs to an address. A Caddy proxy on 127.0.0.1 then serves
`https://<site>.localhost`. A project only has to accept `--bind`. Everything else is lpcp's job.

The rule that matters most: **the project must keep working on its own.** A colleague who clones it and runs
`./dev up` without lpcp must get exactly what they got before.

## The contract

1. `./dev up --bind=IP` (and `--bind IP`) publishes the project's ports on IP instead of 127.0.0.1. With no
   `--bind`, nothing changes.
2. If the project has `--lan` (publish on every address, 0.0.0.0), `--bind` and `--lan` are mutually
   exclusive: both together is an error.
3. Every published port takes its address from a variable defaulting to the old one:
   `"${BIND:-127.0.0.1}:80:80"`. **Port numbers stay the same.**
4. `./dev down` stops the project; `./dev logs` follows its logs (optional).
5. Optional: when `LPCP_URL` is set (lpcp sets `LPCP_URL`, `LPCP_HOST`, `LPCP_BIND`, `LPCP_PROJECT` when it
   starts a project), use it as the app's public base URL and for CORS/allowed origins.

## Steps

1. **Read first.** Open the project's `./dev` (or whatever starts it) and its compose file(s). List every
   published port and the address it's on. Note the web entry point's port (lpcp's `port`, default 80).
2. **Compose:** on each published port, replace the address (`127.0.0.1:`, `0.0.0.0:` or none, which means
   every address) with `${BIND:-<old address>}:`. Keep the old default exactly: if a port had no address
   (`"80:80"`, which is every address), the default is `0.0.0.0`; say so to the user, because such a port blocks
   other projects in standalone use, and suggest `127.0.0.1` only if they agree. Don't rename services or add
   networks or labels.
3. **`./dev`:** add the option parsing below to the existing `up` handling, keeping everything else it does. If
   it already has `--lan` or other flags, merge into its existing parsing and keep them working in any order.
   If the project has no `./dev`, don't invent one unless asked: register it with a custom command (step 5).
4. **Private ports on a network address:** if the project might be bound to the LAN address (it has real
   hostnames and its own TLS, for phone or tablet testing), its database and internal API must not follow
   `--bind` onto the network. Keep those on 127.0.0.1 (or a separate `--local-bind`), and tell the user.
5. **Register** (from the lpcp repo): `./dev add <name> <path-to-project>` (add `--port N` if the web port isn't
   80). For a project with real hostnames, its own TLS and public DNS pointing at this machine:
   `./dev add <name> <path> --bind lan --port 443 --no-proxy --url https://<its name>`. For a project with no
   `./dev`: `./dev add blog ~/blog --up 'BIND={bind} docker compose up -d --wait' --down 'docker compose stop'`.
   `./dev ls` shows the result.
6. **Verify** (see below). Report what you ran and saw.

## Option parsing for a bash `./dev`

Put it after the command word has been read, before the compose call:

```bash
bind="" lan=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bind=*) bind="${1#--bind=}"; [[ -n "$bind" ]] || { echo "--bind needs an address" >&2; exit 2; }; shift ;;
    --bind)   bind="${2:?--bind needs an address}"; shift 2 ;;
    --lan)    lan=1; shift ;;          # only if the project already has --lan
    *) break ;;
  esac
done
if [[ -n "$bind" && -n "$lan" ]]; then echo "--bind and --lan can't be used together" >&2; exit 2; fi
if [[ -n "$bind" ]]; then export BIND="$bind"; elif [[ -n "$lan" ]]; then export BIND=0.0.0.0; fi
```

A full reference script and compose file are in `examples/` in the lpcp repo.

## Verify

Do all of these. Leave nothing running that wasn't running before.

1. **Unchanged standalone:** `./dev up` with no options. Every port is on its old address (`ss -ltn`). `./dev down`.
2. **Moved:** `./dev up --bind=127.0.0.99`. Every published port is on 127.0.0.99 and nothing is on the old
   address (except private ports deliberately kept on loopback). `./dev down`.
3. **Exclusive:** `./dev up --bind=127.0.0.99 --lan` exits non-zero with a message, if the project has `--lan`.
4. **Two at once:** from the lpcp repo, `./dev up <name> <another>`, then `curl -k https://<name>.localhost`
   (or the project's own URL), and `./dev ls`. If `./dev up` reports a 0.0.0.0 holder, something was started
   with `--lan`: ask the user before stopping it.
5. `git diff` in the project should show only the port addresses, the option parsing and any docs.

## Don't

- Don't change port numbers, remove ports, or make `--bind` required.
- Don't add `external` networks, labels, or any dependency on lpcp to the project. A project must start on a
  machine that has never heard of lpcp.
- Don't let a database or unauthenticated API follow `--bind` onto a network address.
- Don't stop or restart other projects' running stacks without asking.
- Don't edit a real project without being asked. Propose the diff first when the user only asked how it works.
- Don't stage or commit for the user.
