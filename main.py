import asyncio
import secrets
import string
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pymysql

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


class QQRegistrationGuardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._check_task: asyncio.Task | None = None
        self._miss_counts: dict[str, int] = {}
        self._bot: Any | None = None
        self._logged_missing_bot = False

    async def initialize(self):
        if self._get_bool("create_audit_table", True):
            try:
                await asyncio.to_thread(self._ensure_audit_table)
            except Exception as exc:
                logger.warning(
                    "QQRegistrationGuardPlugin audit table init skipped. "
                    f"Please check database config: {exc}"
                )
        if self._get_bool("periodic_check_enabled", True):
            self._check_task = asyncio.create_task(self._periodic_group_check())
        logger.info("QQRegistrationGuardPlugin initialized")

    async def terminate(self):
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
            self._check_task = None

    @filter.command("注册")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def register_account(self, event: AiocqhttpMessageEvent, password: str = ""):
        """私聊注册账号；账号用QQ号。/注册 直接生成随机密码，/注册 密码 用自定义密码。"""
        self._remember_bot(event)
        qq = event.get_sender_id()
        group_id = event.get_group_id()
        password = (password or "").strip()

        # 密码属于隐私（尤其自动生成的初始密码），只在私聊处理
        if group_id:
            yield event.plain_result(
                "请私聊我发送 /注册 即可自动注册（不用自己想密码，我会给你生成一个）。\n"
                "⚠️ 也不要在群里发密码，会被其他群成员看到。"
            )
            return

        auto_generated = False
        if not password:
            password = self._generate_password()
            auto_generated = True
        else:
            password_error = self._password_error(password)
            if password_error:
                yield event.plain_result(
                    f"❌ 注册失败：{password_error}\n\n" + self._registration_help(qq)
                )
                return

        member_group_id = await self._find_member_group(event, qq)
        if not member_group_id:
            yield event.plain_result(
                "❌ 注册失败：没有检测到你在官方受管QQ群内"
                "（也可能是机器人暂时查不到群成员信息）。\n\n"
                + self._registration_help(qq)
            )
            return

        result = await asyncio.to_thread(
            self._register_user, qq, password, member_group_id
        )
        if not result["success"]:
            # _register_user 已给出针对性原因和后续指引
            yield event.plain_result(result["message"])
            return

        await asyncio.to_thread(
            self._record_audit, qq, member_group_id, "register", "user registered"
        )
        if auto_generated:
            yield event.plain_result(
                "✅ 注册成功！\n"
                f"账号（你的QQ号）：{qq}\n"
                f"初始密码：{password}\n\n"
                "⚠️ 这是随机生成的密码，请先复制保存，再进游戏登录。\n"
                "想改成自己的密码：私聊我发送  /找回密码 新密码\n"
                "（新密码至少8位，且同时包含大写字母、小写字母、数字和标点，例：Abc12345!）"
            )
        else:
            yield event.plain_result(
                f"✅ 注册成功！\n账号（你的QQ号）：{qq}\n"
                "现在可以用这个账号和你刚设置的密码登录游戏了。\n"
                "想改密码：私聊我发送  /找回密码 新密码"
            )

    @filter.command("找回密码")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def recover_password(self, event: AiocqhttpMessageEvent, password: str = ""):
        """私聊找回密码，只允许修改发送者QQ对应的账号。"""
        self._remember_bot(event)
        qq = event.get_sender_id()
        group_id = event.get_group_id()
        password = (password or "").strip()

        if group_id:
            yield event.plain_result("密码属于隐私，请私聊机器人发送：/找回密码 新密码")
            return
        if not password:
            yield event.plain_result("用法：/找回密码 新密码")
            return
        password_error = self._password_error(password)
        if password_error:
            yield event.plain_result(password_error)
            return

        member_group_id = await self._find_member_group(event, qq)
        if not member_group_id:
            yield event.plain_result("未检测到你在受管群内，不能找回密码。")
            return

        result = await asyncio.to_thread(
            self._reset_password, qq, password, member_group_id
        )
        if result["success"]:
            await asyncio.to_thread(
                self._record_audit,
                qq,
                member_group_id,
                "reset_password",
                "password reset by owner",
            )
        yield event.plain_result(result["message"])

    @filter.command("查注册")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def query_registration(self, event: AiocqhttpMessageEvent):
        """查询自己的注册与封禁状态。"""
        self._remember_bot(event)
        qq = event.get_sender_id()
        user = await asyncio.to_thread(self._get_user_by_name, qq)
        if not user:
            yield event.plain_result("当前QQ尚未注册。\n私聊发送 /注册 密码 即可注册。")
            return
        ban_value = self._get_user_ban_value(user)
        if ban_value:
            yield event.plain_result(
                f"账号：{qq}\n状态：已封禁\n"
                "如果是退群被自动封的，重新加入官方QQ群后，私聊发送 /解封 即可自助解封。"
            )
        else:
            yield event.plain_result(f"账号：{qq}\n状态：正常")

    @filter.command("解封")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def unban_account(self, event: AiocqhttpMessageEvent):
        """自助解封：仅能解除本人因退群被插件自动封禁的账号。"""
        self._remember_bot(event)
        qq = event.get_sender_id()

        user = await asyncio.to_thread(self._get_user_by_name, qq)
        if not user:
            yield event.plain_result(
                "❌ 解封失败：当前QQ还没有注册账号。\n请先私聊发送：/注册 密码"
            )
            return
        if not self._get_user_ban_value(user):
            yield event.plain_result("✅ 你的账号目前是正常状态，无需解封。")
            return

        member_group_id = await self._find_member_group(event, qq)
        if not member_group_id:
            yield event.plain_result(
                "❌ 解封失败：没有检测到你在官方受管QQ群内。\n"
                "账号是退群后被自动封禁的，请先重新加入官方QQ群，再私聊发送 /解封。"
            )
            return

        if self._get_bool("unban_only_auto_banned", False) and not await asyncio.to_thread(
            self._can_self_unban, qq
        ):
            yield event.plain_result(
                "❌ 解封失败：你的账号不是因退群被自动封禁的（可能是管理员手动封禁）。\n"
                "这种情况请联系管理员处理。"
            )
            return

        ok = await asyncio.to_thread(self._unban_user, qq, member_group_id, "self_unban")
        if ok:
            yield event.plain_result(
                f"✅ 解封成功！账号：{qq}\n现在可以正常登录游戏了。"
            )
        else:
            yield event.plain_result(
                "❌ 解封失败：服务器处理时出现异常，请稍后再试或联系管理员。"
            )

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_all_events(self, event: AstrMessageEvent):
        """监听 OneBot notice：退群自动封号 / 重新进群自动解封。"""
        self._remember_bot(event)
        raw = getattr(event.message_obj, "raw_message", None)
        if not raw:
            return
        try:
            post_type = raw.get("post_type")
            notice_type = raw.get("notice_type")
            group_id = str(raw.get("group_id") or "")
            user_id = str(raw.get("user_id") or "")
        except AttributeError:
            return
        if post_type != "notice":
            return
        if not group_id or not user_id or group_id not in self._managed_groups():
            return

        if notice_type == "group_decrease":
            if not self._get_bool("auto_ban_on_leave", True):
                return
            ok = await asyncio.to_thread(
                self._ban_registered_user, user_id, group_id, "leave_notice"
            )
            if ok:
                await self._notify_admin(
                    event, f"检测到 QQ {user_id} 退出群 {group_id}，已自动封号。"
                )
        elif notice_type == "group_increase":
            if not self._get_bool("auto_unban_on_rejoin", False):
                return
            user = await asyncio.to_thread(self._get_user_by_name, user_id)
            if not user or not self._get_user_ban_value(user):
                return
            if self._get_bool("unban_only_auto_banned", False) and not await asyncio.to_thread(
                self._can_self_unban, user_id
            ):
                return
            ok = await asyncio.to_thread(
                self._unban_user, user_id, group_id, "auto_unban"
            )
            if ok:
                await self._notify_admin(
                    event, f"检测到 QQ {user_id} 重新加入群 {group_id}，已自动解封。"
                )

    def _remember_bot(self, event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        if bot:
            self._bot = bot
            self._logged_missing_bot = False

    async def _is_group_member(
        self, event: AiocqhttpMessageEvent, group_id: str, qq: str
    ) -> bool:
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(qq), no_cache=True
            )
            return bool(info)
        except Exception as exc:
            logger.warning(f"get_group_member_info failed group={group_id} qq={qq}: {exc}")
            return False

    async def _find_member_group(
        self, event: AiocqhttpMessageEvent, qq: str
    ) -> str | None:
        for group_id in self._managed_groups():
            if await self._is_group_member(event, group_id, qq):
                return group_id
        return None

    def _effective_min_length(self) -> int:
        return max(8, self._get_int("password_min_length", 8))

    def _registration_help(self, qq: str | None = None) -> str:
        min_length = self._effective_min_length()
        lines = [
            "正确的注册方式：",
            "1. 先加入官方受管QQ群（不在群里无法注册）。",
            "2. 私聊机器人本人（不要在群里发密码，避免泄露）。",
            "3. 直接发送 /注册（我会自动生成密码），或发送 /注册 你自己的密码。",
            f"   自己设密码需至少 {min_length} 位，且同时含大写字母、小写字母、数字和标点（例：/注册 Abc12345!）。",
            "   注册后可私聊发送 /找回密码 新密码 修改。",
        ]
        if qq:
            lines.append(f"注册成功后，账号就是你的QQ号：{qq}")
        return "\n".join(lines)

    def _password_error(self, password: str) -> str | None:
        min_length = self._effective_min_length()
        if len(password) < min_length:
            return f"密码太短，至少需要 {min_length} 位。"
        checks = [
            (any(ch.islower() for ch in password), "小写字母"),
            (any(ch.isupper() for ch in password), "大写字母"),
            (any(ch.isdigit() for ch in password), "数字"),
            (any(ch in string.punctuation for ch in password), "标点符号"),
        ]
        missing = [name for ok, name in checks if not ok]
        if missing:
            return "密码必须同时包含小写字母、大写字母、数字和标点符号。"
        return None

    def _generate_password(self) -> str:
        """生成一个满足强密码规则、且避开易混字符的随机密码。"""
        length = max(self._effective_min_length(), 10)
        lowers = "abcdefghijkmnpqrstuvwxyz"  # 去掉 l o
        uppers = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # 去掉 I O
        digits = "23456789"  # 去掉 0 1
        puncts = "!@#$%*"
        pool = lowers + uppers + digits + puncts
        rng = secrets.SystemRandom()
        for _ in range(50):
            chars = [
                secrets.choice(lowers),
                secrets.choice(uppers),
                secrets.choice(digits),
                secrets.choice(puncts),
            ]
            chars += [secrets.choice(pool) for _ in range(length - 4)]
            rng.shuffle(chars)
            candidate = "".join(chars)
            if self._password_error(candidate) is None:
                return candidate
        return "Aa2!" + "".join(secrets.choice(pool) for _ in range(max(4, length - 4)))

    async def _periodic_group_check(self):
        await asyncio.sleep(15)
        while True:
            interval = max(1, self._get_int("periodic_check_minutes", 30)) * 60
            try:
                await self._run_group_check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"periodic group check failed: {exc}")
            await asyncio.sleep(interval)

    async def _run_group_check_once(self):
        bot = self._bot
        if not bot:
            if not self._logged_missing_bot:
                logger.info("periodic group check waiting for first aiocqhttp event")
                self._logged_missing_bot = True
            return
        if not self._get_bool("create_audit_table", True):
            logger.warning("periodic group check requires create_audit_table=true")
            return

        threshold = max(1, self._get_int("periodic_miss_threshold", 2))
        for group_id in self._managed_groups():
            try:
                members = await bot.get_group_member_list(
                    group_id=int(group_id), no_cache=True
                )
            except Exception as exc:
                logger.warning(f"get_group_member_list failed group={group_id}: {exc}")
                continue

            member_ids = {
                str(item.get("user_id"))
                for item in members
                if isinstance(item, dict) and item.get("user_id") is not None
            }
            registered_qqs = await asyncio.to_thread(self._registered_qqs, group_id)
            for qq in registered_qqs:
                key = f"{group_id}:{qq}"
                if await asyncio.to_thread(self._is_banned, qq):
                    self._miss_counts.pop(key, None)
                    continue
                if qq in member_ids:
                    self._miss_counts.pop(key, None)
                    continue

                misses = self._miss_counts.get(key, 0) + 1
                self._miss_counts[key] = misses
                logger.warning(
                    f"registered qq missing from group group={group_id} qq={qq} misses={misses}"
                )
                if misses >= threshold:
                    ok = await asyncio.to_thread(
                        self._ban_registered_user, qq, group_id, "periodic_absent"
                    )
                    if ok:
                        self._miss_counts.pop(key, None)
                        await self._send_admin_notice(
                            bot,
                            f"定时校验发现 QQ {qq} 不在群 {group_id}，已自动封号。",
                        )

    async def _notify_admin(self, event: AstrMessageEvent, message: str):
        group_id = self._get_str("admin_notice_group_id", "").strip()
        if not group_id:
            return
        bot = getattr(event, "bot", None)
        await self._send_admin_notice(bot, message)

    async def _send_admin_notice(self, bot: Any, message: str):
        group_id = self._get_str("admin_notice_group_id", "").strip()
        if not group_id:
            return
        if not bot:
            return
        try:
            await bot.send_group_msg(group_id=int(group_id), message=message)
        except Exception as exc:
            logger.warning(f"admin notice failed: {exc}")

    @contextmanager
    def _conn(self):
        db_cfg = dict(self.config.get("database") or {})
        conn = pymysql.connect(
            host=db_cfg.get("host") or "127.0.0.1",
            port=int(db_cfg.get("port") or 3306),
            user=db_cfg.get("user") or "root",
            password=db_cfg.get("password") or "",
            database=db_cfg.get("database") or "user",
            charset=db_cfg.get("charset") or "utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
        try:
            yield conn
        finally:
            conn.close()

    def _execute_query(self, sql: str, params: tuple = (), fetch_one=False):
        with self._conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone() if fetch_one else cursor.fetchall()

    def _execute_update(self, sql: str, params: tuple = ()) -> bool:
        with self._conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                conn.commit()
                return True

    def _table_columns(self, table: str) -> list[str]:
        rows = self._execute_query(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (table,),
        )
        return [row["COLUMN_NAME"] for row in rows]

    def _has_column(self, table: str, column: str) -> bool:
        return any(col.lower() == column.lower() for col in self._table_columns(table))

    def _get_ban_column(self) -> str:
        if self._has_column("user", "IsBan"):
            return "IsBan"
        if self._has_column("user", "bantype"):
            return "bantype"
        return "IsBan"

    def _get_user_by_name(self, qq: str) -> dict[str, Any] | None:
        return self._execute_query(
            "SELECT * FROM user WHERE Name = %s LIMIT 1", (qq,), fetch_one=True
        )

    def _get_user_ban_value(self, user: dict[str, Any]) -> int:
        for key in ("IsBan", "bantype"):
            if key in user:
                return int(user.get(key) or 0)
        return 0

    def _is_banned(self, qq: str) -> bool:
        user = self._get_user_by_name(qq)
        if not user:
            return True
        return bool(self._get_user_ban_value(user))

    def _registered_qqs(self, group_id: str) -> list[str]:
        rows = self._execute_query(
            """
            SELECT DISTINCT qq
            FROM astrbot_qq_registration_guard
            WHERE group_id = %s AND action = 'register'
            """,
            (int(group_id),),
        )
        return [str(row["qq"]) for row in rows]

    def _register_user(self, qq: str, password: str, group_id: str) -> dict[str, Any]:
        try:
            if self._get_user_by_name(qq):
                return {
                    "success": False,
                    "message": (
                        "该QQ已经注册过账号了，不需要重复注册。\n"
                        "· 忘记密码：私聊发送 /找回密码 新密码\n"
                        "· 查询状态：发送 /查注册"
                    ),
                }

            columns = self._table_columns("user")
            lookup = {col.lower(): col for col in columns}
            insert_columns = [lookup.get("name", "Name"), lookup.get("password", "Password")]
            values: list[Any] = [qq, password]
            optional_values = {
                "email": None,
                "ip": None,
                "registration_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "isban": 0,
                "bantype": 0,
                "uinidentity": 0,
                "point": 0,
                "points": 0,
                "用户积分": 0,
            }
            for key, value in optional_values.items():
                if key in lookup:
                    insert_columns.append(lookup[key])
                    values.append(value)

            quoted = ", ".join(f"`{col}`" for col in insert_columns)
            placeholders = ", ".join(["%s"] * len(insert_columns))
            with self._conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO user ({quoted}) VALUES ({placeholders})",
                        tuple(values),
                    )
                    conn.commit()

            return {"success": True, "message": "注册成功"}
        except Exception as exc:
            logger.exception(f"register user failed qq={qq} group={group_id}: {exc}")
            return {
                "success": False,
                "message": "服务器暂时无法完成注册（数据库异常），请稍后再试或联系管理员。",
            }

    def _reset_password(self, qq: str, password: str, group_id: str) -> dict[str, Any]:
        try:
            if not self._get_user_by_name(qq):
                return {"success": False, "message": "当前QQ尚未注册，无法找回密码。"}

            columns = self._table_columns("user")
            lookup = {col.lower(): col for col in columns}
            password_col = lookup.get("password", "Password")
            self._execute_update(
                f"UPDATE user SET `{password_col}` = %s WHERE Name = %s",
                (password, qq),
            )
            return {"success": True, "message": "密码已更新。"}
        except Exception as exc:
            logger.exception(f"reset password failed qq={qq} group={group_id}: {exc}")
            return {"success": False, "message": f"找回密码失败：{exc}"}

    def _ban_registered_user(self, qq: str, group_id: str, reason: str) -> bool:
        user = self._get_user_by_name(qq)
        if not user:
            self._record_audit(qq, group_id, "leave_no_account", reason)
            return False
        ban_col = self._get_ban_column()
        ban_value = self._get_int("ban_value", 1)
        self._execute_update(
            f"UPDATE user SET `{ban_col}` = %s WHERE Name = %s",
            (ban_value, qq),
        )
        self._record_audit(qq, group_id, "auto_ban", reason)
        logger.info(f"auto banned qq={qq} group={group_id} reason={reason}")
        return True

    def _latest_ban_or_unban_action(self, qq: str) -> str | None:
        """取该QQ最近一次封/解封动作，用于判断当前封禁是否由本插件所为。"""
        try:
            row = self._execute_query(
                """
                SELECT action FROM astrbot_qq_registration_guard
                WHERE qq = %s
                  AND action IN ('auto_ban', 'self_unban', 'auto_unban', 'admin_unban')
                ORDER BY id DESC LIMIT 1
                """,
                (int(qq),),
                fetch_one=True,
            )
            return row["action"] if row else None
        except Exception as exc:
            logger.warning(f"query latest ban/unban action failed qq={qq}: {exc}")
            return None

    def _can_self_unban(self, qq: str) -> bool:
        # 仅允许解除“本插件因退群自动封禁”的账号，避免绕过管理员的手动封禁
        if not self._get_bool("create_audit_table", True):
            return False
        return self._latest_ban_or_unban_action(qq) == "auto_ban"

    def _unban_user(self, qq: str, group_id: str, action: str) -> bool:
        user = self._get_user_by_name(qq)
        if not user:
            return False
        ban_col = self._get_ban_column()
        try:
            self._execute_update(
                f"UPDATE user SET `{ban_col}` = 0 WHERE Name = %s",
                (qq,),
            )
        except Exception as exc:
            logger.exception(f"unban user failed qq={qq} group={group_id}: {exc}")
            return False
        self._record_audit(qq, group_id, action, "account unbanned")
        logger.info(f"unbanned qq={qq} group={group_id} action={action}")
        return True

    def _ensure_audit_table(self):
        self._execute_update(
            """
            CREATE TABLE IF NOT EXISTS astrbot_qq_registration_guard (
                id INT AUTO_INCREMENT PRIMARY KEY,
                qq BIGINT NOT NULL,
                group_id BIGINT NULL,
                action VARCHAR(64) NOT NULL,
                detail VARCHAR(255) NULL,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_qq (qq),
                INDEX idx_group_id (group_id),
                INDEX idx_action (action)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    def _record_audit(self, qq: str, group_id: str, action: str, detail: str):
        if not self._get_bool("create_audit_table", True):
            return
        try:
            self._execute_update(
                """
                INSERT INTO astrbot_qq_registration_guard
                    (qq, group_id, action, detail, create_time)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    int(qq),
                    int(group_id) if str(group_id).isdigit() else None,
                    action,
                    detail[:255],
                    datetime.now(),
                ),
            )
        except Exception as exc:
            logger.warning(f"record audit failed: {exc}")

    def _managed_groups(self) -> set[str]:
        raw = self.config.get("managed_group_ids") or []
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",")]
        return {str(item).strip() for item in raw if str(item).strip()}

    def _get_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _get_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _get_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        return default if value is None else str(value)
