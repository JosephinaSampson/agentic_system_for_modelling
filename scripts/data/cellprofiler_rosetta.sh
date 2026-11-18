#!/bin/sh
# Launcher for the x86_64/Rosetta CellProfiler environment on Apple Silicon.
# Forwards every argument to the environment's own `cellprofiler` entry point with
# JAVA_HOME set to the environment's x86_64 JDK (python-javabridge refuses the host's
# arm64 Java). See docs/data-workflow.md, "Setup", for how the environment is created.
# Pass this script as `--executable` to the vendored CellProfiler helper
# (vendor/scientific-agent-skills/cellprofiler/scripts/nuclei_assay.py run).
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd -P)
env_dir="$root/.venv-cellprofiler-rosetta"
if [ ! -x "$env_dir/bin/cellprofiler" ]; then
    echo "cellprofiler_rosetta.sh: $env_dir/bin/cellprofiler is missing; create the environment first" >&2
    exit 1
fi
JAVA_HOME="$env_dir/lib/jvm"
export JAVA_HOME
exec "$env_dir/bin/cellprofiler" "$@"
