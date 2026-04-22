.PHONY: all deb rpm native-deb native-rpm docker docker-push clean lint help

VERSION     := $(shell cat VERSION)
DIST_DIR    := dist
IMAGE_NAME  := kentik-device-onboarder
IMAGE_TAG   ?= $(VERSION)
REGISTRY    ?= 

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

all: deb rpm ## Build both .deb and .rpm packages

deb: ## Build the Debian (.deb) package using fpm
	bash packaging/build-packages.sh deb

rpm: ## Build the RPM (.rpm) package using fpm
	bash packaging/build-packages.sh rpm

native-deb: ## Build .deb using native dpkg-buildpackage (requires build-essential, debhelper)
	@echo "Building native Debian package (version $(VERSION))..."
	dpkg-buildpackage -us -uc -b --rules-file=packaging/debian/rules

native-rpm: ## Build .rpm using rpmbuild
	@echo "Building native RPM package (version $(VERSION))..."
	rpmbuild -bb \
		--define "_sourcedir $(CURDIR)" \
		--define "_specdir   $(CURDIR)/packaging/rpm" \
		--define "_builddir  $(CURDIR)/build" \
		--define "_rpmdir    $(CURDIR)/$(DIST_DIR)" \
		--define "_srcrpmdir $(CURDIR)/$(DIST_DIR)" \
		packaging/rpm/kentik-device-onboarder.spec

docker: ## Build the Docker image
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) -t $(IMAGE_NAME):latest .

docker-push: docker ## Tag and push the Docker image to REGISTRY
	@if [ -z "$(REGISTRY)" ]; then echo "Set REGISTRY=registry.example.com/org before pushing" >&2; exit 1; fi
	docker tag $(IMAGE_NAME):$(IMAGE_TAG) $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
	docker tag $(IMAGE_NAME):$(IMAGE_TAG) $(REGISTRY)/$(IMAGE_NAME):latest
	docker push $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
	docker push $(REGISTRY)/$(IMAGE_NAME):latest

lint: ## Lint the Python source with flake8 (install separately)
	flake8 kentik_device_onboarder.py

clean: ## Remove build artifacts
	rm -rf $(DIST_DIR) build
	find packaging/debian -name '*.debhelper*' -delete 2>/dev/null || true
	find packaging/debian -name 'debhelper-build-stamp' -delete 2>/dev/null || true
	find packaging/debian -name 'files' -delete 2>/dev/null || true
	find packaging/debian -name 'kentik-device-onboarder' -type d -exec rm -rf {} + 2>/dev/null || true
