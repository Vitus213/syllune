set dotenv-load := false

test:
    nix develop -c cargo test --all-targets

lint:
    nix develop -c cargo fmt --check
    nix develop -c cargo clippy --all-targets --all-features -- -D warnings

check:
    just lint
    just test
    nix flake check -L

run *ARGS:
    nix run . -- {{ARGS}}
