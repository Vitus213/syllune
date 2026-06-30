set dotenv-load := false

test:
    python -m pytest

lint:
    ruff check .
    ruff format --check .

check:
    just lint
    just test
    nix flake check

precommit:
    pre-commit run --all-files

run *ARGS:
    nix run . -- {{ARGS}}
