#!/usr/bin/env sh
set -eu

mkdir -p \
  /run/munge \
  /run/mysqld \
  /var/log/munge \
  /var/log/slurm \
  /var/run/slurm \
  /var/spool/slurm/ctld \
  /var/spool/slurm/d \
  /public/home/alice \
  /public/home/bob \
  /public/app \
  /pilot107/evidence-derived

chown -R munge:munge /run/munge /var/log/munge
chown -R mysql:mysql /run/mysqld /var/lib/mysql
chown -R slurm:slurm /var/log/slurm /var/run/slurm /var/spool/slurm
chown alice:alice /public/home/alice || true
chown bob:bob /public/home/bob || true
chmod 0700 /public/home/alice /public/home/bob
chmod 0755 /public/app

if command -v munged >/dev/null 2>&1; then
  rm -f /run/munge/munge.socket.2
  gosu munge munged --force
  tries=0
  while [ ! -S /run/munge/munge.socket.2 ]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 50 ]; then
      echo "munged did not create socket" >&2
      exit 1
    fi
    sleep 0.1
  done
fi

# JWT HS256 signing key: generated on first container start (NOT baked into
# the image, so the image is deterministic). Shared by slurmctld (signs
# scontrol tokens) and slurmrestd (verifies). 32 random bytes, owner
# slurm:slurm, mode 0400. Must exist before slurmctld starts.
if [ ! -f /etc/slurm/jwt_hs256.key ]; then
  dd if=/dev/urandom of=/etc/slurm/jwt_hs256.key bs=32 count=1 2>/dev/null
  chown slurm:slurm /etc/slurm/jwt_hs256.key
  chmod 0400 /etc/slurm/jwt_hs256.key
fi

start_mariadb() {
  if [ ! -d /var/lib/mysql/mysql ]; then
    mariadb-install-db --user=mysql --datadir=/var/lib/mysql >/dev/null
  fi

  mariadbd --user=mysql --bind-address=0.0.0.0 --skip-networking=0 &
  pid="$!"

  tries=0
  until mariadb-admin ping -uroot --silent >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -gt 100 ]; then
      echo "mariadbd did not become ready" >&2
      exit 1
    fi
    sleep 0.2
  done

  mariadb -uroot <<SQL
CREATE DATABASE IF NOT EXISTS ${MARIADB_DATABASE:-slurm_acct_db};
CREATE USER IF NOT EXISTS '${MARIADB_USER:-slurm}'@'%' IDENTIFIED BY '${MARIADB_PASSWORD:-pilot107-slurm}';
GRANT ALL PRIVILEGES ON ${MARIADB_DATABASE:-slurm_acct_db}.* TO '${MARIADB_USER:-slurm}'@'%';
FLUSH PRIVILEGES;
SQL

  wait "$pid"
}

if [ "$#" -eq 0 ]; then
  set -- sleep infinity
fi

case "$1" in
  pilot107-slurmdbd)
    chown slurm:slurm /etc/slurm/slurmdbd.conf
    chmod 0600 /etc/slurm/slurmdbd.conf
    exec gosu slurm slurmdbd -D -vvv
    ;;
  pilot107-mariadb)
    start_mariadb
    ;;
  slurmctld|slurmdbd)
    exec gosu slurm "$@"
    ;;
  slurmrestd)
    exec gosu pilot107 "$@"
    ;;
  slurmd)
    touch /tmp/pilot107-a100-gpu0 /tmp/pilot107-a100-gpu1
    exec "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
