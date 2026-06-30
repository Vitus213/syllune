self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.type4me-linux;
  toml = pkgs.formats.toml { };
in
{
  options.programs.type4me-linux = {
    enable = lib.mkEnableOption "type4me-linux voice input";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.system}.default;
      defaultText = lib.literalExpression "type4me-linux.packages.${pkgs.system}.default";
      description = "type4me-linux package to install.";
    };

    settings = lib.mkOption {
      type = toml.type;
      default = { };
      description = "Configuration written to type4me-linux/config.toml.";
    };

    service.enable = lib.mkEnableOption "type4me-linux user daemon";
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    xdg.configFile."type4me-linux/config.toml" = lib.mkIf (cfg.settings != { }) {
      source = toml.generate "type4me-linux-config.toml" cfg.settings;
    };

    systemd.user.services.type4me-linux = lib.mkIf cfg.service.enable {
      Unit = {
        Description = "type4me-linux voice input daemon";
        After = [ "graphical-session.target" ];
        PartOf = [ "graphical-session.target" ];
      };

      Service = {
        ExecStart = "${lib.getExe cfg.package} daemon";
        Restart = "on-failure";
        RestartSec = 2;
      };

      Install.WantedBy = [ "graphical-session.target" ];
    };
  };
}
