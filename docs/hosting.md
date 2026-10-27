# Hosting brickpipe for the family

One home server runs LDraw Nova, brickpipe and a Cloudflare tunnel. Family
members open `https://bricks.<your domain>` from anywhere, sign in with a
one-time code sent to their email, and use the page exactly as on a laptop.
Nothing is opened on the router and nothing is reachable without that login.

This guide covers the stack itself. Setting up the machine underneath it
(a Debian VM on Proxmox, Docker, the firewall, the Cloudflare tunnel and
the Access login) is in [server-setup.md](server-setup.md).

## What runs where

```mermaid
flowchart LR
    F["Family browser"] -->|https, after email code| CF["Cloudflare<br/>Access policy + tunnel"]
    CF --> T["cloudflared<br/>(container)"]
    T --> B["brickpipe :8766"]
    T --> N["Nova :8000"]
    B -->|internal network| N
    B -.->|read only| D["ldraw-nova/data<br/>part database, shadow library"]
    N --> G["ldraw-nova-docker/data<br/>chats, generated models"]
    N --> C["nova-config volume<br/>agents, keys"]
```

- **brickpipe** runs *locked*: the server decides where Nova and the part
  libraries are; the page hides those settings. Browsers only choose what
  goes into a run. The server can hold one Rebrickable key for everyone.
- **Nova** has no login of its own and uses your Anthropic API key, so it
  is never published to the network; the tunnel is
  the only way in, and the Access policy in front of the tunnel is the login.
- Links from brickpipe into Nova (the chat, the 3D viewer) use the public
  hostname, so they work from outside.
- Everyone shares one Nova: all chats and all runs are visible to all.
- **Whose AI account pays.** Nova's default agent (your Anthropic API
  key, step 5) is what runs unless a person brings their own: under
  Settings > AI account they can switch from "Use <host>'s AI account" to
  "Use my own key", pick a provider and model, and paste a key. brickpipe
  registers it in Nova as a model entry of its own; Nova keeps the key in
  its settings store on the server, and that person's runs are billed to
  it. "Forget" removes the entry again. Set `BRICKPIPE_HOST_NAME=Chris` so
  the toggle names you.

## 0. Before you start

- Get an Anthropic API key for the server and cap its monthly spend in
  the Anthropic console (step 5). Nova spends it; a login in front is not a
  budget.
- Pick two hostnames under your domain, e.g. `bricks.example.com` and
  `nova.example.com`.
- The stack needs about 4 GB RAM and 25 GB disk (Nova's image is 5 GB, the
  LDraw library 150 MB, runs are small). Each container has a memory limit
  you can change in `deploy/.env` (`NOVA_MEM_LIMIT`, default `3g`;
  `BRICKPIPE_MEM_LIMIT`, `1g`; `TUNNEL_MEM_LIMIT`, `256m`).

## 1–3. The server, Docker and Cloudflare

Follow [server-setup.md](server-setup.md). It ends with Docker running, the
tunnel token in hand, both hostnames routed (`brickpipe:8766` and
`nova:8000`) and one Access application covering both, and the code checked
out side by side:

```bash
mkdir -p ~/lego && cd ~/lego
git clone https://github.com/cgans/brickpipe.git
git clone --branch v0.6.0 https://github.com/anteloc/ldraw-nova.git
git clone --branch master https://github.com/cgans/ldraw-nova-docker.git
```

Nova's web app comes from the `cgans/ldraw-nova-docker` fork (it uses far
fewer tokens per follow-up turn); the toolkit stays at upstream `v0.6.0`.
`deploy/update.sh` clones whichever of the two is missing, and moves an
older clone of upstream `ldraw-nova-docker` over to the fork.

## 4. Start it

```bash
cd ~/lego/brickpipe
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
nano deploy/.env     # hostnames, TUNNEL_TOKEN, ANTHROPIC_API_KEY, BRICKPIPE_HOST_NAME
deploy/update.sh
```

`update.sh` pulls the three repos, builds, starts everything and waits until
Nova and brickpipe report healthy (the tunnel starts only after both are).
The first build downloads about 5 GB for Nova and takes a while; brickpipe's
first start then downloads the LDraw library (about 150 MB). To watch it:

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env logs -f brickpipe
```

which ends with
`brickpipe 0.x is at http://localhost:8766 (listening on 0.0.0.0) [locked: hosted mode]`.

**Done when:**
- `docker compose -f deploy/docker-compose.yml --env-file deploy/.env ps`
  shows `nova` and `brickpipe` as `healthy` and `tunnel` running.
- `https://nova.example.com` opens Nova (after the Access login).
- `https://bricks.example.com` opens brickpipe; the header says `v0.x`;
  Settings shows the "This brickpipe is hosted" note and no Nova or library
  fields; under Build the status line reads "Nova is up".

## 5. AI account

On this shared server Nova's **default** agent runs on **your Anthropic API
key** (the host's key). Family members who would rather pay for their own
runs switch to their own key in brickpipe; everyone else uses yours.

1. In the [Anthropic console](https://platform.claude.com/settings/keys)
   create a key for this server and **set a monthly spend limit** on it
   (Settings > Limits). Nova spends it; the login in front is not a budget.
2. Put it in `deploy/.env` as `ANTHROPIC_API_KEY=...` and run
   `deploy/update.sh` (the container is recreated with the key).
3. Nova does not pick the key up by itself; it needs an agent that reads it.
   Once: open `https://nova.example.com` > **Settings**, under Anthropic
   click **+ Add agent**, choose a model preset, set authentication to
   **API key (env var)** with the variable `ANTHROPIC_API_KEY` (the form
   defaults to Browser login for Anthropic; change it), click **Test**, save,
   then **Make default**. The agent only stores the variable's name; the key
   itself stays in `deploy/.env`.

**Do not use "Sign in with Claude" on this server.** That would put your
personal Claude account behind a page the whole family uses: their runs
would count against your plan and your account. Leave Nova's Claude and
ChatGPT sign-ins disconnected here.

**Family members' own keys.** In brickpipe, **Settings > AI account**, they
switch "Use Chris's AI account" (the name comes from `BRICKPIPE_HOST_NAME`)
to "Use my own key", pick Anthropic and a model, and paste their key.
brickpipe registers it in Nova as an agent of its own, and their runs are
billed to them. "Forget" removes it.

**Persistence.** Nova's agents, including those family keys, live in the
`brickpipe_nova-config` volume (mounted at `/config`, readable only by Nova's
server process, not by its agents). They survive restarts, rebuilds and
`update.sh`. Only removing the volume (`docker compose down -v`,
`docker volume rm`) loses them; `backup.sh` keeps a copy. To change your key,
edit `deploy/.env` and run `update.sh`; the agent keeps working.

**Done when:** Nova's Settings shows your Anthropic agent with the
**Default** badge, and a test run from a prompt in brickpipe finishes with
"Connectivity was checked" in its report (proves the key works and the
library and Nova's database are mounted).

## 6. Day to day

- **Update everything:** `~/lego/brickpipe/deploy/update.sh`. It
  fast-forwards brickpipe and the Nova fork, keeps the toolkit at `v0.6.0`,
  rebuilds, restarts what changed and removes old images. Safe to re-run;
  it stops without touching anything if a repo has local edits.
- **Back up:** `~/lego/brickpipe/deploy/backup.sh` writes
  `~/lego/backups/brickpipe-backup-<date>-<time>.tar.gz` with brickpipe's
  runs, Nova's chats and models, and Nova's settings (agents and the
  family's keys), and keeps the newest 7
  (`BACKUP_DIR` and `BACKUP_KEEP` in `deploy/.env`). Restore steps are at the
  top of the script. **The archive contains the API keys stored in Nova
  (family members' keys included):** it is written readable only by you; keep every copy
  private (never in a shared folder or a public bucket). A nightly cron
  line: `15 3 * * * $HOME/lego/brickpipe/deploy/backup.sh >> $HOME/lego/backups/backup.log 2>&1`
- **Add a family member:** add their email to the Access policy. Nothing to
  restart.
- **Where things are:** runs in the `brickpipe_brickpipe-data` volume,
  Nova's agents and keys in the `brickpipe_nova-config` volume, Nova's
  chats and models under `~/lego/ldraw-nova-docker/data`.
- **Logs:** `docker compose -f deploy/docker-compose.yml --env-file deploy/.env logs --tail 100 nova brickpipe tunnel`
- **Stop everything:** `docker compose -f deploy/docker-compose.yml --env-file deploy/.env down`
  (never add `-v`: that deletes the volumes, Nova's agents and keys included).
- **Newer cloudflared:** change `CLOUDFLARED_VERSION` in `deploy/.env`, then
  `update.sh`.

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Access page appears but then `502` or "unreachable" | tunnel container not connected, or hostname URL wrong | `docker compose logs tunnel`; check the Public Hostname URLs are `brickpipe:8766` and `nova:8000` |
| `update.sh` stops at "waiting until they are healthy", or `ps` shows `unhealthy` | Nova or brickpipe did not start; the tunnel waits for both | `docker compose logs nova brickpipe`; on brickpipe's first start, the library download can take minutes |
| A container keeps restarting, `docker inspect` shows `OOMKilled: true` | memory limit too low | raise `NOVA_MEM_LIMIT` (or the others) in `deploy/.env`, run `update.sh` |
| brickpipe opens but "Nova is not reachable" | Nova still building or crashed | `docker compose logs nova`; wait for the build |
| Runs fail with "Environment variable ANTHROPIC_API_KEY is not set" | key missing from `deploy/.env`, or container not recreated | fill it in, run `update.sh` |
| Runs fail with an authentication or billing error | key revoked, or its spend limit reached | Anthropic console: check the key and limit |
| Runs fail with "no model" or use the wrong account | Nova has no default agent | Nova > Settings: **Make default** on the Anthropic env-var agent (step 5) |
| Nova's agents and family keys are gone after an update | the `brickpipe_nova-config` volume was removed | restore it from a backup (`backup.sh` header), or redo step 5 |
| 3D viewer or "Open the Nova chat" shows a login page or fails | `nova.example.com` is not in the same Access application | add it as a second domain of the brickpipe application |
| Report says the connectivity check was skipped | library download failed or Nova data not mounted | `docker compose logs brickpipe`; check `~/lego/ldraw-nova/data/ldraw-info.db` exists |
| `update.sh` says a repo has local changes | something edited a tracked file there | `git -C <folder> status`; stash or discard the change, re-run |
| Someone outside the family can log in | policy too broad | tighten the Access policy to listed emails |

## Doing it without Proxmox or without Cloudflare

Any Linux box with Docker and the three repos side by side works from
step 4. If you would rather not use Cloudflare, Tailscale is the other sensible option: install it on the server
and on each family device, share the server into their tailnet, drop the
`tunnel` service and change the two `ports` lines to the server's Tailscale
address. There is then no login page; the tailnet membership is the login.
`update.sh` expects the tunnel, so run its `git` and `docker compose` steps
by hand in that setup.
