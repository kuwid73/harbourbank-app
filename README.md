# orders-app

Orders CRUD service for the **agentic DevOps lab**. CI builds the image, pushes it to GHCR
and bumps the image tag in [orders-gitops](https://github.com/kuwid73/orders-gitops); ArgoCD deploys it.
