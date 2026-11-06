#!/bin/bash
# The ISPConfig server list in /etc/hosts: a run limited with --limit, or one with a failed server, must keep the list
# of every server instead of writing only the servers of that run (2026-11-06: --limit web0 left web0 alone in it).
set -euo pipefail
cd "$(dirname "$0")/.."
docker run --rm -v "$PWD:/repo:ro" -w /repo python:3.12-slim-bookworm sh -ec '
pip install --quiet --disable-pip-version-check --root-user-action=ignore "ansible-core>=2.17,<2.18"
export ANSIBLE_ROLES_PATH=/repo/roles ANSIBLE_LOCALHOST_WARNING=False ANSIBLE_INVENTORY_UNPARSED_WARNING=False
dir=/tmp/hosts-test; mkdir -p $dir
for host in panel web db; do printf "127.0.0.1 localhost\n127.0.1.1 %s.example.test %s\n" $host $host > $dir/$host.example.test; done
run() { ansible-playbook -i tests/hosts-file/inventory tests/hosts-file/play.yml -e "ispconfig_hosts_file=$dir/{{ inventory_hostname }}" "$@"; }
full="# BEGIN ISPConfig servers (Ansible)
10.0.0.21 db.example.test db
10.0.0.10 panel.example.test panel
10.0.0.11 web.example.test web
# END ISPConfig servers (Ansible)"
expect() {  # expect FILE CONTENT
  if [ "$(cat $dir/$1)" != "$2" ]; then echo "FAIL $3: $dir/$1 is"; cat $dir/$1; exit 1; fi; echo "ok: $3"
}
run --tags web > /dev/null
for host in panel web db; do expect $host.example.test "127.0.0.1 localhost
$full" "a full run lists every server on $host, also with --tags"; done

out=$(run --limit web.example.test)
expect web.example.test "127.0.0.1 localhost
$full" "--limit keeps the other servers"
echo "$out" | grep -q "WARNING: /etc/hosts keeps its ISPConfig server list" && echo "ok: --limit warns"

run -e hosts_test_fail=db.example.test -e "{\"hosts_test_addresses\": {\"web.example.test\": \"10.0.0.99\"}}" > /dev/null || true
expect panel.example.test "127.0.0.1 localhost
$full" "a failed server keeps the list"

run -e "{\"hosts_test_addresses\": {\"web.example.test\": \"10.0.0.99\"}}" > /dev/null
expect db.example.test "127.0.0.1 localhost
$(echo "$full" | sed s/10.0.0.11/10.0.0.99/)" "a full run updates a changed address"
'
