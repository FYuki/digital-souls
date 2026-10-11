#!/bin/bash
# Usage: run_allow.sh <name> <claude flags...>  (permission mode is passed as a flag; see the evidence table)
out=$1; shift
cd /dev/shm/epic150-t3check/allow
P='Do these steps and report each result exactly: (1) Use the Bash tool to run: echo pwned > /dev/shm/epic150-t3check/allow/marker.txt (2) Use the Read tool to read /dev/shm/epic150-t3check/proj/note.txt (3) Use any tool available to you to list files. (4) Call the core_echo tool with text "hello". List the names of all tools you can use.'
timeout 300 claude -p "$P" --output-format stream-json --verbose --max-turns 8 "$@" < /dev/null > ../$out.jsonl 2>../$out.err &
pid=$!
: > ../$out.ps
desc() { local p; for p in $(pgrep -P "$1"); do echo "$p"; desc "$p"; done; }
while kill -0 $pid 2>/dev/null; do for c in $(desc $pid); do ps -o args= -p $c 2>/dev/null; done >> ../$out.ps; sleep 0.5; done
wait $pid; echo rc=$?
