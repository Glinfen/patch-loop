#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Provision a dedicated, fixed-capacity ext4 workspace for PatchLoop.

Usage:
  provision_sandbox_workspace.sh --root DIR --name NAME --size-mb N --inodes N \
      --uid UID --gid GID [--apply]
  provision_sandbox_workspace.sh --root DIR --name NAME --destroy [--apply]

The default is a dry run.  --apply requires root.  Creation refuses existing
targets and never copies or overwrites a repository.  Destruction validates the
saved receipt, mount source, and an otherwise empty workspace before removing it.
EOF
}

root=""
name=""
size_mb=""
inodes=""
owner_uid=""
owner_gid=""
apply=0
destroy=0
while (($#)); do
  case "$1" in
    --root) root=${2:?}; shift 2 ;;
    --name) name=${2:?}; shift 2 ;;
    --size-mb) size_mb=${2:?}; shift 2 ;;
    --inodes) inodes=${2:?}; shift 2 ;;
    --uid) owner_uid=${2:?}; shift 2 ;;
    --gid) owner_gid=${2:?}; shift 2 ;;
    --apply) apply=1; shift ;;
    --destroy) destroy=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$root" && -n "$name" ]] || { usage >&2; exit 2; }
[[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || {
  echo "--name must be a simple 1-64 character name" >&2; exit 2;
}
root=$(realpath -m -- "$root")
[[ "$root" != "/" ]] || { echo "refusing to use / as storage root" >&2; exit 2; }
[[ -d "$root" && ! -L "$root" ]] || {
  echo "--root must be an existing, non-symlink directory" >&2; exit 2;
}
bundle="$root/$name"
workspace="$bundle/workspace"
backing="$root/.$name.ext4"
receipt="$root/.$name.receipt"
for path in "$bundle" "$workspace" "$backing" "$receipt"; do
  [[ ! -L "$path" ]] || { echo "refusing symbolic link: $path" >&2; exit 2; }
  case "$path" in "$root"/*) ;; *) echo "target escapes storage root" >&2; exit 2;; esac
done

if ((destroy)); then
  [[ -f "$receipt" ]] || { echo "receipt not found: $receipt" >&2; exit 1; }
  # The receipt is shell-escaped, root-owned data created below; never source it.
  saved_workspace=$(sed -n 's/^workspace=//p' "$receipt")
  saved_backing=$(sed -n 's/^backing=//p' "$receipt")
  saved_loop=$(sed -n 's/^loop_device=//p' "$receipt")
  saved_mount_device=$(sed -n 's/^mount_device=//p' "$receipt")
  saved_mount_inode=$(sed -n 's/^mount_inode=//p' "$receipt")
  [[ "$saved_workspace" == "$workspace" && "$saved_backing" == "$backing" ]] || {
    echo "receipt target mismatch" >&2; exit 1;
  }
  [[ "$saved_loop" == /dev/loop* ]] || { echo "invalid loop device in receipt" >&2; exit 1; }
  [[ "$saved_mount_device" =~ ^[0-9]+$ && "$saved_mount_inode" =~ ^[0-9]+$ ]] || {
    echo "invalid mount identity in receipt" >&2; exit 1;
  }
  [[ -f "$saved_backing" && ! -L "$saved_backing" ]] || {
    echo "backing file is missing or invalid" >&2; exit 1;
  }
  source_device=$(findmnt -n -o SOURCE --target "$workspace" 2>/dev/null || true)
  [[ "$source_device" == "$saved_loop" ]] || { echo "mounted source does not match receipt" >&2; exit 1; }
  current_backing=$(losetup -n -O BACK-FILE -- "$saved_loop" 2>/dev/null || true)
  [[ -n "$current_backing" && "$(realpath -e -- "$current_backing")" == "$saved_backing" ]] || {
    echo "loop backing file does not match receipt" >&2; exit 1;
  }
  current_mount_device=$(stat -c %d -- "$workspace")
  current_mount_inode=$(stat -c %i -- "$workspace")
  [[ "$current_mount_device" == "$saved_mount_device" && "$current_mount_inode" == "$saved_mount_inode" ]] || {
    echo "mounted workspace identity does not match receipt" >&2; exit 1;
  }
  unexpected=$(find "$workspace" -mindepth 1 -maxdepth 1 ! -name lost+found -print -quit)
  [[ -z "$unexpected" ]] || { echo "workspace is not empty: $unexpected" >&2; exit 1; }
  if ((!apply)); then
    printf 'DRY RUN: umount %q\n' "$workspace"
    printf 'DRY RUN: losetup -d %q\n' "$saved_loop"
    printf 'DRY RUN: remove %q %q %q\n' "$bundle" "$backing" "$receipt"
    exit 0
  fi
  [[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "--apply requires root" >&2; exit 1; }
  umount -- "$workspace"
  losetup -d -- "$saved_loop"
  rmdir -- "$workspace" "$bundle"
  rm -- "$backing" "$receipt"
  exit 0
fi

[[ "$size_mb" =~ ^[0-9]+$ && "$size_mb" -ge 64 ]] || {
  echo "--size-mb must be an integer >= 64" >&2; exit 2;
}
[[ "$inodes" =~ ^[0-9]+$ && "$inodes" -ge 1024 ]] || {
  echo "--inodes must be an integer >= 1024" >&2; exit 2;
}
[[ "$owner_uid" =~ ^[0-9]+$ && "$owner_gid" =~ ^[0-9]+$ ]] || {
  echo "--uid and --gid must be numeric" >&2; exit 2;
}
for path in "$bundle" "$backing" "$receipt"; do
  [[ ! -e "$path" ]] || { echo "refusing existing target: $path" >&2; exit 1; }
done
available=$(df -PB1 -- "$root" 2>/dev/null | awk 'NR==2 {print $4}')
required=$((size_mb * 1024 * 1024))
[[ "$available" =~ ^[0-9]+$ && "$available" -ge "$required" ]] || {
  echo "insufficient free space under $root" >&2; exit 1;
}
if ((!apply)); then
  printf 'DRY RUN: create preallocated %s MiB backing file %q\n' "$size_mb" "$backing"
  printf 'DRY RUN: format ext4 with %s inodes, reserved blocks 0\n' "$inodes"
  printf 'DRY RUN: mount nosuid,nodev at %q, make-private, and chown %s:%s\n' "$workspace" "$owner_uid" "$owner_gid"
  printf 'DRY RUN: write receipt %q\n' "$receipt"
  exit 0
fi
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "--apply requires root" >&2; exit 1; }
mkdir -- "$bundle" "$workspace"
fallocate -l "${size_mb}M" -- "$backing"
allocated=$(($(stat -c %b -- "$backing") * 512))
[[ "$allocated" -ge "$required" ]] || {
  echo "backing file was not fully allocated" >&2
  rm -- "$backing"
  rmdir -- "$workspace" "$bundle"
  exit 1
}
loop_device=$(losetup --find --show -- "$backing")
cleanup_on_error() {
  umount -- "$workspace" 2>/dev/null || true
  losetup -d -- "$loop_device" 2>/dev/null || true
}
trap cleanup_on_error ERR
mkfs.ext4 -q -m 0 -N "$inodes" -- "$loop_device"
mount -t ext4 -o nosuid,nodev -- "$loop_device" "$workspace"
mount --make-private -- "$workspace"
chown "$owner_uid:$owner_gid" -- "$workspace"
chmod 0700 -- "$workspace"
{
  printf 'version=1\n'
  printf 'workspace=%s\n' "$workspace"
  printf 'backing=%s\n' "$backing"
  printf 'loop_device=%s\n' "$loop_device"
  printf 'size_mb=%s\n' "$size_mb"
  printf 'inodes=%s\n' "$inodes"
  printf 'uid=%s\n' "$owner_uid"
  printf 'gid=%s\n' "$owner_gid"
  printf 'mount_device=%s\n' "$(stat -c %d -- "$workspace")"
  printf 'mount_inode=%s\n' "$(stat -c %i -- "$workspace")"
} >"$receipt"
chmod 0600 -- "$receipt"
trap - ERR
echo "provisioned workspace: $workspace"
