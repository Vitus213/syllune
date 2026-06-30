{
  description = "Nix-packaged Linux voice input pipeline inspired by Type4Me";

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
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;
        pythonEnv = python.withPackages (
          ps: with ps; [
            pytest
            pytest-cov
          ]
        );
        type4me-linux = python.pkgs.buildPythonApplication {
          pname = "type4me-linux";
          version = "0.1.0";
          pyproject = true;
          src = ./.;

          build-system = with python.pkgs; [
            setuptools
            wheel
          ];

          nativeCheckInputs = with python.pkgs; [
            pytest
            pytest-cov
          ];

          checkPhase = ''
            runHook preCheck
            pytest
            runHook postCheck
          '';

          makeWrapperArgs = [
            "--prefix PATH : ${
              pkgs.lib.makeBinPath [
                pkgs.pipewire
                pkgs.sherpa-onnx
                pkgs.wl-clipboard
                pkgs.wtype
                pkgs.libnotify
              ]
            }"
          ];

          meta = {
            description = "Linux voice input pipeline with SenseVoice/Qwen3-ASR hooks";
            homepage = "https://github.com/vitus/type4me-linux";
            license = pkgs.lib.licenses.mit;
            mainProgram = "type4me-linux";
            platforms = pkgs.lib.platforms.linux;
          };
        };
      in
      {
        packages.default = type4me-linux;
        packages.type4me-linux = type4me-linux;

        apps.default = flake-utils.lib.mkApp { drv = type4me-linux; };

        checks.default = type4me-linux;

        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            python.pkgs.ruff
            pkgs.pre-commit
            pkgs.just
            pkgs.nixfmt
            pkgs.pipewire
            pkgs.sherpa-onnx
            pkgs.wl-clipboard
            pkgs.wtype
          ];
        };

        formatter = pkgs.writeShellApplication {
          name = "type4me-linux-format";
          runtimeInputs = [ pkgs.nixfmt ];
          text = ''
            nixfmt flake.nix nix/home-manager.nix
          '';
        };
      }
    )
    // {
      overlays.default = final: prev: {
        type4me-linux = self.packages.${prev.system}.default;
      };

      homeManagerModules.default = import ./nix/home-manager.nix self;
    };
}
