# Apple `container` (https://github.com/apple/container) wrappers.
IMAGE ?= freee-hr-bot
NAME  ?= freee-hr-bot

.PHONY: build run stop logs restart key test lint

build:
	container build --tag $(IMAGE) --file Containerfile .

run:
	mkdir -p data
	container run --detach --name $(NAME) --env-file .env \
		--volume "$(CURDIR)/data:/app/data" $(IMAGE)

stop:
	-container stop $(NAME)
	-container rm $(NAME)

restart: stop build run

logs:
	container logs --follow $(NAME)

# Generate FERNET_KEY for .env
key:
	@uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .
