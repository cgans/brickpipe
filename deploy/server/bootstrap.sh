#!/usr/bin/env bash
# bootstrap.sh -- prepare the brickpipe VM (Debian 12/13) for the Docker stack.
# Run inside the VM:   sudo ./bootstrap.sh [options]
#
# Safe to re-run: every step checks before it changes anything.
# See docs/server-setup.md. --help for the options.
#
# Firewall choice: nftables, in its own table (inet brickpipe_fw) loaded by its
# own systemd unit. nftables is Debian's native firewall and already installed
# in the cloud image; ufw would add another layer on the same kernel tables.
# Using a private table (never 'flush ruleset') means Docker's own NAT/forward
# rules are left alone, so restarting the firewall never breaks container
# networking. Only the input hook is filtered: containers' outbound traffic
# (the Cloudflare tunnel) goes through forward/output and is not affected.
# Debian's stock nftables.service is MASKED: its /etc/nftables.conf starts with
# 'flush ruleset' (and its stop action flushes too), which would wipe this
# table and Docker's rules. Our own unit, brickpipe-firewall.service, loads
# the table at boot instead. To use /etc/nftables.conf later, unmask it and
# remove 'flush ruleset' from it first.

set -euo pipefail

LAN_CIDR=192.168.1.0/24
TIMEZONE=America/Chicago
TARGET_USER=${SUDO_USER:-}
DO_UPGRADE=1
DO_FIREWALL=1
DO_SSH_HARDEN=1
FORCE_FIREWALL=0

usage() {
  cat <<EOF
Usage: sudo ./bootstrap.sh [options]

  --user NAME          user to set up (default: the user who ran sudo${TARGET_USER:+, $TARGET_USER})
  --lan-cidr CIDR      LAN allowed to SSH in (default $LAN_CIDR)
  --timezone TZ        timezone (default $TIMEZONE)
  --skip-upgrade       skip 'apt-get upgrade'
  --skip-firewall      do not install the nftables firewall
  --skip-ssh-hardening do not disable password / root SSH login
  --force-firewall     install the firewall even if your current SSH session
                       is NOT coming from --lan-cidr (you would be cut off)
  -h, --help           this help
EOF
}

die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok: %s\n' "$*"; }

need_arg() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)               need_arg "$@"; TARGET_USER=$2; shift 2 ;;
    --lan-cidr)           need_arg "$@"; LAN_CIDR=$2; shift 2 ;;
    --timezone)           need_arg "$@"; TIMEZONE=$2; shift 2 ;;
    --skip-upgrade)       DO_UPGRADE=0; shift ;;
    --skip-firewall)      DO_FIREWALL=0; shift ;;
    --skip-ssh-hardening) DO_SSH_HARDEN=0; shift ;;
    --force-firewall)     FORCE_FIREWALL=1; shift ;;
    -h|--help)            usage; exit 0 ;;
    *)                    usage >&2; die "unknown option: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n $TARGET_USER ]] || die "cannot tell which user to set up; pass --user NAME"
[[ $TARGET_USER != root ]] || die "--user must be your normal login user, not root"
id "$TARGET_USER" >/dev/null 2>&1 || die "user '$TARGET_USER' does not exist"
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[[ -d $TARGET_HOME ]] || die "home of $TARGET_USER ($TARGET_HOME) not found"

# shellcheck source=/dev/null
. /etc/os-release
[[ ${ID:-} == debian ]] || die "this script is for Debian (found ${ID:-unknown})"
[[ ${VERSION_CODENAME:-} == bookworm || ${VERSION_CODENAME:-} == trixie ]] \
  || warn "tested on Debian 12/13; this is ${VERSION_CODENAME:-unknown}"

# --- IPv4 CIDR helpers -------------------------------------------------------
ip2int() { local IFS=. a b c d; read -r a b c d <<<"$1"; echo $(( (a<<24) | (b<<16) | (c<<8) | d )); }
valid_cidr() {
  [[ $1 =~ ^([0-9]{1,3}(\.[0-9]{1,3}){3})/([0-9]|[12][0-9]|3[0-2])$ ]] || return 1
  local IFS=. o; for o in ${1%/*}; do (( 10#$o <= 255 )) || return 1; done
}
in_cidr() {  # ip cidr
  local ip net len mask
  ip=$(ip2int "$1"); net=$(ip2int "${2%/*}"); len=${2#*/}
  mask=$(( len == 0 ? 0 : (0xFFFFFFFF << (32 - len)) & 0xFFFFFFFF ))
  (( (ip & mask) == (net & mask) ))
}
valid_cidr "$LAN_CIDR" || die "--lan-cidr '$LAN_CIDR' is not an IPv4 CIDR like 192.168.1.0/24"
[[ -e /usr/share/zoneinfo/$TIMEZONE ]] || die "unknown timezone '$TIMEZONE'"

export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

# --- 1. packages --------------------------------------------------------------
step "System packages"
apt-get update
if [[ $DO_UPGRADE -eq 1 ]]; then
  apt-get "${APT_OPTS[@]}" upgrade
fi
apt-get "${APT_OPTS[@]}" install ca-certificates curl gnupg git nftables iproute2 \
  qemu-guest-agent unattended-upgrades apt-listchanges
systemctl enable --now qemu-guest-agent 2>/dev/null \
  || systemctl start qemu-guest-agent 2>/dev/null \
  || warn "qemu-guest-agent did not start (is 'QEMU Guest Agent' enabled on the VM in Proxmox?)"
ok "base packages"

# --- 2. timezone --------------------------------------------------------------
step "Timezone"
if [[ $(timedatectl show -p Timezone --value 2>/dev/null) != "$TIMEZONE" ]]; then
  timedatectl set-timezone "$TIMEZONE"
fi
ok "timezone $TIMEZONE"

# --- 3. Docker CE from Docker's apt repository -------------------------------
step "Docker CE (download.docker.com apt repo)"
DOCKER_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88   # Docker Release (CE deb), published by Docker
for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do
  if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
    warn "removing conflicting Debian package $pkg"
    apt-get -y remove "$pkg"
  fi
done
install -m 0755 -d /etc/apt/keyrings
key_tmp=$(mktemp)
trap 'rm -f "$key_tmp"' EXIT
curl -fsSL https://download.docker.com/linux/debian/gpg -o "$key_tmp"
got_fpr=$(gpg --show-keys --with-colons "$key_tmp" 2>/dev/null | awk -F: '$1=="fpr" {print $10; exit}')
[[ $got_fpr == "$DOCKER_FPR" ]] || die "Docker repo key fingerprint is '$got_fpr', expected $DOCKER_FPR. Not installing."
install -m 0644 "$key_tmp" /etc/apt/keyrings/docker.asc
ok "Docker key fingerprint $DOCKER_FPR"
arch=$(dpkg --print-architecture)
docker_sources="Types: deb
URIs: https://download.docker.com/linux/debian
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: ${arch}
Signed-By: /etc/apt/keyrings/docker.asc"
if [[ ! -f /etc/apt/sources.list.d/docker.sources ]] \
   || [[ $(cat /etc/apt/sources.list.d/docker.sources) != "$docker_sources" ]]; then
  printf '%s\n' "$docker_sources" > /etc/apt/sources.list.d/docker.sources
  rm -f /etc/apt/sources.list.d/docker.list   # old one-line format, if any
fi
apt-get update
apt-get "${APT_OPTS[@]}" install docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker containerd
ok "docker installed and enabled"

# --- 4. user ------------------------------------------------------------------
step "User $TARGET_USER"
if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx docker; then
  ok "$TARGET_USER already in group docker"
else
  usermod -aG docker "$TARGET_USER"
  ok "added $TARGET_USER to group docker (log out and back in to use it)"
fi
install -d -m 0755 -o "$TARGET_USER" -g "$(id -gn "$TARGET_USER")" "$TARGET_HOME/lego"
ok "$TARGET_HOME/lego"

# --- 5. unattended security upgrades -----------------------------------------
step "Unattended upgrades"
# Debian's stock 50unattended-upgrades already limits origins to Debian
# (security + point releases). Docker's repo is not in it, so Docker is only
# upgraded when you run apt yourself -- no surprise container restarts.
auto_conf='APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";'
if [[ ! -f /etc/apt/apt.conf.d/20auto-upgrades ]] \
   || [[ $(cat /etc/apt/apt.conf.d/20auto-upgrades) != "$auto_conf" ]]; then
  printf '%s\n' "$auto_conf" > /etc/apt/apt.conf.d/20auto-upgrades
fi
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
ok "unattended-upgrades enabled"

# --- 6. firewall --------------------------------------------------------------
FW_FILE=/etc/brickpipe/firewall.nft
FW_UNIT=/etc/systemd/system/brickpipe-firewall.service
if [[ $DO_FIREWALL -eq 1 ]]; then
  step "Firewall (nftables table inet brickpipe_fw)"
  # sudo drops SSH_CLIENT, so look at the live SSH connections instead.
  mapfile -t ssh_peers < <(ss -Htn state established '( sport = :22 )' 2>/dev/null \
    | awk '{print $4}' | sed -E 's/^\[(::ffff:)?//; s/\]?:[0-9]+$//' | sort -u)
  if [[ ${#ssh_peers[@]} -eq 0 ]]; then
    warn "no SSH session found (running from the console?); make sure $LAN_CIDR is right"
  fi
  for peer in "${ssh_peers[@]}"; do
    if [[ $peer =~ ^[0-9]+(\.[0-9]+){3}$ ]] && in_cidr "$peer" "$LAN_CIDR"; then
      ok "SSH session from $peer is inside $LAN_CIDR"
    elif [[ $FORCE_FIREWALL -eq 1 ]]; then
      warn "SSH session from $peer is OUTSIDE $LAN_CIDR; continuing (--force-firewall)"
    else
      die "an SSH session comes from $peer, outside --lan-cidr $LAN_CIDR. The firewall would cut it off. Fix --lan-cidr, or use --force-firewall."
    fi
  done
  install -d -m 0755 /etc/brickpipe
  fw_rules="#!/usr/sbin/nft -f
# brickpipe VM firewall -- written by deploy/server/bootstrap.sh; re-run it to change.
# Inbound: SSH from the LAN only. Nothing else (the Cloudflare tunnel is outbound).
# Own table: never touches Docker's tables, so it is safe to reload any time.
table inet brickpipe_fw
delete table inet brickpipe_fw
table inet brickpipe_fw {
  chain input {
    type filter hook input priority filter; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif lo accept
    # Containers talking to the host (docker0 and compose bridges).
    iifname \"docker0\" accept
    iifname \"br-*\" accept
    # IPv6 needs neighbour discovery; ping from the LAN helps troubleshooting.
    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, nd-router-solicit } accept
    # DHCPv6 client replies (link-local server, port 547 -> 546).
    ip6 saddr fe80::/10 udp sport 547 udp dport 546 accept
    ip saddr $LAN_CIDR icmp type echo-request accept
    ip saddr $LAN_CIDR tcp dport 22 ct state new accept
  }
}"
  tmp_fw=$(mktemp)
  printf '%s\n' "$fw_rules" > "$tmp_fw"
  nft -c -f "$tmp_fw" || { rm -f "$tmp_fw"; die "nftables rules failed the syntax check; nothing changed"; }
  install -m 0644 "$tmp_fw" "$FW_FILE"; rm -f "$tmp_fw"
  fw_unit="[Unit]
Description=brickpipe host firewall (nftables table inet brickpipe_fw)
Wants=network-pre.target
Before=network-pre.target
DefaultDependencies=no
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $FW_FILE
ExecReload=/usr/sbin/nft -f $FW_FILE
ExecStop=/usr/sbin/nft delete table inet brickpipe_fw

[Install]
WantedBy=multi-user.target"
  if [[ ! -f $FW_UNIT ]] || [[ $(cat "$FW_UNIT") != "$fw_unit" ]]; then
    printf '%s\n' "$fw_unit" > "$FW_UNIT"
    systemctl daemon-reload
  fi
  # Mask (do not stop) Debian's nftables.service: stopping it runs
  # 'nft flush ruleset', and starting it loads a file that flushes everything.
  if [[ $(systemctl is-enabled nftables.service 2>/dev/null || true) != masked ]]; then
    systemctl mask nftables.service >/dev/null 2>&1
    ok "masked nftables.service (its 'flush ruleset' would wipe Docker's rules)"
  else
    ok "nftables.service already masked"
  fi
  systemctl enable brickpipe-firewall.service >/dev/null 2>&1
  systemctl restart brickpipe-firewall.service
  ok "firewall active: inbound SSH from $LAN_CIDR only"
else
  step "Firewall skipped (--skip-firewall)"
fi

# --- 7. SSH hardening -----------------------------------------------------------
SSHD_DROPIN=/etc/ssh/sshd_config.d/10-brickpipe-hardening.conf
if [[ $DO_SSH_HARDEN -eq 1 ]]; then
  step "SSH hardening"
  ak="$TARGET_HOME/.ssh/authorized_keys"
  nkeys=0
  if [[ -s $ak ]]; then
    nkeys=$(ssh-keygen -l -f "$ak" 2>/dev/null | wc -l || true)
  fi
  if (( nkeys < 1 )); then
    warn "no valid key in $ak -- NOT disabling password login (you could be locked out)."
    warn "Add your public key there, check you can log in with it, then re-run this script."
  else
    ok "$nkeys valid key(s) in $ak"
    # 10- sorts before cloud-init's 50-cloud-init.conf; first value wins in sshd.
    hardening="# Written by brickpipe deploy/server/bootstrap.sh
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no"
    if [[ ! -f $SSHD_DROPIN ]] || [[ $(cat "$SSHD_DROPIN") != "$hardening" ]]; then
      printf '%s\n' "$hardening" > "$SSHD_DROPIN"
      if sshd -t; then
        systemctl reload ssh
        ok "sshd reloaded (your current session stays open)"
      else
        rm -f "$SSHD_DROPIN"
        die "sshd config test failed; removed $SSHD_DROPIN, nothing changed"
      fi
    fi
    ok "password and root login disabled"
  fi
else
  step "SSH hardening skipped (--skip-ssh-hardening)"
fi

# --- summary --------------------------------------------------------------------
step "Summary"
check() {  # label, command...
  local label=$1; shift
  local out
  if out=$("$@" 2>&1); then printf '  [PASS] %-16s %s\n' "$label" "$(head -n1 <<<"$out")"
  else printf '  [FAIL] %-16s %s\n' "$label" "$(head -n1 <<<"$out")"; fi
}
check "docker" docker version --format 'client {{.Client.Version}} / server {{.Server.Version}}'
check "compose" docker compose version
check "docker running" systemctl is-active docker
check "git" git --version
check "timezone" timedatectl show -p Timezone --value
check "unattended-upg" systemctl is-enabled unattended-upgrades
check "guest agent" systemctl is-active qemu-guest-agent
if [[ $DO_FIREWALL -eq 1 ]]; then
  check "firewall" systemctl is-active brickpipe-firewall
  nft_svc_masked() {
    [[ $(systemctl is-enabled nftables.service 2>/dev/null || true) == masked ]] \
      && echo "masked (cannot flush the ruleset)"
  }
  check "nftables.svc" nft_svc_masked
  check "fw table" sh -c 'nft list table inet brickpipe_fw >/dev/null && echo "inet brickpipe_fw loaded, SSH from '"$LAN_CIDR"' only"'
fi
check "sshd password" sh -c 'sshd -T 2>/dev/null | grep -i "^passwordauthentication"'
check "sshd root" sh -c 'sshd -T 2>/dev/null | grep -i "^permitrootlogin"'
check "docker group" sh -c "id -nG '$TARGET_USER' | tr ' ' '\n' | grep -x docker"
check "lego dir" ls -ld "$TARGET_HOME/lego"
printf '\nDone. Log out and back in once so "docker" works without sudo for %s.\n' "$TARGET_USER"
