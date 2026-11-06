#!/bin/bash
# Renders roles/ispconfig_db's MariaDB settings with Ansible and starts the MariaDB of every supported OS with them:
# the validate command must accept them, refuse unknown options, and databases ISPConfig creates must get the
# configured collation. RAM is passed as the fact (7.25 GB, the production database server).
set -euo pipefail
cd "$(dirname "$0")/.."
WORK=$(mktemp -d)
CONTAINERS=()
cleanup() { for c in "${CONTAINERS[@]}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done; rm -rf "$WORK"; }
trap cleanup EXIT
RAM=7424
declare -A VERSION
for image in ${IMAGES:-debian:12 debian:13 ubuntu:22.04 ubuntu:24.04}; do
    name="ispcp-mariadb-config-${image/:/-}"
    CONTAINERS+=("$name")
    # The role's LimitNOFILE for the mariadb service; with less MariaDB shrinks table_open_cache and warns.
    docker run -d --ulimit nofile=131072:131072 --name "$name" "$image" sleep infinity >/dev/null
    docker exec -e DEBIAN_FRONTEND=noninteractive "$name" sh -ec \
        'apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends mariadb-server >/dev/null'
    VERSION[$image]=$(docker exec "$name" mariadbd --version | sed -n 's/.*Ver \([0-9]*\.[0-9]*\).*/\1/p')
done
render() { # image, output, extra vars JSON
    docker run --rm -v "$PWD:/repo:ro" -v "$WORK:/out" -w /repo python:3.12-slim-bookworm sh -ec \
        "pip install --quiet --disable-pip-version-check --root-user-action=ignore 'ansible-core>=2.17,<2.18' >/dev/null
         ansible localhost -c local -m ansible.builtin.template -a 'src=roles/ispconfig_db/templates/mariadb.cnf.j2 dest=/out/$2' \
           -e @roles/ispconfig_db/defaults/main.yml -e ansible_memtotal_mb=$RAM -e ispconfig_mariadb_version=${VERSION[$1]} -e '$3' >/dev/null"
}
for image in "${!VERSION[@]}"; do
    name="ispcp-mariadb-config-${image/:/-}"
    out="${image/:/-}.cnf"
    render "$image" "$out" '{}'
    # A MySQL-only option (as in configurations copied from MySQL 8.4) must stop the file before installation.
    render "$image" "bad-$out" '{"ispconfig_db_mariadb_settings": {"mysql_native_password": "ON"}}'
    docker cp "$WORK/$out" "$name:/etc/mysql/mariadb.conf.d/91-ispconfig-db.cnf"
    docker cp "$WORK/bad-$out" "$name:/tmp/bad.cnf"
    # The role's validate command, run on the candidate file.
    validation=$(docker exec "$name" mariadbd --defaults-file=/etc/mysql/mariadb.conf.d/91-ispconfig-db.cnf --help --verbose 2>&1 >/dev/null)
    # Removed/deprecated options only warn; none may be in the file for this MariaDB version.
    [ -z "$validation" ] || { echo "FAIL $image (MariaDB ${VERSION[$image]}): $validation" >&2; exit 1; }
    if docker exec "$name" mariadbd --defaults-file=/tmp/bad.cnf --help --verbose >/dev/null 2>&1; then
        echo "FAIL $image: an unknown option passed validation" >&2; exit 1
    fi
    docker exec "$name" sh -ec 'mkdir -p /run/mysqld && chown mysql: /run/mysqld
        (mariadbd --user=mysql >/tmp/mariadb.log 2>&1 &)
        for i in $(seq 1 60); do mariadb-admin ping --silent 2>/dev/null && exit 0; sleep 1; done
        cat /tmp/mariadb.log; exit 1'
    query() { docker exec "$name" mariadb --batch --skip-column-names -e "$1"; }
    got=$(query "SELECT @@innodb_buffer_pool_size DIV 1048576, @@innodb_log_file_size DIV 1048576, @@max_connections, @@collation_server, CONCAT('[', @@sql_mode, ']'), @@tx_isolation, @@innodb_flush_log_at_trx_commit, @@table_open_cache")
    expected=$'4352\t1088\t300\tutf8mb4_unicode_ci\t[]\tREAD-COMMITTED\t2\t4096'
    [ "$got" = "$expected" ] || { echo "FAIL $image (MariaDB ${VERSION[$image]}): $got" >&2; exit 1; }
    # ISPConfig creates databases with DEFAULT CHARACTER SET of the chosen charset, or none (server default).
    query "CREATE DATABASE c_mb4 DEFAULT CHARACTER SET utf8mb4; CREATE DATABASE c_default; CREATE DATABASE c_utf8 DEFAULT CHARACTER SET utf8; CREATE TABLE c_mb4.t (v VARCHAR(10));"
    collations=$(query "SELECT GROUP_CONCAT(DEFAULT_COLLATION_NAME ORDER BY SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE 'c\\_%'; SELECT TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'c_mb4'")
    # Before MariaDB 11.2 an explicit character set takes that set's compiled default collation; only databases
    # created with the server default character set get collation-server.
    if [ "$(printf '%s\n' 11.2 "${VERSION[$image]}" | sort -V | head -n1)" = 11.2 ]; then
        expected=$'utf8mb4_unicode_ci,utf8mb4_unicode_ci,utf8mb3_unicode_ci\nutf8mb4_unicode_ci'
    else
        expected=$'utf8mb4_unicode_ci,utf8mb4_general_ci,utf8mb3_general_ci\nutf8mb4_general_ci'
    fi
    [ "$collations" = "$expected" ] || { echo "FAIL $image: collations $collations" >&2; exit 1; }
    warnings=$(docker exec "$name" grep -i 'warning\|error' /tmp/mariadb.log | grep -iv 'mysql.user\|root\|plugin' || true)
    echo "ok   $image MariaDB ${VERSION[$image]}: pool 4352M, redo 1088M, collations ${collations//$'\n'/ / }"
    [ -z "$warnings" ] || printf '     log: %s\n' "$warnings"
done
