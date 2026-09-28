#!/usr/bin/env bash
# Run the testbed inside an Ubuntu 26.04 VM (kernel 7.0, iproute2 6.19: dualpi2 in both).
# For hosts whose kernel lacks sch_dualpi2 and must not be swapped (e.g. the CUDA box, 6.8 + NVIDIA DKMS).
# All three namespaces live inside the VM; nothing on the host changes except multipass.
#
# One-time on the host:  sudo snap install multipass
#
#   ./vm.sh up       create the VM, install packages, push the code, run preflight
#   ./vm.sh push     copy the current code into the VM (results/ is not touched)
#   ./vm.sh shell    open a shell in the VM (code in ~/l4s-rescue-testbed)
#   ./vm.sh pull     copy results/ and results_*.log from the VM into ./results/
#   ./vm.sh delete   delete the VM
#
# env: VM (name, default l4s-tb), IMAGE (default 26.04), CPUS (4), MEM (8G), DISK (20G)
set -euo pipefail
cd "$(dirname "$0")"

VM=${VM:-l4s-tb}
IMAGE=${IMAGE:-26.04}
CPUS=${CPUS:-4}
MEM=${MEM:-8G}
DISK=${DISK:-20G}
DIR=/home/ubuntu/l4s-rescue-testbed

command -v multipass >/dev/null || { echo "multipass missing: sudo snap install multipass" >&2; exit 1; }

push() {
    multipass exec "$VM" -- mkdir -p "$DIR"
    tar c --exclude=results --exclude=__pycache__ --exclude=.git --exclude='results_*.log' . \
        | multipass exec "$VM" -- tar x -C "$DIR"
    multipass exec "$VM" -- bash -c "chmod +x $DIR/*.sh $DIR/tb.py $DIR/analyze.py"
    echo "code pushed to $VM:$DIR"
}

exists() { multipass info "$VM" >/dev/null 2>&1; }

need_vm() {
    exists || { echo "VM '$VM' does not exist yet: run ./vm.sh up and check its output for the launch error" >&2; exit 1; }
}

up() {
    if exists; then
        echo "VM '$VM' already exists: reusing it"
    else
        # The cloud-init file must be under $HOME: snap multipass has a private /tmp and cannot see the host's.
        local ci="$PWD/vm-cloud-init.yaml"
        [ -r "$ci" ] || { echo "missing $ci" >&2; exit 1; }
        if ! multipass launch "$IMAGE" --name "$VM" --cpus "$CPUS" --memory "$MEM" --disk "$DISK" --cloud-init "$ci"; then
            echo >&2
            echo "multipass launch failed. Checks:" >&2
            echo "  ls -l /dev/kvm                 (must exist: enable VT-x/AMD-V in the BIOS)" >&2
            echo "  multipass find                 (is '$IMAGE' listed? else IMAGE=<name> ./vm.sh up)" >&2
            echo "  multipass list                 (a half-created '$VM'? then ./vm.sh delete and retry)" >&2
            echo "  repo must be under \$HOME       (snap multipass cannot read $ci otherwise)" >&2
            exit 1
        fi
    fi
    multipass exec "$VM" -- cloud-init status --wait >/dev/null || true
    push
    multipass exec "$VM" -- bash -c "uname -r; tc -V"
    multipass exec "$VM" -- bash -c "cd $DIR && sudo ./preflight.sh"
}

pull() {
    # results_*.log are stored under results/ on the host, as in RUN_COMMANDS.md step 7.
    multipass exec "$VM" -- sudo bash -c \
        "cd $DIR && mkdir -p results && shopt -s nullglob && tar c --transform 's,^results_,results/results_,' results results_*.log" \
        | tar x --no-same-owner
    echo "results copied to ./results/"
}

case ${1:-} in
    up) up ;;
    push) need_vm; push ;;
    shell) need_vm; multipass shell "$VM" ;;
    pull) need_vm; pull ;;
    delete) multipass delete --purge "$VM" ;;
    *) sed -n '2,15p' "$0" >&2; exit 1 ;;
esac
