# Spec Delta

## ADDED Requirements

### Requirement: Syllune 对外身份

Syllune SHALL 将 `syllune` 作为唯一安装后的 CLI 名称，并在 Python 分发、Nix 包和应用元数据中使用 Syllune 身份。所有面向用户的帮助文本、文档、桌面名称、D-Bus 标识和 Home Manager 选项 MUST 使用 Syllune；旧 `type4me-linux` CLI、包名、Nix 属性、Home Manager 选项和 D-Bus 标识 MUST 不再作为可用兼容入口。

#### Scenario: 新 CLI 和系统集成可识别

- **WHEN** 用户安装 Syllune 并调用 `syllune --help`
- **THEN** 命令 MUST 成功启动并显示 Syllune 身份，且 Nix 元数据、桌面条目、D-Bus 标识和 Home Manager 配置入口使用 Syllune

#### Scenario: 旧 CLI 不提供兼容别名

- **WHEN** 用户在完成切换后调用 `type4me-linux`
- **THEN** 系统 MUST 不将该名称解析为 Syllune 的兼容命令

### Requirement: 干净的持久化状态边界

Syllune SHALL 仅在 `syllune` 命名的 XDG 配置、数据、缓存、状态和运行时目录中读取或写入自身配置、模型、词汇和历史。Syllune MUST 不自动读取、移动、删除或导入旧 `type4me-linux` 目录；迁移文档 MUST 说明旧目录保留和用户手动迁移的边界。

#### Scenario: 仅存在旧状态目录时首次启动

- **WHEN** 用户环境中只存在旧 `type4me-linux` XDG 状态而不存在 Syllune 状态
- **THEN** Syllune MUST 创建并使用自己的目录，且旧目录内容保持未修改

#### Scenario: 用户需要迁移既有数据

- **WHEN** 用户查阅 Syllune 迁移说明
- **THEN** 文档 MUST 明确旧配置、模型、词汇和历史不会自动导入，并说明用户自行备份或迁移的前提

## MODIFIED Requirements

无。

## REMOVED Requirements

无。

## RENAMED Requirements

无。
