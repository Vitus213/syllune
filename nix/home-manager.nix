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
    enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "是否启用 type4me-linux 语音输入应用。";
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.system}.default;
      defaultText = lib.literalExpression "type4me-linux.packages.${pkgs.system}.default";
      description = "要安装的 type4me-linux 软件包。";
    };

    settings = lib.mkOption {
      type = toml.type;
      default = { };
      description = "写入 type4me-linux/config.toml 的配置。";
    };

    service.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "是否启动常驻的 type4me-linux 图形界面用户服务。";
    };

    shortcuts.sway = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          是否添加 Sway 全局快捷键作为后备方案。GlobalShortcuts 门户后端仍须由用户在
          NixOS 层配置；此 Home Manager 模块不会代为配置门户。
        '';
      };

      holdKey = lib.mkOption {
        type = lib.types.str;
        default = "XF86AudioRecord";
        description = "Sway 按住说话快捷键。";
      };

      toggleKey = lib.mkOption {
        type = lib.types.str;
        default = "$mod+Shift+d";
        description = "Sway 切换录音快捷键。";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    xdg.configFile."type4me-linux/config.toml" = lib.mkIf (cfg.settings != { }) {
      source = toml.generate "type4me-linux-config.toml" cfg.settings;
    };

    systemd.user.services.type4me-linux = lib.mkIf cfg.service.enable {
      Unit = {
        Description = "type4me-linux 常驻语音输入服务";
        After = [ "graphical-session.target" ];
        PartOf = [ "graphical-session.target" ];
      };

      Service = {
        ExecStart = "${lib.getExe cfg.package} service";
        Restart = "on-failure";
        RestartSec = 2;
        TimeoutStopSec = 20;
      };

      Install.WantedBy = [ "graphical-session.target" ];
    };

    wayland.windowManager.sway.extraConfig = lib.mkIf cfg.shortcuts.sway.enable ''
      bindsym --no-repeat ${cfg.shortcuts.sway.holdKey} exec type4me-linux hold-start
      bindsym --release ${cfg.shortcuts.sway.holdKey} exec type4me-linux hold-stop
      bindsym ${cfg.shortcuts.sway.toggleKey} exec type4me-linux toggle
    '';
  };
}
