# CustomerRAG — build, push, and deploy
# Registry: GitHub Container Registry (GHCR)

SHELL := /bin/bash
.RECIPEPREFIX := >

REGISTRY ?= ghcr.io/devmittal1
IMAGE_PREFIX ?= cutomerrag
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)
DOCKERFILE := infra/docker/Dockerfile
DOCKER_CONTEXT := .
K8S_OVERLAY ?= infra/kubernetes/overlays/prod

APPS := api s3_ingestion local_chunk_worker external_chunk_worker embedding_sync_worker ragas_eval_worker

# Map app directory names to image suffixes (underscores -> hyphens).
define image_name
$(REGISTRY)/$(IMAGE_PREFIX)-$(subst _,-,$(1))
endef

.PHONY: help
help:
> @echo "CustomerRAG infrastructure targets"
> @echo ""
> @echo "  make build              Build all service images"
> @echo "  make build-api          Build a single image (api, s3_ingestion, ...)"
> @echo "  make push               Push all images to $(REGISTRY)"
> @echo "  make release            Build and push all images with TAG=$(TAG)"
> @echo "  make docker-login       Log in to GHCR (needs GITHUB_TOKEN)"
> @echo "  make k8s-render         Print rendered Kubernetes manifests"
> @echo "  make k8s-apply          Apply prod overlay (needs secrets + kubectl)"
> @echo "  make k8s-delete         Delete prod overlay resources"
> @echo "  make ci-validate        Validate kustomize and Dockerfiles"
> @echo ""
> @echo "Variables: REGISTRY=$(REGISTRY) IMAGE_PREFIX=$(IMAGE_PREFIX) TAG=$(TAG)"

.PHONY: build
build: $(addprefix build-,$(APPS))

.PHONY: build-%
build-%:
> @test -d apps/$* || (echo "Unknown app: $*" && exit 1)
> docker build \
>   --build-arg APP=$* \
>   -f $(DOCKERFILE) \
>   -t $(call image_name,$*):$(TAG) \
>   -t $(call image_name,$*):latest \
>   $(DOCKER_CONTEXT)
> @echo "Built $(call image_name,$*):$(TAG)"

.PHONY: push
push: docker-login $(addprefix push-,$(APPS))

.PHONY: push-%
push-%:
> docker push $(call image_name,$*):$(TAG)
> docker push $(call image_name,$*):latest

.PHONY: release
release: build push

.PHONY: docker-login
docker-login:
> @test -n "$$GITHUB_TOKEN" || (echo "Set GITHUB_TOKEN with read:packages,write:packages" && exit 1)
> echo "$$GITHUB_TOKEN" | docker login $(REGISTRY) -u "$${GITHUB_ACTOR:-$$(git config user.name)}" --password-stdin

.PHONY: k8s-render
k8s-render:
> kubectl kustomize $(K8S_OVERLAY)

.PHONY: k8s-apply
k8s-apply:
> kubectl apply -k $(K8S_OVERLAY)

.PHONY: k8s-delete
k8s-delete:
> kubectl delete -k $(K8S_OVERLAY) --ignore-not-found

.PHONY: ci-validate
ci-validate:
> @if command -v kubectl >/dev/null 2>&1; then \
>     kubectl kustomize infra/kubernetes/base > /dev/null; \
>     kubectl kustomize $(K8S_OVERLAY) > /dev/null; \
>   elif command -v kustomize >/dev/null 2>&1; then \
>     kustomize build infra/kubernetes/base > /dev/null; \
>     kustomize build $(K8S_OVERLAY) > /dev/null; \
>   else \
>     echo "Install kubectl or kustomize to validate manifests"; exit 1; \
>   fi
> @echo "Kustomize OK"

.PHONY: clean
clean:
> @docker images --format '{{.Repository}}:{{.Tag}}' | grep '$(IMAGE_PREFIX)-' | xargs -r docker rmi || true
