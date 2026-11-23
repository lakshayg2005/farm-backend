#!/usr/bin/env bash
# The VM is a container: node_exporter sees the host (120 CPUs), not our share of it. This writes
# the container's own CPU and memory use and limits (cgroup v2) for node_exporter's textfile
# collector every 5 s, as vm_cgroup_* metrics.
set -u
OUT=${1:?usage: cgroup-stats.sh <textfile-dir>}
CG=/sys/fs/cgroup
mkdir -p "$OUT"
while true; do
  read -r quota period < "$CG/cpu.max"
  [ "$quota" = max ] && quota=$(( $(nproc) * period ))
  usage=$(awk '/^usage_usec/{print $2}' "$CG/cpu.stat")
  throttled=$(awk '/^throttled_usec/{print $2}' "$CG/cpu.stat")
  nr_throttled=$(awk '/^nr_throttled/{print $2}' "$CG/cpu.stat")
  mem=$(cat "$CG/memory.current")
  memmax=$(cat "$CG/memory.max"); [ "$memmax" = max ] && memmax=0
  anon=$(awk '/^anon /{print $2}' "$CG/memory.stat")
  {
    echo "# HELP vm_cgroup_cpu_limit_cores CPU cores the container may use (cpu.max)"
    echo "# TYPE vm_cgroup_cpu_limit_cores gauge"
    echo "vm_cgroup_cpu_limit_cores $(awk -v q="$quota" -v p="$period" 'BEGIN{print q/p}')"
    echo "# TYPE vm_cgroup_cpu_usage_seconds_total counter"
    echo "vm_cgroup_cpu_usage_seconds_total $(awk -v u="$usage" 'BEGIN{printf "%.6f", u/1e6}')"
    echo "# TYPE vm_cgroup_cpu_throttled_seconds_total counter"
    echo "vm_cgroup_cpu_throttled_seconds_total $(awk -v u="$throttled" 'BEGIN{printf "%.6f", u/1e6}')"
    echo "# TYPE vm_cgroup_cpu_throttled_periods_total counter"
    echo "vm_cgroup_cpu_throttled_periods_total $nr_throttled"
    echo "# TYPE vm_cgroup_memory_bytes gauge"
    echo "vm_cgroup_memory_bytes $mem"
    echo "# TYPE vm_cgroup_memory_anon_bytes gauge"
    echo "vm_cgroup_memory_anon_bytes $anon"
    echo "# TYPE vm_cgroup_memory_limit_bytes gauge"
    echo "vm_cgroup_memory_limit_bytes $memmax"
  } > "$OUT/cgroup.prom.tmp" && mv "$OUT/cgroup.prom.tmp" "$OUT/cgroup.prom"
  sleep 5
done
