#!/usr/bin/env bash
# create-vm.sh -- create the brickpipe VM on a Proxmox VE host from a Debian
# cloud image, with cloud-init. Run as root on the Proxmox host.
#
# DRY-RUN BY DEFAULT: read-only checks run, every command that would change
# something is printed, nothing is changed. Add --apply to do it for real.
#
# See docs/server-setup.md for the whole procedure. --help for the options.

set -euo pipefail

# ---------------------------------------------------------------- defaults
VMID=110
NAME=brickpipe
CORES=2
MEMORY_MB=6144
DISK_GB=40
DEBIAN=13
DISK_STORAGE=local-lvm
SNIPPET_STORAGE=local
BRIDGE=vmbr0
IP=dhcp                # or CIDR, e.g. 192.168.1.40/24
GATEWAY=192.168.1.1
DNS=192.168.1.1
SEARCHDOMAIN=
CIUSER=chris
SSH_KEY_FILE=
IMAGE_DIR=/var/lib/vz/brickpipe-cloud-images
HOST_RESERVE_MB=2048
FORCE_MEMORY=0
APPLY=0
START=1

PROTECTED_VMIDS="100"   # your Home Assistant VM: never touched, never reused

usage() {
  cat <<EOF
Usage: create-vm.sh --ssh-key-file FILE [options] [--apply]

Creates a Debian cloud-image VM for brickpipe with qm + cloud-init.
Without --apply nothing is changed: checks run and commands are printed.

Required:
  --ssh-key-file FILE   SSH *public* key (e.g. id_ed25519.pub) for the VM user

VM:
  --vmid N              VM id (default $VMID; must not exist; 100 is refused)
  --name NAME           VM name (default $NAME)
  --cores N             vCPUs, CPU type host (default $CORES)
  --memory MB           RAM in MB (default $MEMORY_MB)
  --disk-gb N           disk size in GB (default $DISK_GB)
  --debian 12|13        Debian release of the cloud image (default $DEBIAN)
  --storage NAME        storage for the VM disk + cloud-init drive (default $DISK_STORAGE)
  --snippet-storage N   storage with 'snippets' content for the guest-agent
                        vendor-data (default $SNIPPET_STORAGE; skipped if it
                        has no snippets content -- bootstrap.sh installs the
                        agent anyway)
  --bridge NAME         network bridge (default $BRIDGE)
  --no-start            do not start the VM after creating it

Network / cloud-init:
  --ip dhcp|CIDR        'dhcp' (default) or a static address like 192.168.1.40/24
  --gateway IP          gateway for a static address (default $GATEWAY)
  --dns "IP [IP]"       DNS server(s) (default $DNS)
  --searchdomain D      DNS search domain (default: none)
  --user NAME           login user created by cloud-init (default $CIUSER)

Safety:
  --host-reserve-mb MB  RAM that must stay free for the host (default $HOST_RESERVE_MB)
  --force-memory        create the VM even if the memory check fails
  --image-dir DIR       where the cloud image is cached (default $IMAGE_DIR)
  --apply               actually do it (default is a dry run)
  -h, --help            this help

Examples:
  ./create-vm.sh --ssh-key-file /root/chris.pub                      # dry run
  ./create-vm.sh --ssh-key-file /root/chris.pub --ip 192.168.1.40/24 --apply
EOF
}

die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
info() { printf '%s\n' "$*"; }
hdr()  { printf '\n== %s\n' "$*"; }

# Print a command; run it only with --apply.
# Shell-quote only what needs it, so printed commands stay readable and pasteable.
q() {
  if [[ $1 =~ ^[A-Za-z0-9_./:=,@%+-]+$ ]]; then printf '%s' "$1"
  else printf "'%s'" "${1//\'/\'\\\'\'}"; fi
}
run() {
  local a out="+"
  for a in "$@"; do out+=" $(q "$a")"; done
  printf '%s\n' "$out"
  if [[ $APPLY -eq 1 ]]; then "$@"; fi
}

need_arg() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value"; }

# ---------------------------------------------------------------- arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vmid)            need_arg "$@"; VMID=$2; shift 2 ;;
    --name)            need_arg "$@"; NAME=$2; shift 2 ;;
    --cores)           need_arg "$@"; CORES=$2; shift 2 ;;
    --memory)          need_arg "$@"; MEMORY_MB=$2; shift 2 ;;
    --disk-gb)         need_arg "$@"; DISK_GB=$2; shift 2 ;;
    --debian)          need_arg "$@"; DEBIAN=$2; shift 2 ;;
    --storage)         need_arg "$@"; DISK_STORAGE=$2; shift 2 ;;
    --snippet-storage) need_arg "$@"; SNIPPET_STORAGE=$2; shift 2 ;;
    --bridge)          need_arg "$@"; BRIDGE=$2; shift 2 ;;
    --ip)              need_arg "$@"; IP=$2; shift 2 ;;
    --gateway)         need_arg "$@"; GATEWAY=$2; shift 2 ;;
    --dns)             need_arg "$@"; DNS=$2; shift 2 ;;
    --searchdomain)    need_arg "$@"; SEARCHDOMAIN=$2; shift 2 ;;
    --user)            need_arg "$@"; CIUSER=$2; shift 2 ;;
    --ssh-key-file)    need_arg "$@"; SSH_KEY_FILE=$2; shift 2 ;;
    --image-dir)       need_arg "$@"; IMAGE_DIR=$2; shift 2 ;;
    --host-reserve-mb) need_arg "$@"; HOST_RESERVE_MB=$2; shift 2 ;;
    --force-memory)    FORCE_MEMORY=1; shift ;;
    --no-start)        START=0; shift ;;
    --apply)           APPLY=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    *)                 usage >&2; die "unknown option: $1" ;;
  esac
done

# ---------------------------------------------------------------- validation
is_uint() { [[ $1 =~ ^[0-9]+$ ]]; }
is_ipv4() {
  local IFS=. o; local -a p
  [[ $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  read -r -a p <<<"$1"
  for o in "${p[@]}"; do (( 10#$o <= 255 )) || return 1; done
}

{ is_uint "$VMID" && (( VMID >= 100 )); } || die "--vmid must be a number >= 100"
for p in $PROTECTED_VMIDS; do
  [[ $VMID -ne $p ]] || die "VMID $p is the Home Assistant VM and is never used by this script"
done
{ is_uint "$CORES" && (( CORES >= 1 )); } || die "--cores must be a positive number"
{ is_uint "$MEMORY_MB" && (( MEMORY_MB >= 1024 )); } || die "--memory must be >= 1024 (MB)"
{ is_uint "$DISK_GB" && (( DISK_GB >= 10 )); } || die "--disk-gb must be >= 10"
is_uint "$HOST_RESERVE_MB" || die "--host-reserve-mb must be a number"
[[ $NAME =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || die "--name must be a valid DNS name"
[[ $CIUSER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "--user must be a valid Linux user name"
[[ $CIUSER != root ]] || die "--user must not be root"

case "$DEBIAN" in
  12) CODENAME=bookworm ;;
  13) CODENAME=trixie ;;
  *)  die "--debian must be 12 or 13" ;;
esac
IMAGE_FILE="debian-${DEBIAN}-genericcloud-amd64.qcow2"
IMAGE_BASE_URL="https://cloud.debian.org/images/cloud/${CODENAME}/latest"

if [[ $IP == dhcp ]]; then
  IPCONFIG="ip=dhcp"
else
  [[ $IP =~ ^([0-9.]+)/([0-9]+)$ ]] || die "--ip must be 'dhcp' or CIDR like 192.168.1.40/24"
  ip_addr=${BASH_REMATCH[1]}; ip_len=${BASH_REMATCH[2]}
  { is_ipv4 "$ip_addr" && (( ip_len >= 8 && ip_len <= 30 )); } \
    || die "--ip '$IP' is not a valid IPv4 CIDR"
  is_ipv4 "$GATEWAY" || die "--gateway '$GATEWAY' is not an IPv4 address"
  IPCONFIG="ip=${IP},gw=${GATEWAY}"
fi
for d in $DNS; do is_ipv4 "$d" || die "--dns '$d' is not an IPv4 address"; done

[[ -n $SSH_KEY_FILE ]] || { usage >&2; die "--ssh-key-file is required"; }
[[ -r $SSH_KEY_FILE ]] || die "cannot read SSH key file: $SSH_KEY_FILE"
SSH_KEY_FILE=$(readlink -f "$SSH_KEY_FILE")
if grep -q 'PRIVATE KEY' "$SSH_KEY_FILE"; then
  die "$SSH_KEY_FILE is a PRIVATE key. Use the .pub file; delete the private key from this host."
fi
grep -Eq '^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) ' "$SSH_KEY_FILE" \
  || die "$SSH_KEY_FILE does not look like an OpenSSH public key"

for c in qm pvesm free wget sha512sum awk; do
  command -v "$c" >/dev/null 2>&1 || die "'$c' not found -- run this on the Proxmox host"
done
if [[ $APPLY -eq 1 && $EUID -ne 0 ]]; then die "--apply must run as root"; fi

if [[ $APPLY -eq 1 ]]; then
  info "MODE: APPLY -- changes will be made."
else
  info "MODE: DRY RUN -- nothing will be changed. Add --apply to do it."
fi

# ---------------------------------------------------------------- VMID
hdr "VM id"
if qm status "$VMID" >/dev/null 2>&1; then
  die "VMID $VMID already exists ($(qm status "$VMID" 2>/dev/null)). Refusing to touch it. Pick another --vmid."
fi
# VM ids are shared with containers (and with other cluster nodes).
for f in /etc/pve/nodes/*/qemu-server/"$VMID".conf /etc/pve/nodes/*/lxc/"$VMID".conf; do
  [[ ! -e $f ]] || die "VMID $VMID is already used ($f). Refusing. Pick another --vmid."
done
info "VMID $VMID is free."
if qm list 2>/dev/null | awk -v n="$NAME" 'NR>1 && $2==n {f=1} END {exit !f}'; then
  warn "a VM named '$NAME' already exists (different VMID). Continuing, but check this is intended."
fi

# ---------------------------------------------------------------- storage
hdr "Storages on this host (pvesm status)"
pvesm status
storage_has() {  # storage content -> 0 if active and supports content
  pvesm status --content "$2" 2>/dev/null | awk -v s="$1" 'NR>1 && $1==s && $3=="active" {f=1} END {exit !f}'
}
storage_has "$DISK_STORAGE" images \
  || die "storage '$DISK_STORAGE' is not active or does not hold VM disks. Pick one from the list above with --storage."
avail_kib=$(pvesm status 2>/dev/null | awk -v s="$DISK_STORAGE" 'NR>1 && $1==s {print $6}')
if is_uint "${avail_kib:-x}"; then
  avail_gb=$(( avail_kib / 1024 / 1024 ))
  info "Disk storage '$DISK_STORAGE': ${avail_gb} GB available, VM needs up to ${DISK_GB} GB."
  (( avail_gb >= DISK_GB )) || die "not enough space on '$DISK_STORAGE' (${avail_gb} GB < ${DISK_GB} GB)."
fi
USE_SNIPPET=0
if storage_has "$SNIPPET_STORAGE" snippets; then
  USE_SNIPPET=1
  info "Snippet storage '$SNIPPET_STORAGE' supports snippets: the guest agent is installed on first boot."
else
  info "Storage '$SNIPPET_STORAGE' has no 'snippets' content: skipping guest-agent vendor-data."
  info "  (Not changing storage config. bootstrap.sh installs qemu-guest-agent instead.)"
fi

# ---------------------------------------------------------------- network
hdr "Network"
if [[ -d /sys/class/net/$BRIDGE ]]; then
  info "Bridge $BRIDGE exists."
elif [[ $APPLY -eq 1 ]]; then
  die "bridge '$BRIDGE' not found on this host"
else
  warn "bridge '$BRIDGE' not found (fine if this is not the Proxmox host)"
fi
info "cloud-init network: ipconfig0=$IPCONFIG nameserver='$DNS'"

# ---------------------------------------------------------------- memory
hdr "Memory"
read -r total_b avail_b < <(free -b | awk '/^Mem:/ {print $2, $7}')
{ is_uint "${total_b:-x}" && is_uint "${avail_b:-x}"; } || die "could not read memory from 'free -b'"
total_mb=$(( total_b / 1024 / 1024 ))
avail_mb=$(( avail_b / 1024 / 1024 ))
# Committed: every existing VM at its full configured memory (conservative;
# counts VM 100's 4 GB even though QEMU may not have touched it all yet).
committed_mb=$(qm list 2>/dev/null | awk 'NR>1 && $4 ~ /^[0-9]+$/ {s+=$4} END {print s+0}')
info "Host RAM total:          ${total_mb} MB"
info "Host RAM available now:  ${avail_mb} MB"
info "Existing VMs configured: ${committed_mb} MB"
qm list 2>/dev/null | awk 'NR>1 {printf "    VM %-5s %-24s %-8s %s MB\n", $1, $2, $3, $4}'
info "New VM:                  ${MEMORY_MB} MB"
info "Host reserve required:   ${HOST_RESERVE_MB} MB"
left_committed=$(( total_mb - committed_mb - MEMORY_MB ))
left_now=$(( avail_mb - MEMORY_MB ))
info "Left if all VMs use their full RAM: ${left_committed} MB"
info "Left from what is available now:    ${left_now} MB"
mem_ok=1
(( left_committed >= HOST_RESERVE_MB )) || mem_ok=0
(( left_now >= HOST_RESERVE_MB )) || mem_ok=0
if [[ $mem_ok -eq 0 ]]; then
  if [[ $FORCE_MEMORY -eq 1 ]]; then
    warn "memory check FAILED, continuing because of --force-memory."
  else
    die "not enough memory: the new VM would leave less than ${HOST_RESERVE_MB} MB for the host and VM 100. Use a smaller --memory, or --force-memory if you are sure."
  fi
else
  info "Memory check OK."
fi

# ---------------------------------------------------------------- image
hdr "Cloud image (Debian $DEBIAN $CODENAME, genericcloud amd64)"
IMAGE_PATH="$IMAGE_DIR/$IMAGE_FILE"
SUMS_PATH="$IMAGE_DIR/SHA512SUMS.$CODENAME"

verify_image() {
  # Pull this image's line out of Debian's SHA512SUMS and check it.
  local line
  line=$(awk -v f="$IMAGE_FILE" '$2==f || $2=="*"f' "$SUMS_PATH")
  [[ -n $line ]] || die "$IMAGE_FILE not listed in $SUMS_PATH"
  (cd "$IMAGE_DIR" && printf '%s\n' "$line" | sha512sum -c --strict -)
}

run mkdir -p "$IMAGE_DIR"
# Always fetch a fresh SHA512SUMS; 'latest' moves when Debian publishes a new build.
run wget -q -O "$SUMS_PATH" "$IMAGE_BASE_URL/SHA512SUMS"
if [[ $APPLY -eq 1 ]]; then
  if [[ -f $IMAGE_PATH ]] && verify_image >/dev/null 2>&1; then
    info "Cached image matches the published checksum; not downloading again."
  else
    run wget -q --show-progress -O "$IMAGE_PATH.part" "$IMAGE_BASE_URL/$IMAGE_FILE"
    run mv -f "$IMAGE_PATH.part" "$IMAGE_PATH"
  fi
  verify_image || { rm -f "$IMAGE_PATH"; die "checksum mismatch -- image deleted, nothing created."; }
  info "Checksum OK."
else
  info "+ wget -q --show-progress -O $IMAGE_PATH.part $IMAGE_BASE_URL/$IMAGE_FILE   (skipped if cached copy verifies)"
  info "+ mv -f $IMAGE_PATH.part $IMAGE_PATH"
  info "+ verify: line for $IMAGE_FILE from $SUMS_PATH | sha512sum -c --strict -   (abort + delete on mismatch)"
fi

# ---------------------------------------------------------------- vendor data
CICUSTOM_ARGS=()
if [[ $USE_SNIPPET -eq 1 ]]; then
  SNIPPET_NAME="brickpipe-vendor.yaml"
  SNIPPET_PATH=$(pvesm path "$SNIPPET_STORAGE:snippets/$SNIPPET_NAME" 2>/dev/null || true)
  [[ -n $SNIPPET_PATH ]] || die "could not resolve path for $SNIPPET_STORAGE:snippets/$SNIPPET_NAME"
  hdr "Vendor data snippet ($SNIPPET_PATH)"
  SNIPPET_CONTENT='#cloud-config
# Written by brickpipe deploy/proxmox/create-vm.sh. Installs the QEMU guest
# agent on first boot so Proxmox can show the VM IP address.
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]'
  printf '%s\n' "$SNIPPET_CONTENT" | sed 's/^/    | /'
  run mkdir -p "$(dirname "$SNIPPET_PATH")"
  if [[ $APPLY -eq 1 ]]; then
    printf '%s\n' "$SNIPPET_CONTENT" > "$SNIPPET_PATH"
  else
    info "+ write the content above to $SNIPPET_PATH"
  fi
  CICUSTOM_ARGS=(--cicustom "vendor=$SNIPPET_STORAGE:snippets/$SNIPPET_NAME")
fi

# ---------------------------------------------------------------- create
hdr "Create VM $VMID"
# Startup order is deliberately NOT set: in Proxmox, VMs without an order
# start after those with one, and the Home Assistant VM's order is not ours
# to change.
run qm create "$VMID" \
  --name "$NAME" \
  --ostype l26 \
  --cpu host \
  --cores "$CORES" \
  --memory "$MEMORY_MB" \
  --scsihw virtio-scsi-single \
  --scsi0 "$DISK_STORAGE:0,import-from=$IMAGE_PATH,discard=on,ssd=1,iothread=1" \
  --ide2 "$DISK_STORAGE:cloudinit" \
  --boot order=scsi0 \
  --net0 "virtio,bridge=$BRIDGE" \
  --serial0 socket \
  --vga serial0 \
  --agent enabled=1 \
  --onboot 1 \
  --description "brickpipe + LDraw Nova (Docker). Created by brickpipe deploy/proxmox/create-vm.sh"
run qm disk resize "$VMID" scsi0 "${DISK_GB}G"

CI_ARGS=(--ciuser "$CIUSER" --sshkeys "$SSH_KEY_FILE" --ipconfig0 "$IPCONFIG" --nameserver "$DNS")
[[ -z $SEARCHDOMAIN ]] || CI_ARGS+=(--searchdomain "$SEARCHDOMAIN")
# Package upgrades are bootstrap.sh's job; keep first boot quick (PVE >= 8.1 option).
CI_ARGS+=(--ciupgrade 0)
run qm set "$VMID" "${CI_ARGS[@]}" "${CICUSTOM_ARGS[@]}"

if [[ $START -eq 1 ]]; then
  run qm start "$VMID"
fi

# ---------------------------------------------------------------- summary
hdr "Next"
if [[ $IP == dhcp ]]; then
  info "The VM gets its address from DHCP. Find it with (after ~1 min, needs the guest agent):"
  info "  qm guest cmd $VMID network-get-interfaces | grep -w ip-address"
  info "or in the router's DHCP lease list (name: $NAME). Then reserve it on the router."
else
  info "The VM will be at ${IP%/*}."
fi
if [[ $IP == dhcp ]]; then VM_ADDR="<vm-ip>"; else VM_ADDR=${IP%/*}; fi
info "SSH from the laptop:   ssh $CIUSER@$VM_ADDR"
info "Serial console here:   qm terminal $VMID   (exit with Ctrl+O)"
hdr "Undo (removes VM $VMID only -- never run with another id)"
info "  qm stop $VMID; qm destroy $VMID --purge 1 --destroy-unreferenced-disks 1"
[[ $USE_SNIPPET -eq 0 ]] || info "  rm -f ${SNIPPET_PATH}   # optional, the vendor-data snippet"
info "  rm -rf $IMAGE_DIR       # optional, the cached cloud image"
if [[ $APPLY -eq 0 ]]; then
  printf '\nDRY RUN complete. Nothing was changed. Re-run with --apply to create the VM.\n'
fi
