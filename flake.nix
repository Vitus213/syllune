{
  description = "Syllune：面向 NixOS/Wayland 的原生 Rust 实时语音输入";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
        python = pkgs.python312;
        onnxruntime =
          if system == "x86_64-linux" then
            pkgs.onnxruntime.override {
              cudaSupport = true;
              cudaPackages = pkgs.cudaPackages_12;
            }
          else
            pkgs.onnxruntime;
        sherpaOnnx = pkgs.sherpa-onnx.override (
          {
            python3Packages = python.pkgs;
          }
          // pkgs.lib.optionalAttrs (system == "x86_64-linux") {
            cudaSupport = true;
            inherit onnxruntime;
          }
        );
        sherpaRuntimeLib = pkgs.symlinkJoin {
          name = "syllune-sherpa-runtime-libs";
          paths = [ sherpaOnnx onnxruntime ];
        };
        syllune = pkgs.rustPlatform.buildRustPackage {
          pname = "syllune";
          version = "0.1.0";
          src = ./rust;
          cargoLock.lockFile = ./rust/Cargo.lock;
          nativeBuildInputs = [ pkgs.makeWrapper pkgs.pkg-config ];
          buildInputs = [ sherpaOnnx onnxruntime ];
          nativeCheckInputs = [ pkgs.pipewire pkgs.wtype pkgs.wl-clipboard ];
          SHERPA_ONNX_LIB_DIR = sherpaRuntimeLib + "/lib";
          postInstall = ''
            wrapProgram "$out/bin/syllune" \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.pipewire pkgs.wtype pkgs.wl-clipboard ]} \
              --prefix LD_LIBRARY_PATH : ${sherpaRuntimeLib}/lib
          '';
          meta = {
            description = "Fast realtime voice input for Linux";
            homepage = "https://github.com/vitus/type4me-linux";
            license = pkgs.lib.licenses.mit;
            mainProgram = "syllune";
            platforms = pkgs.lib.platforms.linux;
          };
        };
      in
      {
        packages.default = syllune;
        packages.syllune = syllune;

        apps.default = flake-utils.lib.mkApp { drv = syllune; };

        checks.default = syllune;

        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.just
            pkgs.nixfmt
            pkgs.cargo
            pkgs.rustc
            pkgs.pipewire
            sherpaOnnx
            onnxruntime
            pkgs.wl-clipboard
            pkgs.wtype
          ];

          SHERPA_ONNX_LIB_DIR = "${sherpaRuntimeLib}/lib";
          LD_LIBRARY_PATH = "${sherpaRuntimeLib}/lib";
        };

        formatter = pkgs.writeShellApplication {
          name = "syllune-format";
          runtimeInputs = [ pkgs.nixfmt ];
          text = ''
            nixfmt flake.nix nix/home-manager.nix
          '';
        };
      }
    )
    // {
      overlays.default = final: prev: {
        syllune = self.packages.${prev.system}.syllune;
      };

      homeManagerModules.default = import ./nix/home-manager.nix self;
    };
}
