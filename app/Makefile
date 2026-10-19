IMAGE_TAG ?= 0.1.0
APPLICATION_IMAGE := running-drafts-backend:$(IMAGE_TAG)

.PHONY: image test

image:
	docker build --pull --target api --tag $(APPLICATION_IMAGE) .

test:
	uv run ruff check src tests
	uv run pytest -q
