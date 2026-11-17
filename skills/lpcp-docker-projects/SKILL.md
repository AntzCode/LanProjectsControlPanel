---
name: lpcp-docker-projects
description: Make a docker/compose project compatible with LanProjectsControlPanel (lpcp), so several projects can run at once on one machine without port conflicts. Use when asked to make a project run side by side with others, to fix port collisions between local projects (web, database or other ports), to add `--bind`, `--lan` or `--host` options to a project's `./dev`, to add `./dev launch` or `./dev config.*` keys such as `config.launchers` (launch buttons in the panel), to register a project with the control panel, or when you see a repo with a `./dev up` script and ports published on 127.0.0.1 or 0.0.0.0.
---

# Making a docker project work with LanProjectsControlPanel (lpcp)

lpcp is a separate repo with its own `./dev`. It keeps a registry of projects and gives each one its own
address (127.0.0.2, 127.0.0.3, ..., or the LAN address). `./dev up [site...]` in the control panel starts each
project by running **that project's** `./dev up --bind=<address>`. Each project keeps its usual port numbers,
and they don't collide because a port belongs to an address. A Caddy proxy on 127.0.0.1 then serves
`https://<site>.localhost`. A project only has to accept `--bind`. Everything else is lpcp's job.

The project must keep working on its own: a colleague who clones it and runs `./dev up` without lpcp gets a
working project on this machine, with no lpcp installed.

## The contract

Where a project listens. Exactly one of these applies:

| `./dev up ...`        | Published ports are on | Reachable from                                          |
|-----------------------|------------------------|---------------------------------------------------------|
| (no option)           | `127.0.0.1`            | this machine only. **Always the default.**              |
| `--bind=IP`           | `IP`                   | depends on IP: 127.x is this machine, the LAN IP is the LAN |
| `--lan`               | `0.0.0.0`              | every device on the network (phones, tablets)           |

The name it answers to, independent of the address:

| `./dev up ...`        | The project answers to                                                          |
|-----------------------|----------------------------------------------------------------------------------|
| (no option)           | its usual name (`localhost`, or what its config says)                            |
| `--host=NAME`         | NAME: `./dev up --host phuppi.loc` is reachable at `http://phuppi.loc` once the hosts file points `phuppi.loc` at the address it listens on |

Rules:

1. **Plain `./dev up` publishes on 127.0.0.1 only. Never 0.0.0.0.** Every published port is
   `"${BIND:-127.0.0.1}:<port>:<port>"`, whatever the compose file had before. 0.0.0.0 (every address) happens
   only with `--lan`.
2. `--bind=IP` (also `--bind IP`) publishes the same ports on IP instead. **Port numbers never change.**
3. `--lan` publishes on 0.0.0.0. `--bind` and `--lan` together is an error.
4. `--host=NAME` (also `--host NAME`) makes the app answer to NAME: its web server's site address or
   `server_name`, its base URL, allowed hosts and CORS origins, and the TLS certificate if it serves HTTPS
   itself. It doesn't change the address. It combines with `--bind` or `--lan`.
5. `./dev down` stops the project; `./dev logs` follows its logs (optional).
6. Optional: when `LPCP_URL` is set (lpcp sets `LPCP_URL`, `LPCP_HOST`, `LPCP_BIND`, `LPCP_PROJECT` when it
   starts a project), use it as the app's public base URL and for CORS/allowed origins.
7. `./dev launch` opens the project's default entry point, normally its URL in the browser;
   `./dev launch --app=NAME` opens another one. `./dev config.launchers` lists them, and lpcp shows one launch
   button per launcher on the project's entry. See "Config API" and "Launchers" below.

About names and the hosts file:
- `*.localhost` names need no hosts entry: they always resolve to 127.0.0.1. So `./dev up --host
  phuppi.localhost` works with no setup. It doesn't combine with `--bind=127.0.0.5`, because the name still
  resolves to 127.0.0.1.
- Any other name (`phuppi.loc`, `phuppi.test`) needs a line in `/etc/hosts` pointing it at the address the
  project listens on: `127.0.0.1 phuppi.loc`, or `127.0.0.5 phuppi.loc` with `--bind=127.0.0.5`. For other
  devices with `--lan`, their hosts file (or DNS) points the name at this machine's LAN address.
- Editing `/etc/hosts` needs sudo. **Never edit it yourself.** Give the user the exact line to add.

## Steps

1. **Read first.** Open the project's `./dev` (or whatever starts it) and its compose file(s). List every
   published port and the address it's on, and where the app's hostname is configured (web server config,
   `.env`, app settings). Note the web entry point's port (lpcp's `port`, default 80).
2. **Compose:** on each published port, replace the address (`127.0.0.1:`, `0.0.0.0:`, or none) with
   `${BIND:-127.0.0.1}:`. The default is **always 127.0.0.1**, also for ports that had no address or 0.0.0.0
   before (those were open to the whole network). If any port used to be on every address, tell the user: plain
   `./dev up` is now this-machine-only, and `./dev up --lan` gives the old network access. Don't rename
   services, add networks or labels, or change port numbers.
3. **Hostname:** take the app's hostname from a variable with the old value as default, for example
   `SITE_HOST: ${SITE_HOST:-localhost}` in compose, used as the Caddy site address or nginx `server_name`, and in
   the app's base URL and allowed hosts. If the project already has a hosts variable (`DEV_HOSTS`, `APP_URL`,
   ...), have `--host` set that instead of adding a new one.
4. **`./dev`:** add the option parsing below to the existing `up` handling, keeping everything else it does. If
   it already has `--lan` or other flags, merge into its existing parsing and keep them working in any order.
   If it has no `--lan`, add it. If the project has no `./dev`, don't invent one unless asked: register it with
   a custom command (step 7). After starting, print the URL: `http://<host or address>[:port]`.
5. **Launchers:** add `launch` and `config.launchers` to `./dev` (see "Launchers" below). Choose the
   launchers from what the project serves: the site itself, a mail catcher, an admin area, a data or API
   browser, docs. Give each a `class` (web, mail, db, logs, config, tool) so its icon says what it opens. The
   first is the default. If the project has no `./dev`, skip this: lpcp falls back to its own URL.
6. **Private ports** (database, internal API, mail catcher) use `${PRIVATE_BIND:-127.0.0.1}` instead of
   `${BIND...}`. `./dev` sets PRIVATE_BIND only when `--bind` is another loopback address (127.*), so private
   ports move with the project under lpcp (no collisions) but never onto the network with `--lan` or a LAN
   `--bind`. Tell the user which ports you treated as private.
7. **Register** (from the lpcp repo): `./dev add <name> <path-to-project>`, adding `--port N` if the web port
   isn't 80.
   - If the project accepts `--host`, also add `--up './dev up --bind={bind} --host={host}'`, so it answers to
     its `<name>.localhost` name behind the proxy.
   - For a project with real hostnames, its own TLS, and public DNS pointing at this machine:
     `./dev add <name> <path> --bind lan --port 443 --no-proxy --url https://<its name>`.
   - For a project with no `./dev`:
     `./dev add blog ~/blog --up 'BIND={bind} docker compose up -d --wait' --down 'docker compose stop'`.
   - `./dev ls` shows the result.
8. **Verify** (see below). Report what you ran and saw.

## Option parsing for a bash `./dev`

Put it after the command word has been read, before the compose call:

```bash
bind="" lan="" host=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bind=*) bind="${1#--bind=}"; [[ -n "$bind" ]] || { echo "--bind needs an address" >&2; exit 2; }; shift ;;
    --bind)   bind="${2:?--bind needs an address}"; shift 2 ;;
    --host=*) host="${1#--host=}"; [[ -n "$host" ]] || { echo "--host needs a name" >&2; exit 2; }; shift ;;
    --host)   host="${2:?--host needs a name}"; shift 2 ;;
    --lan)    lan=1; shift ;;
    *) break ;;
  esac
done
if [[ -n "$bind" && -n "$lan" ]]; then echo "--bind and --lan can't be used together" >&2; exit 2; fi
# Where: 127.0.0.1 unless --bind or --lan says otherwise (compose: "${BIND:-127.0.0.1}:80:80").
if [[ -n "$bind" ]]; then export BIND="$bind"; elif [[ -n "$lan" ]]; then export BIND=0.0.0.0; fi
# Private ports (compose: "${PRIVATE_BIND:-127.0.0.1}:5432:5432") follow --bind only to another loopback address.
if [[ "$bind" == 127.* ]]; then export PRIVATE_BIND="$bind"; fi
# Which name: the app's usual one unless --host says otherwise (compose: ${SITE_HOST:-localhost}).
if [[ -n "$host" ]]; then export SITE_HOST="$host"; fi
if [[ ( "$host" == localhost || "$host" == *.localhost ) && -n "$bind" && "$bind" != 127.0.0.1 ]]; then
  echo "--host $host always resolves to 127.0.0.1, so it can't reach $bind: use another name (and a hosts entry)" >&2
  exit 2
fi
```

A full reference script and compose file are in `examples/` in the lpcp repo.

## Config API

`./dev config.<key>` is how a project describes itself to lpcp. `config.launchers` is the first key; later
keys will cover things like branding color, logo and short name. Every key follows the same rules:

- It prints one JSON value on stdout and exits 0. Nothing else goes to stdout (messages go to stderr).
- It is read-only and quick: it works when the project is stopped and needs no docker, network or tool
  beyond bash.
- An unknown `config.*` key prints an error on stderr and exits 2. lpcp treats any non-zero exit (including
  from an older `./dev` that has no config commands) as "not provided" and uses its own default.
- Key names are lowercase words joined with `-` (`config.launchers`, `config.short-name`).
- Add only the keys the project actually provides. Don't add empty or placeholder keys.

## Launchers

A launcher is one way to open the project: its site in the browser, an admin area, a data browser, a docs
page. lpcp runs `./dev config.launchers` in the project's directory and shows one round icon per entry. An
icon runs `./dev launch --app=<app>` there, with the `LPCP_*` variables set as for `./dev up`.

**`./dev config.launchers`** prints a JSON array:

```json
[{"app":"site","title":"Open dev.scripture.tips in the browser"},
 {"app":"mail","title":"Open the mail catcher","class":"mail"},
 {"app":"data","title":"Open the data browser","class":"db"}]
```

- `app`: a short id (lowercase letters, digits, `-`), unique within the project. It is the value `--app`
  takes.
- `title`: the icon's tooltip, a short instruction in the project's UI language.
- `class` (optional, default `web`): what it opens, which picks the icon's colour and glyph, so the user can
  tell the icons apart before reading a tooltip. Choose by what the launcher opens, from this fixed set:

  | class | icon | use it for |
  |---|---|---|
  | `web` | blue globe | the site itself (the default) |
  | `mail` | orange envelope | a mail catcher (Mailpit) or webmail |
  | `db` | purple cylinder | a database or data browser (Adminer, pgAdmin, an API explorer) |
  | `logs` | teal lines | logs, docs, anything the user reads |
  | `config` | amber gear | settings or an admin area |
  | `tool` | grey wrench | anything else: a shell, a profiler, another dev tool |

  Don't invent class names: an unknown one is drawn as a `tool` and `./dev open <name> --list` flags it.
  Leave `class` out for ordinary web pages.
- At least one entry. **The first is the default**, the one plain `./dev launch` opens.

**`./dev launch [--app=NAME]`** (also `--app NAME`) opens that launcher, or the first one without `--app`.
- The URL is `$LPCP_URL` plus the launcher's path when `LPCP_URL` is set; otherwise it is the URL plain
  `./dev up` prints.
- It prints the URL, then opens it with `xdg-open` (Linux) or `open` (macOS), detached. With neither
  available it only prints it.
- `--print` prints the URL without opening anything (for scripts and for verifying).
- An unknown `--app` exits 2 and names the valid ones.
- It doesn't start the project. If the project is down, it says so on stderr and exits 1.
- A launcher may run something other than a browser (a desktop tool, a terminal). It still has an `app` and
  a `title` and is started the same way.

Keep the launchers in one place in `./dev`, so the list and what `launch` opens can't disagree:

```bash
# One launcher per line: app|title|path|class. The first line is the default. No "|" inside a title.
# The class is optional (web) and goes last, so a line without it still works.
launchers() {
  cat <<'EOF'
site|Open dev.scripture.tips in the browser|/
mail|Open the mail catcher|/mail/|mail
data|Open the data browser|/data/|db
EOF
}
base_url() { echo "${LPCP_URL:-http://${SITE_HOST:-localhost}}"; }   # what plain ./dev up prints

json_str() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

config_launchers() {
  local first=1 app title path kind
  printf '['
  while IFS='|' read -r app title path kind; do
    [[ -n "$app" ]] || continue
    (( first )) || printf ','
    first=0
    printf '{"app":%s,"title":%s' "$(json_str "$app")" "$(json_str "$title")"
    [[ -z "$kind" ]] || printf ',"class":%s' "$(json_str "$kind")"
    printf '}'
  done < <(launchers)
  printf ']\n'
}

cmd_launch() {
  local want="" print="" app title path kind url=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --app=*) want="${1#--app=}"; shift ;;
      --app)   want="${2:?--app needs a name}"; shift 2 ;;
      --print) print=1; shift ;;
      *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
  done
  while IFS='|' read -r app title path kind; do
    [[ -n "$app" ]] || continue
    if [[ -z "$want" || "$app" == "$want" ]]; then url="$(base_url)${path}"; break; fi
  done < <(launchers)
  if [[ -z "$url" ]]; then
    echo "unknown app '$want'; choose from: $(launchers | cut -d'|' -f1 | paste -sd' ')" >&2; exit 2
  fi
  echo "$url"
  [[ -n "$print" ]] && return 0
  if ! curl -ks -o /dev/null --max-time 3 "$url"; then echo "the project isn't running: ./dev up first" >&2; exit 1; fi
  if command -v xdg-open >/dev/null; then xdg-open "$url" >/dev/null 2>&1 &
  elif command -v open >/dev/null; then open "$url"
  fi
}
```

In the command dispatch, next to `up` and `down`:

```bash
  launch)           shift; cmd_launch "$@" ;;
  config.launchers) config_launchers ;;
  config.*)         echo "unknown config key: ${1#config.}" >&2; exit 2 ;;
```

If the project's `./dev` is not bash, implement the same contract in its language.

## Verify

Do all of these. Leave nothing running that wasn't running before.

1. **Standalone default:** `./dev up` with no options. Every published port is on 127.0.0.1 and none is on
   0.0.0.0 (`ss -ltn`). `./dev down`.
2. **Moved:** `./dev up --bind=127.0.0.99`. Every published port is on 127.0.0.99 and nothing is on 127.0.0.1
   (except private ports deliberately kept on loopback). `./dev down`.
3. **LAN:** `./dev up --lan` publishes the web ports on 0.0.0.0, with private ports still on 127.0.0.1.
   `./dev down`. `./dev up --bind=127.0.0.99 --lan` and `./dev up --bind=127.0.0.99 --host=x.localhost` each
   exit non-zero with a message.
4. **Host:** `./dev up --host <name>.localhost`, then `curl -s http://<name>.localhost[:port]/` (or `curl -k
   https://...`) gets the app, not a wrong-host error or redirect. The printed URL uses the name. Don't touch
   `/etc/hosts`: `.localhost` needs no entry. For another name, test without the hosts file:
   `curl --resolve <name>:<port>:127.0.0.1 http://<name>:<port>/`. `./dev down`.
5. **Two at once:** from the lpcp repo, `./dev up <name> <another>`, then `curl -k https://<name>.localhost`
   (or the project's own URL), and `./dev ls`. If `./dev up` reports a 0.0.0.0 holder, something was started
   with `--lan`: ask the user before stopping it.
6. **Launchers**, with the project stopped: `./dev config.launchers | python3 -m json.tool` is valid JSON with
   at least one entry and nothing else on stdout, and every `class` is one of web, mail, db, logs, config,
   tool (or absent); `./dev config.nope` exits 2. `./dev launch --print` and
   `./dev launch --print --app=<app>` for each launcher print the expected URLs; `--app=nope` exits 2;
   `./dev launch` (without `--print`) exits 1 with a message. With the project up, `curl -ks` each printed URL
   gets the app. Don't open browser windows to test.
7. `git diff` in the project should show only the port addresses, the hostname variable, the option parsing,
   the launch and config commands, and docs.

## Don't

- Don't default any port to 0.0.0.0, or publish a port without an address (`"80:80"` means every address).
  0.0.0.0 is only for `--lan`.
- Don't change port numbers, remove ports, or make `--bind` or `--host` required.
- Don't edit `/etc/hosts` or other system files. Give the user the line to add.
- Don't add `external` networks, labels, or any dependency on lpcp to the project. A project must start on a
  machine that has never heard of lpcp.
- Don't let a database or unauthenticated API follow `--lan` or `--bind` onto a network address.
- Don't stop or restart other projects' running stacks without asking.
- Don't edit a real project without being asked. Propose the diff first when the user only asked how it works.
- Don't print anything but the JSON value on stdout from a `config.*` command, and don't make one start
  containers, call the network, or depend on lpcp.
- Don't stage or commit for the user.
