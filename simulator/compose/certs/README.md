# Local TLS material

`start-competition.sh` and `start-competition-app-node.sh` generate a short-lived self-signed certificate here when no certificate is present.

Private keys and certificates in this directory are runtime material and must not be committed. A formal deployment must provision its certificate outside the source tree or copy it into this ignored directory during deployment.
