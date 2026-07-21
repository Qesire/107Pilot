# Builder stage: compile the Slurm control-plane auth/jwt plugin (auth_jwt.so)
# from Ubuntu's own slurm-wlm source so the plugin ABI matches the installed
# 23.11.x runtime exactly. Ubuntu's slurm-wlm-basic-plugins ships rest_auth_jwt.so
# but NOT auth_jwt.so (control-plane). We build only that plugin with --with-jwt
# + libjwt-dev (libjwt2 runtime is already a slurmrestd dependency).
# ubuntu:24.04, pinned 2026-07-21 (digest pin for reproducible base).
FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90 AS jwt-builder
ENV DEBIAN_FRONTEND=noninteractive

# deb822: append deb-src so apt-get source / build-dep work.
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        dpkg-dev \
        build-essential \
        pkg-config \
        ca-certificates \
    && cd /tmp \
    && apt-get source slurm-wlm \
    && apt-get build-dep -y slurm-wlm \
    && apt-get install -y --no-install-recommends libjwt-dev \
    && cd /tmp/slurm-wlm-*/ \
    && ./configure \
        --with-jwt \
        --prefix=/usr \
        --sysconfdir=/etc/slurm \
        --libdir=/usr/lib/x86_64-linux-gnu \
        --localstatedir=/var \
        --enable-shared \
        --disable-static \
    && make -C src/plugins/auth/jwt

# Final stage: the simulator image with auth_jwt.so installed.
# ubuntu:24.04, pinned 2026-07-21 (digest pin for reproducible base).
FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        gosu \
        mariadb-client \
        mariadb-server \
        munge \
        python3 \
        python-is-python3 \
        slurm-client \
        slurm-wlm \
        slurmdbd \
        slurmrestd \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Install the control-plane JWT auth plugin built above. Slurm loads plugins
# from /usr/lib/x86_64-linux-gnu/slurm-wlm (Ubuntu plugin dir).
COPY --from=jwt-builder /tmp/slurm-wlm-*/src/plugins/auth/jwt/.libs/auth_jwt.so \
     /usr/lib/x86_64-linux-gnu/slurm-wlm/auth_jwt.so

RUN mkdir -p /public/home /var/lib/slurm /var/lib/pilot107 \
    && getent group slurm >/dev/null || groupadd --gid 64030 slurm \
    && id slurm >/dev/null 2>&1 || useradd --uid 64030 --gid slurm --home-dir /var/lib/slurm --create-home --shell /usr/sbin/nologin slurm \
    && getent group pilot107 >/dev/null || groupadd --gid 10700 pilot107 \
    && id pilot107 >/dev/null 2>&1 || useradd --uid 10700 --gid pilot107 --home-dir /var/lib/pilot107 --create-home --shell /bin/bash pilot107 \
    && id alice >/dev/null 2>&1 || useradd --uid 11001 --create-home --home-dir /public/home/alice --shell /bin/bash alice \
    && id bob >/dev/null 2>&1 || useradd --uid 11002 --create-home --home-dir /public/home/bob --shell /bin/bash bob

RUN mkdir -p \
        /etc/munge \
        /run/munge \
        /var/lib/munge \
        /var/log/munge \
        /var/log/slurm \
        /run/mysqld \
        /var/run/slurm \
        /var/spool/slurm/ctld \
        /var/spool/slurm/d \
        /public/home/alice \
        /public/home/bob \
        /public/app \
        /pilot107/evidence-derived \
    && chown -R munge:munge /etc/munge /run/munge /var/lib/munge /var/log/munge \
    && chown -R mysql:mysql /run/mysqld /var/lib/mysql \
    && chown -R slurm:slurm /var/log/slurm /var/run/slurm /var/spool/slurm \
    && chown alice:alice /public/home/alice \
    && chown bob:bob /public/home/bob \
    && chmod 0700 /public/home/alice /public/home/bob \
    && chmod 0755 /public/app

RUN mkdir -p /etc/pilot107

COPY slurmdbd.conf /etc/slurm/slurmdbd.conf
COPY version-manifest.json /etc/pilot107/slurm-sim-version-manifest.json
RUN chown slurm:slurm /etc/slurm/slurmdbd.conf \
    && chmod 0600 /etc/slurm/slurmdbd.conf \
    && rm -f /etc/slurm/cgroup.conf

# JWT HS256 signing key shared by slurmctld (signs scontrol token) and
# slurmrestd (verifies). Baked into the image so every container that uses
# this image shares the same key (no host bind-mount / permission issues).
# 32 random bytes, owner slurm:slurm, mode 0400.
RUN dd if=/dev/urandom of=/etc/slurm/jwt_hs256.key bs=32 count=1 \
    && chown slurm:slurm /etc/slurm/jwt_hs256.key \
    && chmod 0400 /etc/slurm/jwt_hs256.key

COPY munge.key.base64 /tmp/munge.key.base64
RUN base64 -d /tmp/munge.key.base64 > /etc/munge/munge.key \
    && chown munge:munge /etc/munge/munge.key \
    && chmod 0400 /etc/munge/munge.key \
    && rm /tmp/munge.key.base64

COPY docker-entrypoint.sh /usr/local/bin/pilot107-slurm-entrypoint
RUN chmod 0755 /usr/local/bin/pilot107-slurm-entrypoint

ENTRYPOINT ["tini", "--", "pilot107-slurm-entrypoint"]
CMD ["sleep", "infinity"]
