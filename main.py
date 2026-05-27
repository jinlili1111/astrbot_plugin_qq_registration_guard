import asyncio
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
        """私聊注册账号，账号默认使用QQ号。"""
        self._remember_bot(event)
        qq = event.get_sender_id()
        group_id = event.get_group_id()
        password = (password or "").strip()
        min_length = self._get_int("password_min_length", 6)

        if group_id:
            yield event.plain_result("密码属于隐私，请私聊机器人发送：/注册 密码")
            return
        if not password:
            yield event.plain_result("用法：/注册 密码")
            return
        if len(password) < min_length:
            yield event.plain_result(f"密码至少需要 {min_length} 位。")
            return

        member_group_id = await self._find_member_group(event, qq)
        if not member_group_id:
            yield event.plain_result("未检测到你在受管群内，不能注册。")
            return

        result = await asyncio.to_thread(
            self._register_user, qq, password, member_group_id
        )
        if result["success"]:
            await asyncio.to_thread(
                self._record_audit, qq, member_group_id, "register", "user registered"
            )
            yield event.plain_result(f"注册成功。账号：{qq}")
        else:
            yield event.plain_result(result["message"])

    @filter.command("找回密码")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def recover_password(self, event: AiocqhttpMessageEvent, password: str = ""):
        """私聊找回密码，只允许修改发送者QQ对应的账号。"""
        self._remember_bot(event)
        qq = event.get_sender_id()
        group_id = event.get_group_id()
        password = (password or "").strip()
        min_length = self._get_int("password_min_length", 6)

        if group_id:
            yield event.plain_result("密码属于隐私，请私聊机器人发送：/找回密码 新密码")
            return
        if not password:
            yield event.plain_result("用法：/找回密码 新密码")
            return
        if len(password) < min_length:
            yield event.plain_result(f"密码至少需要 {min_length} 位。")
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
            yield event.plain_result("当前QQ尚未注册。")
            return
        ban_value = self._get_user_ban_value(user)
        status = "已封禁" if ban_value else "正常"
        yield event.plain_result(f"账号：{qq}\n状态：{status}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_all_events(self, event: AstrMessageEvent):
        """监听 OneBot notice 事件，处理退群自动封号。"""
        self._remember_bot(event)
        if not self._get_bool("auto_ban_on_leave", True):
            return
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
        if post_type != "notice" or notice_type != "group_decrease":
            return
        if not group_id or not user_id or group_id not in self._managed_groups():
            return

        ok = await asyncio.to_thread(
            self._ban_registered_user, user_id, group_id, "leave_notice"
        )
        if ok:
            await self._notify_admin(event, f"检测到 QQ {user_id} 退出群 {group_id}，已自动封号。")

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
                return {"success": False, "message": "当前QQ已注册账号。"}

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
            return {"success": False, "message": f"注册失败：{exc}"}

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
