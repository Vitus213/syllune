from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_home_manager_service_and_sway_shortcuts() -> None:
    module_path = json.dumps(str(REPOSITORY_ROOT / "nix" / "home-manager.nix"))
    nixpkgs_path = json.dumps(os.environ["TYPE4ME_NIXPKGS"])
    expression = f"""
      let
        system = builtins.currentSystem;
        pkgs = import (builtins.toPath {nixpkgs_path}) {{ inherit system; }};
        lib = pkgs.lib;
        package = pkgs.writeShellScriptBin "type4me-linux" "exit 0";
        self = {{ packages.${{system}}.default = package; }};
        evaluated = lib.evalModules {{
          specialArgs = {{ inherit pkgs; }};
          modules = [
            {{
              options = {{
                home.packages = lib.mkOption {{
                  type = lib.types.listOf lib.types.package;
                  default = [ ];
                }};
                xdg.configFile = lib.mkOption {{
                  type = lib.types.attrsOf lib.types.anything;
                  default = {{ }};
                }};
                systemd.user.services = lib.mkOption {{
                  type = lib.types.attrsOf lib.types.anything;
                  default = {{ }};
                }};
                wayland.windowManager.sway.extraConfig = lib.mkOption {{
                  type = lib.types.lines;
                  default = "";
                }};
              }};
            }}
            (import {module_path} self)
            {{
              programs.type4me-linux = {{
                enable = true;
                package = package;
                service.enable = true;
                shortcuts.sway = {{
                  enable = true;
                  holdKey = "F10";
                  toggleKey = "$mod+F10";
                }};
              }};
            }}
          ];
        }};
        service = evaluated.config.systemd.user.services.type4me-linux;
      in {{
        execStart = service.Service.ExecStart;
        restart = service.Service.Restart;
        timeoutStopSec = service.Service.TimeoutStopSec;
        after = service.Unit.After;
        partOf = service.Unit.PartOf;
        wantedBy = service.Install.WantedBy;
        description = service.Unit.Description;
        swayConfig = evaluated.config.wayland.windowManager.sway.extraConfig;
        portalDescription =
          evaluated.options.programs.type4me-linux.shortcuts.sway.enable.description;
      }}
    """

    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    actual = json.loads(result.stdout)

    assert actual["execStart"].endswith("/bin/type4me-linux service")
    assert actual["restart"] == "on-failure"
    assert actual["timeoutStopSec"] == 20
    assert actual["after"] == ["graphical-session.target"]
    assert actual["partOf"] == ["graphical-session.target"]
    assert actual["wantedBy"] == ["graphical-session.target"]
    assert actual["description"] == "type4me-linux 常驻语音输入服务"
    assert actual["swayConfig"].splitlines() == [
        "bindsym --no-repeat F10 exec type4me-linux hold-start",
        "bindsym --release F10 exec type4me-linux hold-stop",
        "bindsym $mod+F10 exec type4me-linux toggle",
    ]
    assert "NixOS 层" in actual["portalDescription"]
    assert "不会代为配置门户" in actual["portalDescription"]
