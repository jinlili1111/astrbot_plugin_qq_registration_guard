# QQ群注册守卫

AstrBot 插件：用户在指定 QQ 群内发送 `/注册 密码`，插件自动读取发送者 QQ 号作为游戏账号，写入 MySQL `user` 表；用户退群后自动封号，并提供定时群成员校验兜底。

## 功能

- 群内注册：账号默认使用发送者 QQ 号。
- 群成员校验：注册前调用 OneBot `get_group_member_info` 确认用户仍在指定群。
- 退群封号：监听 OneBot `group_decrease` notice。
- 定时兜底：周期拉取群成员列表，连续缺席达到阈值后封号；依赖审计表记录插件注册过的 QQ。
- 审计表：记录注册、封号、解封等插件动作。
- 配置化数据库连接：通过 AstrBot WebUI 插件配置填写。

## 要求

- AstrBot 使用 `aiocqhttp` / OneBot v11 适配器，例如 NapCat。
- OneBot 端需要能调用 `get_group_member_info` / `get_group_member_list`。
- 自动退群封号需要 OneBot 端转发群成员减少 notice。
- MySQL 用户库中存在 `user` 表，登录字段兼容 `Name` + `Password`，封禁字段为 `IsBan` 或 `bantype`。

## 配置

在 AstrBot WebUI 插件配置中填写：

- `database`: MySQL 用户库连接。
- `managed_group_ids`: 允许注册并受守卫的 QQ 群号列表。
- `password_min_length`: 密码最小长度。
- `auto_ban_on_leave`: 是否收到退群事件立即封号。
- `periodic_check_enabled`: 是否启用定时兜底。
- `periodic_check_minutes`: 定时校验间隔。
- `periodic_miss_threshold`: 连续缺席多少次后封号。

## 命令

```text
/注册 密码
/查注册
```

## 安全说明

本插件按现有 QQSpeed GM 后台兼容方式写入 `Password` 字段。如果你的服务端支持密码哈希，应优先改造服务端登录链路后再改插件存储策略。
