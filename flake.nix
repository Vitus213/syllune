{
  description = "面向 NixOS 的 Type4Me Linux 语音输入应用";

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
        pythonEnv = python.withPackages (
          ps: with ps; [
            numpy
            pygobject3
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

          nativeBuildInputs = [
            pkgs.gobject-introspection
            pkgs.wrapGAppsHook4
          ];

          dependencies = with python.pkgs; [
            numpy
            pygobject3
          ];

          buildInputs = [
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.adwaita-icon-theme
          ];

          nativeCheckInputs = with python.pkgs; [
            pytest
            pytest-cov
            pkgs.xvfb
            pkgs.xvfb-run
            pkgs.nix
          ];

          checkPhase = ''
            runHook preCheck
            export NIX_CONFIG="experimental-features = nix-command flakes"
            export TYPE4ME_NIXPKGS="${pkgs.path}"
            export HOME="$TMPDIR/home"
            export XDG_CONFIG_HOME="$TMPDIR/xdg/config"
            export XDG_DATA_HOME="$TMPDIR/xdg/data"
            export XDG_CACHE_HOME="$TMPDIR/xdg/cache"
            export XDG_STATE_HOME="$TMPDIR/xdg/state"
            export XDG_RUNTIME_DIR="$TMPDIR/xdg/runtime"
            mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" \
              "$XDG_CACHE_HOME" "$XDG_STATE_HOME" "$XDG_RUNTIME_DIR"
            chmod 700 "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" \
              "$XDG_CACHE_HOME" "$XDG_STATE_HOME" "$XDG_RUNTIME_DIR"
            xvfb-run -a pytest
            runHook postCheck
          '';

          postInstall = ''
            install -Dm644 data/io.github.vitus.Type4Me.desktop \
              "$out/share/applications/io.github.vitus.Type4Me.desktop"
            install -Dm644 data/vocabulary/hotwords.json \
              "$out/share/type4me-linux/vocabulary/hotwords.json"
            install -Dm644 data/vocabulary/snippets.json \
              "$out/share/type4me-linux/vocabulary/snippets.json"
          '';

          dontWrapGApps = true;

          makeWrapperArgs = [
            "--prefix PATH : ${
              pkgs.lib.makeBinPath [
                pkgs.pipewire
                sherpaOnnx
                pkgs.wl-clipboard
                pkgs.wtype
                pkgs.libnotify
              ]
            }"
            "--prefix PYTHONPATH : ${sherpaOnnx.python}"
            "--prefix GI_TYPELIB_PATH : ${
              pkgs.lib.makeSearchPath "lib/girepository-1.0" [
                pkgs.glib.out
                pkgs.gobject-introspection
                pkgs.gdk-pixbuf
                pkgs.graphene
                pkgs.harfbuzz
                pkgs.pango.out
                pkgs.gsettings-desktop-schemas
                pkgs.gtk4
                pkgs.libadwaita
                pkgs.librsvg
              ]
            }"
            "--prefix XDG_DATA_DIRS : ${
              pkgs.lib.makeSearchPath "share" [
                pkgs.gtk4
                pkgs.libadwaita
                pkgs.adwaita-icon-theme
                pkgs.gsettings-desktop-schemas
              ]
            }"
            "--prefix GIO_EXTRA_MODULES : ${pkgs.lib.makeSearchPath "lib/gio/modules" [ pkgs.glib-networking ]}"
          ]
          ++ pkgs.lib.optional (
            system == "x86_64-linux"
          ) "--prefix LD_PRELOAD : ${pkgs.cudaPackages_12.cuda_nvrtc.lib}/lib/libnvrtc.so.12";

          preFixup = ''
            makeWrapperArgs+=("''${gappsWrapperArgs[@]}")
          '';

          meta = {
            description = "集成 SenseVoice 与 Qwen3-ASR 的 Linux 语音输入应用";
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
        packages.syllune = syllune;

        apps.default = flake-utils.lib.mkApp { drv = type4me-linux; };
        apps.syllune = flake-utils.lib.mkApp { drv = syllune; };

        checks.default = type4me-linux;

        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            python.pkgs.ruff
            pkgs.pre-commit
            pkgs.just
            pkgs.nixfmt
            pkgs.cargo
            pkgs.rustc
            pkgs.pipewire
            sherpaOnnx
            onnxruntime
            pkgs.wl-clipboard
            pkgs.wtype
            pkgs.xvfb
            pkgs.xvfb-run
          ];
          nativeBuildInputs = [
            pkgs.gobject-introspection
            pkgs.wrapGAppsHook4
          ];

          buildInputs = [
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.adwaita-icon-theme
          ];

          PYTHONPATH = "${sherpaOnnx.python}";
          SHERPA_ONNX_LIB_DIR = "${sherpaRuntimeLib}/lib";
          LD_LIBRARY_PATH = "${sherpaRuntimeLib}/lib";
          TYPE4ME_NIXPKGS = "${pkgs.path}";
          LD_PRELOAD = pkgs.lib.optionalString (
            system == "x86_64-linux"
          ) "${pkgs.cudaPackages_12.cuda_nvrtc.lib}/lib/libnvrtc.so.12";
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
