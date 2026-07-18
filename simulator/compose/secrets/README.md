# Local Compose secrets

Run `scripts/init-local-secrets.sh` from the repository root before starting the
application profile. It creates `proxy-hmac.local` with mode `0640`: the host owner
can rotate it and only the host group can read it. API and Web receive that group as
a supplemental group through `PILOT107_SECRET_GID` (default `1000`) because local
Compose secrets preserve bind-mount ownership. Set the variable to the file's host
GID when it differs. The generated file is ignored by Git and must not be copied into
a release bundle or reused on a different host.
