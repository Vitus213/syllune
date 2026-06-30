set dotenv-load := false

test:
    pytest

check:
    nix flake check

run *ARGS:
    nix run . -- {{ARGS}}

