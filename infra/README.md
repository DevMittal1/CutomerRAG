# CustomerRAG Infrastructure

Container images, Kubernetes manifests, Makefile automation, and GitHub Actions CI/CD for the six CustomerRAG services.

## Layout

```
infra/
├── docker/
│   ├── Dockerfile          # Multi-app build (APP build-arg)
│   └── .dockerignore
├── kubernetes/
│   ├── base/               # Namespace, ConfigMap, Deployments, Ingress
│   └── overlays/prod/      # Production overlay (replicas, image tags)
└── README.md

Makefile                    # build, push, k8s-apply
.github/workflows/
├── ci.yml                  # PR validation + Docker build
└── docker-publish.yml      # Push to GHCR + optional deploy
```

## Container registry (GHCR)

Images are published to **GitHub Container Registry**:

| Service | Image |
|---------|-------|
| API | `ghcr.io/devmittal1/cutomerrag-api` |
| S3 ingestion | `ghcr.io/devmittal1/cutomerrag-s3-ingestion` |
| Local chunk worker | `ghcr.io/devmittal1/cutomerrag-local-chunk-worker` |
| External chunk worker | `ghcr.io/devmittal1/cutomerrag-external-chunk-worker` |
| Embedding sync | `ghcr.io/devmittal1/cutomerrag-embedding-sync-worker` |
| RAGAS eval | `ghcr.io/devmittal1/cutomerrag-ragas-eval-worker` |

Tags: `latest` (main branch), git SHA, and semver tags (`v*`).

### Enable GHCR for your fork

1. Repo **Settings → Actions → General** — allow workflows to write packages.
2. After first publish, set package visibility to **public** (or configure `imagePullSecrets` for private clusters).
3. For local push: `export GITHUB_TOKEN=<pat with write:packages>` then `make docker-login release`.

## Quick start

### Build locally

```bash
make build-api TAG=dev
docker run --rm -p 8000:8000 --env-file apps/api/.env ghcr.io/devmittal1/cutomerrag-api:dev
```

### Push to GHCR

```bash
export GITHUB_TOKEN=ghp_...
make release TAG=$(git rev-parse --short HEAD)
```

### Deploy to Kubernetes

1. Copy secrets template and fill values (do not commit):

   ```bash
   cp infra/kubernetes/base/secret.example.yaml infra/kubernetes/overlays/prod/secrets.yaml
   # Edit secrets.yaml, then uncomment the resource in overlays/prod/kustomization.yaml
   ```

2. Update `infra/kubernetes/base/configmap.yaml` with your MongoDB, Redis, and Qdrant endpoints.

3. Apply:

   ```bash
   make k8s-apply
   ```

4. Create the namespace secret if not using `secrets.yaml`:

   ```bash
   kubectl create secret generic cutomerrag-secrets -n cutomerrag \
     --from-literal=JWT_SECRET_KEY=... \
     --from-literal=GOOGLE_API_KEY=... \
     # ... see secret.example.yaml
   ```

### GitHub Actions deploy

Add repository secrets:

| Secret | Purpose |
|--------|---------|
| `KUBE_CONFIG_DATA` | Base64-encoded kubeconfig (optional; deploy skipped if unset) |

On push to `main`, workflows build all images, push to GHCR, and apply the prod overlay when `KUBE_CONFIG_DATA` is configured.

## Kubernetes notes

- **API** exposes port 8000 with `/health` probes and a ClusterIP Service on port 80.
- **Workers** are Deployments without Services (no HTTP).
- Data stores (MongoDB, Redis, Qdrant, AWS) are **external** — update ConfigMap/Secrets accordingly.
- Ingress defaults to `api.cutomerrag.example.com` with `ingressClassName: nginx`.

## Makefile reference

| Target | Description |
|--------|-------------|
| `make build` | Build all six images |
| `make build-<app>` | Build one app (`api`, `s3_ingestion`, …) |
| `make push` | Push all images (requires `docker-login`) |
| `make release` | `build` + `push` |
| `make k8s-render` | Print rendered manifests |
| `make k8s-apply` | Apply prod overlay |
| `make ci-validate` | Validate Kustomize output |

Override registry: `make REGISTRY=ghcr.io/myorg TAG=v1.0.0 release`
