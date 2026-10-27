# Setting up the brickpipe server (Proxmox VM)

This gets you from "Proxmox host with Home Assistant on it" to "a hardened
Debian VM with Docker, a Cloudflare tunnel token and an Access login", ready
for [docs/hosting.md, step "4. Start it"](hosting.md#4-start-it). It replaces
steps 1–3 of that file with scripts.

What you end up with:

| Thing | Value (defaults; change them if your answers differ) |
|---|---|
| Proxmox host | `pve.snaghomes.dev`, 192.168.1.10 |
| New VM | VMID **110**, name `brickpipe`, Debian 13 cloud image, 2 cores, 6 GB RAM, 40 GB disk on `local-lvm`, start at boot |
| VM address | DHCP (default) or a static one you choose, e.g. `192.168.1.40/24` |
| Login | user `chris`, SSH key only, no root login, no passwords |
| Inbound firewall | SSH from `192.168.1.0/24` only. Nothing else. Nothing forwarded on the router. |
| Public access | `bricks.snaghomes.dev` and `nova.snaghomes.dev` through a Cloudflare Tunnel (outbound only) behind Cloudflare Access |

**Leave the Home Assistant VM alone.** Nothing here stops, changes or takes
resources from VM 100 (`gans-home-assistant`). `create-vm.sh` refuses VMID 100
outright and refuses to run if the new VM would leave the host short on
memory.

Commands are marked **[laptop]** (Windows PowerShell), **[pve]** (root shell on
the Proxmox host) or **[vm]** (the new VM).

---

## 0. Before you start

Answer these first. Each one is a script option.

| Question | How to find out | Option |
|---|---|---|
| How much RAM does the host have? | **[pve]** `free -h` (the `total` column) | `--memory` (default 6144) |
| Which storage holds VM disks? | **[pve]** `pvesm status --content images` | `--storage` (default `local-lvm`) |
| Is VMID 110 free? | **[pve]** `qm list` and `pct list` | `--vmid` (default 110) |
| Static address or DHCP? If static, which free IP? | Router's DHCP range / lease list. HA is .30, the host .10. | `--ip dhcp` or `--ip 192.168.1.40/24` |
| Gateway and DNS of the new router | Router admin page | `--gateway`, `--dns` (default 192.168.1.1) |
| Is the LAN still 192.168.1.0/24? | **[laptop]** `ipconfig` | bootstrap `--lan-cidr` |

Memory rule of thumb: host total − 4 GB (HA) − 6 GB (brickpipe) should be at
least 2 GB, i.e. a host with 12 GB or more. On a smaller host use `--memory 4096`
(Nova runs; builds are slower).

### Make an SSH key on the laptop

**[laptop]**

```powershell
# Skip if $env:USERPROFILE\.ssh\id_ed25519.pub already exists.
ssh-keygen -t ed25519 -C "chris@laptop"
# Accept the default path. Use a passphrase.
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
```

Optional, so the passphrase is asked once per login (run PowerShell as admin once):

```powershell
Get-Service ssh-agent | Set-Service -StartupType Automatic; Start-Service ssh-agent
ssh-add $env:USERPROFILE\.ssh\id_ed25519
```

**Done when:** `Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub` prints one
line starting `ssh-ed25519`.
**Undo:** delete `id_ed25519` and `id_ed25519.pub` from `%USERPROFILE%\.ssh`.

---

## 1. Create the VM (create-vm.sh on the Proxmox host)

**[laptop]** copy your *public* key to the host:

```powershell
scp $env:USERPROFILE\.ssh\id_ed25519.pub root@192.168.1.10:/root/chris.pub
```

**[pve]** get the script and read it before running it:

```bash
cd /root
curl -fsSLO https://raw.githubusercontent.com/cgans/brickpipe/main/deploy/proxmox/create-vm.sh
# (before this is merged, use .../brickpipe/deploy/server/deploy/proxmox/create-vm.sh)
less create-vm.sh
chmod +x create-vm.sh
./create-vm.sh --help
```

**Dry run first** (the default; it changes nothing, but runs the read-only
checks and lists your storages):

```bash
./create-vm.sh --ssh-key-file /root/chris.pub --ip 192.168.1.40/24
```

Read the output: storage list, the memory table (VM 100 should be listed with
4096 MB), and every `qm` command it would run. Fix any `ERROR:` by changing
options. Then do it for real by adding `--apply`:

```bash
./create-vm.sh --ssh-key-file /root/chris.pub --ip 192.168.1.40/24 --apply
```

It downloads the Debian cloud image and Debian's `SHA512SUMS`, verifies the
image (deletes it and stops on mismatch), creates VM 110 with cloud-init,
resizes the disk to 40 GB and starts it. Re-running after a failure is safe:
a verified cached image is reused, and an existing VMID is never touched.

About the guest agent: if `local` has the `snippets` content type, the VM
installs `qemu-guest-agent` on first boot (so Proxmox shows its IP). If not,
the script says so and does **not** change your storage config;
`bootstrap.sh` installs the agent in step 2 instead.

**Done when:**
- **[pve]** `qm status 110` says `status: running`, and `qm status 100` still says `running`.
- **[pve]** `qm config 110 | grep -E 'onboot|agent|cpu|memory'` shows `onboot: 1`, `agent: enabled=1`, `cpu: host`, your memory.
- With DHCP: after a minute, `qm guest cmd 110 network-get-interfaces | grep -w ip-address`
  (or the router's lease list, name `brickpipe`) gives the address. Reserve it
  on the router so it never moves.

**Undo** (VM 110 only; the script prints this too):
`qm stop 110; qm destroy 110 --purge 1 --destroy-unreferenced-disks 1`

---

## 2. First login and bootstrap.sh (inside the VM)

**[laptop]**

```powershell
ssh chris@192.168.1.40
```

Answer `yes` to the host-key question the first time. No password is asked;
your key logs you in.

**[vm]**

```bash
curl -fsSLO https://raw.githubusercontent.com/cgans/brickpipe/main/deploy/server/bootstrap.sh
less bootstrap.sh
sudo bash bootstrap.sh --help
sudo bash bootstrap.sh            # add --lan-cidr / --timezone if yours differ
```

What it does, in order, each step skipped if already done:

1. `apt-get update` + `upgrade`; installs git, curl, nftables, qemu-guest-agent, unattended-upgrades.
2. Timezone `America/Chicago`.
3. Docker CE + compose v2 from Docker's own apt repo (`download.docker.com`).
   It checks the repo key's fingerprint (`9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`)
   and stops if it does not match. No `curl | sh`.
4. Adds you to the `docker` group and creates `~/lego`.
5. Unattended upgrades for Debian (security + point releases). Docker is
   *not* auto-upgraded, so containers never restart unexpectedly.
6. Firewall: an nftables table `inet brickpipe_fw` that drops all inbound
   traffic except SSH (and ping) from the LAN. It refuses to install if your
   current SSH session comes from outside `--lan-cidr`, so you cannot cut
   yourself off. It lives in its own table and never flushes Docker's rules.
7. SSH hardening (no passwords, no root) — **only** if your
   `~/.ssh/authorized_keys` has a valid key; otherwise it warns and skips.

It ends with a `[PASS]/[FAIL]` summary. Then log out and back in (for the
`docker` group) and get the code:

```bash
exit
```

**[laptop]** `ssh chris@192.168.1.40`, then **[vm]**:

```bash
cd ~/lego
git clone https://github.com/cgans/brickpipe.git
git clone --branch v0.6.0 https://github.com/anteloc/ldraw-nova.git
git clone --branch v0.6.0 https://github.com/anteloc/ldraw-nova-docker.git
docker run --rm hello-world
```

**Done when:**
- every line of the bootstrap summary is `[PASS]`;
- `docker run --rm hello-world` works **without** sudo;
- `ls ~/lego` shows `brickpipe  ldraw-nova  ldraw-nova-docker`;
- from a **second** PowerShell window, `ssh chris@192.168.1.40` still works
  (keep the first one open until this succeeds);
- `ssh -o PubkeyAuthentication=no chris@192.168.1.40` is refused with
  `Permission denied (publickey)`;
- `sudo nft list table inet brickpipe_fw` shows the rules.

**Undo:** firewall — `sudo systemctl disable --now brickpipe-firewall`
(removes the table). SSH hardening — `sudo rm /etc/ssh/sshd_config.d/10-brickpipe-hardening.conf && sudo systemctl reload ssh`.
Everything else — destroy and recreate the VM (step 1 undo).

---

## 3. Cloudflare Zero Trust: tunnel, login, hostnames

All in <https://one.dash.cloudflare.com> (Zero Trust, free plan), account that
owns `snaghomes.dev`. Do the Access login **before** the hostnames, so the
hostnames are never reachable without it. Menu names move around in the
Cloudflare dashboard; the older names are in brackets.

### 3a. Create the tunnel and copy its token

1. **Networks > Connectors > Cloudflare Tunnels** (older: *Networks > Tunnels*) **> Create a tunnel**, type **Cloudflared**, name `brickpipe`.
2. On the "install connector" page, copy the **token**: the long string after
   `--token` in any of the install commands. Do **not** run the install
   command; the `cloudflared` container uses the token.
3. Put the token in your password manager. It goes into `deploy/.env` as
   `TUNNEL_TOKEN` in hosting.md "Start it". Never commit it or paste it in chat.

**Done when:** the tunnel `brickpipe` is listed (status *Inactive* or *Down* is
expected until the stack runs).
**Undo:** delete the tunnel. If the token leaked: tunnel **> Configure > Refresh token**
(old token stops working).

### 3b. One Access application for both hostnames

1. **Access controls > Applications** (older: *Access > Applications*) **> Add an application > Self-hosted**.
2. Name `brickpipe`. Add **two** public hostnames (destinations) in the same
   application: `bricks.snaghomes.dev` and `nova.snaghomes.dev`.
3. **Session duration:** `1 week` (one login per week per device; shorter if
   you prefer).
4. Identity provider / login method: **One-time PIN** (built in; Cloudflare
   emails a code). If One-time PIN is not listed, add it under
   **Settings > Authentication > Login methods**.
5. Policy: name `family`, action **Allow**, include **Emails** = the exact
   addresses allowed (yours first). Save.

**Done when:** the application lists both hostnames and the `family` policy.
**Undo:** delete the application. (Do not do this while the tunnel's hostnames
exist — Nova would be public.)

### 3c. Public hostnames on the tunnel

Tunnel `brickpipe` **> Edit > Public hostnames** (newer: *Published application routes*) **> Add**:

| Subdomain | Domain | Service type | URL |
|---|---|---|---|
| `bricks` | `snaghomes.dev` | HTTP | `brickpipe:8766` |
| `nova` | `snaghomes.dev` | HTTP | `nova:8000` |

`brickpipe` and `nova` are the compose service names; the `cloudflared`
container resolves them on the compose network. Cloudflare creates the DNS
CNAMEs itself.

**Done when:** in a private browser window, `https://bricks.snaghomes.dev` and
`https://nova.snaghomes.dev` both show the Cloudflare Access login page, an
email **on** the list gets a code, and an email **not** on the list does not
get in. (After login you get a Cloudflare error page until the stack runs —
expected.)
**Undo:** delete the two public hostnames (and their DNS records under
the `snaghomes.dev` DNS page if Cloudflare leaves them behind).

---

## 4. Backups of the brickpipe VM (Proxmox)

**[pve]** one scheduled vzdump job for VMID 110 only:

```bash
pvesh create /cluster/backup \
  --id brickpipe-weekly \
  --vmid 110 \
  --schedule 'sun 03:15' \
  --storage local \
  --mode snapshot \
  --compress zstd \
  --prune-backups keep-last=3 \
  --comment 'brickpipe VM only' \
  --enabled 1
```

(Same thing in the UI: **Datacenter > Backup > Add**, Selection mode
*Include selected VMs*, tick only **110**.) Expect roughly 6–10 GB per backup,
mostly Nova's image. Weekly with 3 kept is about 30 GB on the 512 GB disk.

Run one now to test it: `vzdump 110 --storage local --mode snapshot --compress zstd`.

Notes:
- **Home Assistant backups are separate.** This job does not include VM 100;
  keep HA's own backups (HA *Settings > System > Backups*, or your existing
  Proxmox job for VM 100) as they are.
- Backups on `local` live on the **same SSD** as the VMs. They protect against
  mistakes, not against the disk dying. Copy them elsewhere now and then
  (`/var/lib/vz/dump/vzdump-qemu-110-*`), or add a NAS/USB storage later.
- What matters most is small: `deploy/.env` (keep a copy in your password
  manager) and the `brickpipe-data` volume. Everything else can be rebuilt.

**Done when:** `pvesh get /cluster/backup/brickpipe-weekly` shows `vmid: 110`,
and the test run left a `vzdump-qemu-110-*.vma.zst` in `pvesm list local --content backup`.
**Undo:** `pvesh delete /cluster/backup/brickpipe-weekly`; remove test backups
with `pvesm free <volid>`.

---

## 5. Hand-off

Continue with [docs/hosting.md, step "4. Start it"](hosting.md#4-start-it) on
the VM (in `~/lego/brickpipe`), using the tunnel token from 3a and the
hostnames `bricks.snaghomes.dev` / `nova.snaghomes.dev`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `create-vm.sh`: `not enough memory` | Host RAM too small for HA (4 GB) + 6 GB + 2 GB reserve | `free -h`; use `--memory 4096`. Use `--force-memory` only if you know the numbers. |
| `create-vm.sh`: `storage 'local-lvm' is not active or does not hold VM disks` | Different storage name | Pick a name from the printed `pvesm status` list; `--storage NAME`. |
| `create-vm.sh`: `VMID 110 already exists` | Earlier run, or used by another VM/CT | Check with `qm config 110`. If it is a failed brickpipe attempt, run the undo; else `--vmid 111`. |
| `checksum mismatch` | Download cut off, or Debian published a new build mid-download | Re-run; it re-fetches both. If it repeats, stop and check <https://cloud.debian.org/images/cloud/>. |
| `ssh` times out | Wrong IP / VM not up yet / not on the LAN | `qm status 110`; `qm terminal 110` to watch boot (Ctrl+O exits); check the router lease. |
| `Permission denied (publickey)` | Wrong key or user | `ssh -v chris@IP`; on **[pve]** `qm cloudinit dump 110 user` shows the key and user it was given. |
| Locked out of SSH after bootstrap | `--lan-cidr` wrong after a network change | **[pve]** `qm set 110 --cipassword 'temp'` then `qm reboot 110`, log in with `qm terminal 110`, `sudo systemctl stop brickpipe-firewall`, fix, re-run bootstrap with the right `--lan-cidr`. Remove the password after: `sudo passwd -l chris`. |
| bootstrap: `Docker repo key fingerprint is ...` | Docker rotated the key, or interception | Stop. Compare with <https://docs.docker.com/engine/install/debian/>. |
| bootstrap: `[FAIL] guest agent` | Agent option off in Proxmox | **[pve]** `qm set 110 --agent enabled=1`, then `qm reboot 110`. |
| `docker: permission denied` | Group not active in this session | Log out and back in. |
| Access login works, then Cloudflare `502` / "Bad gateway" | Stack not started, or wrong service URL | Expected before hosting.md "Start it". After: `docker compose ... ps`; URLs must be `brickpipe:8766` and `nova:8000`. |
| No Access login page, site opens directly | Hostname missing from the Access application | Add it to the application in 3b. Stop the stack until fixed. |
