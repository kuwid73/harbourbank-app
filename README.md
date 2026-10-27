# harbourbank-app

Core banking service (`accounts-api`) for **Harbour Bank**, a fictional bank used in an agentic
DevOps lab. Synthetic data only.

CI builds the image, pushes it to GHCR and bumps the image tag in
[harbourbank-gitops](https://github.com/kuwid73/harbourbank-gitops); ArgoCD deploys it.
