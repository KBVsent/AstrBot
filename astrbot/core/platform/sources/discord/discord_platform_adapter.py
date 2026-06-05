import asyncio
import inspect
import json
import re
import sys
from typing import Any, cast

import discord
from discord.abc import GuildChannel, Messageable, PrivateChannel
from discord.channel import DMChannel

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, File, Image, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import StarHandlerMetadata, star_handlers_registry

from .client import DiscordBotClient
from .discord_platform_event import DiscordPlatformEvent

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from discord.commands.core import valid_locales as _DISCORD_VALID_LOCALES

_DISCORD_VALID_LOCALE_SET = frozenset(_DISCORD_VALID_LOCALES)


# 注册平台适配器
@register_platform_adapter(
    "discord", "Discord 适配器 (基于 Pycord)", support_streaming_message=False
)
class DiscordPlatformAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.bot_self_id: str | None = None
        self.registered_handlers = []
        # 指令注册相关
        self.enable_command_register = self.config.get("discord_command_register", True)
        # 消息模式：mention_and_dm（默认，零特权 intent）/ full_message（旧行为，需特权）。
        # 旧实例存盘里没有该 key，必须写显式 fallback（DEFAULT_CONFIG.platform=[] 不会回填实例字段）。
        self.message_mode = self.config.get("discord_message_mode", "mention_and_dm")
        if self.message_mode not in ("mention_and_dm", "full_message"):
            self.message_mode = "mention_and_dm"
        # 空字符串（schema 默认）按"无调试服务器"处理，走全局注册
        self.guild_id = self.config.get("discord_guild_id_for_debug") or None
        self.activity_name = self.config.get("discord_activity_name", None)
        self.shutdown_event = asyncio.Event()
        self._polling_task = None

    @override
    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        """通过会话发送消息"""
        if self.client.user is None:
            logger.error(
                "[Discord] Client is not ready (self.client.user is None); message send skipped"
            )
            return

        # 创建一个 message_obj 以便在 event 中使用
        message_obj = AstrBotMessage()
        if "_" in session.session_id:
            session.session_id = session.session_id.split("_")[1]
        channel_id_str = session.session_id
        channel = None
        try:
            channel_id = int(channel_id_str)
            channel = self.client.get_channel(channel_id)
        except (ValueError, TypeError):
            logger.warning(f"[Discord] Invalid channel ID format: {channel_id_str}")

        if channel:
            message_obj.type = self._get_message_type(channel)
            message_obj.group_id = self._get_channel_id(channel)
        else:
            logger.error(
                f"[Discord] Proactive send failed: cannot resolve channel {channel_id_str} "
                "(bot may not be in the guild, or the channel does not exist)."
            )
            return

        message_obj.message_str = message_chain.get_plain_text()
        message_obj.sender = MessageMember(
            user_id=str(self.bot_self_id),
            nickname=self.client.user.display_name,
        )
        message_obj.self_id = cast(str, self.bot_self_id)
        message_obj.session_id = session.session_id
        message_obj.message = message_chain.chain

        # 创建临时事件对象来发送消息
        temp_event = DiscordPlatformEvent(
            message_str=message_chain.get_plain_text(),
            message_obj=message_obj,
            platform_meta=self.meta(),
            session_id=session.session_id,
            client=self.client,
        )
        await temp_event.send(message_chain)
        await super().send_by_session(session, message_chain)

    @override
    def meta(self) -> PlatformMetadata:
        """返回平台元数据"""
        return PlatformMetadata(
            "discord",
            "Discord Adapter",
            id=cast(str, self.config.get("id")),
            default_config_tmpl=self.config,
            support_streaming_message=False,
        )

    @override
    async def run(self) -> None:
        """主要运行逻辑"""

        # 初始化回调函数
        async def on_received(message_data) -> None:
            logger.debug(f"[Discord] Message received: {message_data}")
            if self.bot_self_id is None:
                self.bot_self_id = message_data.get("bot_id")
            abm = await self.convert_message(data=message_data)
            await self.handle_msg(abm)

        async def on_interaction_received(interaction_data) -> None:
            # 组件交互（按钮 / select / modal 提交）入站：转 ABM 后进 pipeline。
            logger.debug(f"[Discord] Interaction received: {interaction_data}")
            if self.bot_self_id is None:
                self.bot_self_id = interaction_data.get("bot_id")
            abm = await self.convert_message(data=interaction_data)
            await self.handle_msg(abm)

        # 初始化 Discord 客户端
        token = str(self.config.get("discord_token"))
        if not token:
            logger.error(
                "[Discord] Bot token is not configured. Please set a valid token in the config file."
            )
            return

        proxy = self.config.get("discord_proxy") or None
        allow_bot_messages = bool(self.config.get("discord_allow_bot_messages"))
        self.client = DiscordBotClient(
            token, proxy, allow_bot_messages, self.message_mode
        )
        self.client.on_message_received = on_received
        self.client.on_interaction_received = on_interaction_received

        async def callback() -> None:
            try:
                if self.enable_command_register:
                    await self._collect_and_register_commands()
                if self.activity_name:
                    await self.client.change_presence(
                        status=discord.Status.online,
                        activity=discord.CustomActivity(name=self.activity_name),
                    )
            except Exception as e:
                logger.error(
                    f"[Discord] on_ready_once_callback err: {e}", exc_info=True
                )

        self.client.on_ready_once_callback = callback

        def _on_polling_done(task: asyncio.Task) -> None:
            # start_polling 跑在独立 task 里，其异常（如特权 intent 未授权抛出的
            # PrivilegedIntentsRequired、LoginFailure）不会冒泡到下面的 try/except，
            # 不在此显式打日志就会被静默吞掉、适配器一直挂在 shutdown_event.wait()。
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error(
                    f"[Discord] Polling task exited unexpectedly: {exc}", exc_info=exc
                )
                self.shutdown_event.set()

        try:
            self._polling_task = asyncio.create_task(self.client.start_polling())
            self._polling_task.add_done_callback(_on_polling_done)
            await self.shutdown_event.wait()
        except discord.errors.LoginFailure:
            logger.error(
                "[Discord] Login failed. Please check whether the bot token is correct."
            )
        except discord.errors.ConnectionClosed:
            logger.warning("[Discord] Connection with Discord has been closed.")
        except Exception as e:
            logger.error(
                f"[Discord] Unexpected error while adapter is running: {e}",
                exc_info=True,
            )

    def _get_message_type(
        self,
        channel: Messageable | GuildChannel | PrivateChannel,
        guild_id: int | None = None,
    ) -> MessageType:
        """根据 channel 对象和 guild_id 判断消息类型"""
        if guild_id is not None:
            return MessageType.GROUP_MESSAGE
        if isinstance(channel, DMChannel) or getattr(channel, "guild", None) is None:
            return MessageType.FRIEND_MESSAGE
        return MessageType.GROUP_MESSAGE

    def _get_channel_id(
        self, channel: Messageable | GuildChannel | PrivateChannel
    ) -> str:
        """根据 channel 对象获取ID"""
        return str(getattr(channel, "id", None))

    def _convert_message_to_abm(self, data: dict) -> AstrBotMessage:
        """将普通消息转换为 AstrBotMessage"""
        message = data["message"]

        content = message.content
        bot_id = self.client.user.id if self.client and self.client.user else None

        # 解析正文里的用户 @：按出现顺序、不去重转成 Comp.At（与 slash USER 选项语义一致），
        # 让插件能从消息链拿到 @ 目标。bot 自己的 @（mention_and_dm 模式的唤醒触发）
        # 只从文本剥除、不建 At——否则会把 bot 误当成目标。
        at_components: list[At] = []
        for match in re.finditer(r"<@!?(\d+)>", content):
            mid = match.group(1)
            if bot_id is not None and int(mid) == bot_id:
                continue
            at_components.append(At(qq=mid))

        # 剥离全部用户 mention 标记（含 bot 与他人），避免雪花 ID 污染位置参数
        content = re.sub(r"<@!?\d+>", "", content)

        # 剥离 Role Mention（bot 拥有的任一角色被提及，<@&role_id>）：角色@仅用于唤醒，不转 At，
        # 仅从文本清理 bot 角色 mention（角色@唤醒，full_message 模式）。
        if (
            hasattr(message, "role_mentions")
            and hasattr(message, "guild")
            and message.guild
            and self.client
            and self.client.user
        ):
            bot_member = message.guild.get_member(self.client.user.id)
            if bot_member and hasattr(bot_member, "roles"):
                for role in bot_member.roles:
                    content = content.replace(f"<@&{role.id}>", "")

        # 收敛多余空格
        content = re.sub(r"\s{2,}", " ", content).strip()

        abm = AstrBotMessage()
        abm.type = self._get_message_type(message.channel)
        abm.group_id = self._get_channel_id(message.channel)
        abm.message_str = content
        abm.sender = MessageMember(
            user_id=str(message.author.id),
            nickname=message.author.display_name,
        )
        message_chain = []
        if abm.message_str:
            message_chain.append(Plain(text=abm.message_str))
        message_chain.extend(at_components)
        if message.attachments:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith(
                    "image/",
                ):
                    message_chain.append(
                        Image(file=attachment.url, filename=attachment.filename),
                    )
                else:
                    message_chain.append(
                        File(name=attachment.filename, url=attachment.url),
                    )
        abm.message = message_chain
        abm.raw_message = message
        abm.self_id = cast(str, self.bot_self_id)
        abm.session_id = str(message.channel.id)
        abm.message_id = str(message.id)
        return abm

    def _convert_interaction_to_abm(self, data: dict) -> AstrBotMessage:
        """将组件交互（按钮 / select / modal 提交）转换为 AstrBotMessage。

        交互不靠文本路由，靠 custom_id；message_str 留空，由处理器
        自行从 custom_id 改写。raw_message 设为 discord.Interaction，使事件的
        is_button_interaction()/get_interaction_custom_id() 等生效。
        """
        interaction: discord.Interaction = data["interaction"]
        channel = interaction.channel

        abm = AstrBotMessage()
        if channel is not None:
            abm.type = self._get_message_type(channel, interaction.guild_id)
            abm.group_id = self._get_channel_id(channel)
        else:
            abm.type = (
                MessageType.GROUP_MESSAGE
                if interaction.guild_id is not None
                else MessageType.FRIEND_MESSAGE
            )
            abm.group_id = str(interaction.channel_id)

        # 交互不靠文本路由（处理器读 custom_id），message_str/message 均留空——避免触发命令匹配
        abm.message_str = ""
        abm.message = []
        abm.sender = MessageMember(
            user_id=str(interaction.user.id) if interaction.user else "",
            nickname=interaction.user.display_name if interaction.user else "",
        )
        abm.raw_message = interaction
        abm.self_id = cast(str, self.bot_self_id)
        abm.session_id = str(interaction.channel_id)
        abm.message_id = str(interaction.id)
        return abm

    async def convert_message(self, data: dict) -> AstrBotMessage:
        """将平台消息转换成 AstrBotMessage（普通消息 / 组件交互两类）"""
        if data.get("type") == "interaction":
            return self._convert_interaction_to_abm(data)
        return self._convert_message_to_abm(data)

    async def handle_msg(
        self,
        message: AstrBotMessage,
        followup_webhook=None,
        user_locale: str | None = None,
        ephemeral: bool = False,
    ) -> None:
        """处理消息"""
        message_event = DiscordPlatformEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            client=self.client,
            interaction_followup_webhook=followup_webhook,
            is_ephemeral=ephemeral,
        )

        # slash interaction 自带用户客户端 locale，放进事件 extras 供业务侧做语言判定/seed。
        # on_message（@bot/DM）路径无此信息，user_locale 为 None 不写。
        if user_locale:
            message_event.set_extra("user_locale", user_locale)

        if self.client.user is None:
            logger.error(
                "[Discord] Client is not ready (self.client.user is None); message handling skipped"
            )
            return

        # 检查是否为斜杠指令
        is_slash_command = message_event.interaction_followup_webhook is not None

        # 1. 优先处理斜杠指令
        if is_slash_command:
            message_event.is_wake = True
            message_event.is_at_or_wake_command = True
            self.commit_event(message_event)
            return

        # 2. 组件交互（按钮 / select / modal 提交）：raw_message 是 discord.Interaction。
        if isinstance(message.raw_message, discord.Interaction):
            message_event.is_wake = True
            self.commit_event(message_event)
            return

        # 3. 处理普通消息（提及检测）
        # 确保 raw_message 是 discord.Message 类型，以便静态检查通过
        raw_message = message.raw_message
        if not isinstance(raw_message, discord.Message):
            logger.warning(
                f"[Discord] Non-Message type received and ignored: {type(raw_message)}"
            )
            return

        # 检查是否被@（User Mention 或 Bot 拥有的 Role Mention）
        is_mention = False

        # User Mention
        # 此时 Pylance 知道 raw_message 是 discord.Message，具有 mentions 属性
        if self.client.user in raw_message.mentions:
            is_mention = True

        # Role Mention（Bot 拥有的角色被提及）：依赖 members intent 解析 bot 角色，
        # 仅 full_message 模式可用；其余模式无 members intent，跳过角色@唤醒。
        if (
            self.message_mode == "full_message"
            and not is_mention
            and raw_message.role_mentions
        ):
            bot_member = None
            if raw_message.guild:
                try:
                    bot_member = raw_message.guild.get_member(
                        self.client.user.id,
                    )
                except Exception:
                    bot_member = None
            if bot_member and hasattr(bot_member, "roles"):
                bot_roles = set(bot_member.roles)
                mentioned_roles = set(raw_message.role_mentions)
                if (
                    bot_roles
                    and mentioned_roles
                    and bot_roles.intersection(mentioned_roles)
                ):
                    is_mention = True

        # 如果是被@的消息，设置为唤醒状态
        if is_mention:
            message_event.is_wake = True
            message_event.is_at_or_wake_command = True

        self.commit_event(message_event)

    @override
    async def terminate(self) -> None:
        logger.info("[Discord] Shutting down adapter...")
        self.shutdown_event.set()
        logger.info("[Discord] Cleaning up commands...")
        if self.enable_command_register and self.client:
            try:
                await asyncio.wait_for(
                    self.client.sync_commands(
                        commands=[],
                        guild_ids=[self.guild_id] if self.guild_id else None,
                    ),
                    timeout=10,
                )
                logger.info("[Discord] Commands cleaned up successfully.")
            except Exception as e:
                logger.warning(
                    f"[Discord] Error occurred while cleaning up commands: {e}"
                )

        if self._polling_task:
            self._polling_task.cancel()
            try:
                await asyncio.wait_for(self._polling_task, timeout=10)
            except asyncio.CancelledError:
                logger.info("[Discord] Polling task cancelled successfully.")
            except Exception as e:
                logger.warning(
                    f"[Discord] Error occurred while cancelling polling task: {e}"
                )
        logger.info("[Discord] Closing client connection...")
        if self.client and hasattr(self.client, "close"):
            try:
                await asyncio.wait_for(self.client.close(), timeout=10)
            except Exception as e:
                logger.warning(f"[Discord] Error occurred while closing client: {e}")
        logger.info("[Discord] Adapter shutdown complete.")

    def register_handler(self, handler_info) -> None:
        """注册处理器信息"""
        self.registered_handlers.append(handler_info)

    async def _collect_and_register_commands(self) -> None:
        """按指令注册表（discord_command_schemas）注册斜杠指令。

        注册表为空时自动发现全部已激活指令并持久化（仅一次），之后保留用户编辑。
        """
        schemas = self._load_or_seed_command_schemas()
        if schemas is None:
            # JSON 解析失败，已记录日志；中止注册，避免破坏用户数据。
            return
        if not schemas:
            logger.info(
                "[Discord] Command schema table is empty; no slash commands registered."
            )
            return

        logger.info("[Discord] Registering slash commands from schema table...")
        registered_commands: list[str] = []
        used_slash_names: set[str] = set()

        for cmd_name, entry in schemas.items():
            if not isinstance(entry, dict):
                logger.warning(
                    f"[Discord] Skipping schema entry '{cmd_name}': not a JSON object."
                )
                continue
            if not entry.get("enabled", True):
                continue

            slash_name = str(entry.get("slash_name") or cmd_name).strip()
            if not re.match(r"^[a-z0-9_-]{1,32}$", slash_name):
                logger.warning(
                    f"[Discord] Skipping '{cmd_name}': invalid slash name '{slash_name}' "
                    "(must match ^[a-z0-9_-]{1,32}$)."
                )
                continue
            if slash_name in used_slash_names:
                logger.warning(
                    f"[Discord] Skipping '{cmd_name}': duplicate slash name '{slash_name}'."
                )
                continue

            description = (str(entry.get("description") or f"Command: {cmd_name}"))[
                :100
            ]
            options = self._build_options(entry.get("options"))
            # ephemeral 私密响应：per-trigger 静态位，defer 时刻锁定（事件还没进 pipeline）。
            ephemeral = bool(entry.get("ephemeral", False))

            # 回调用底层指令名（注册表的键）构造 message_str，
            # 保证自定义 slash_name 也能路由回原指令。
            callback = self._create_dynamic_callback(cmd_name, len(options), ephemeral)

            slash_kwargs: dict[str, Any] = {
                "name": slash_name,
                "description": description,
                "func": callback,
                "options": options,
                "guild_ids": [self.guild_id] if self.guild_id else None,
            }
            desc_localizations = self._filter_localizations(
                entry.get("description_localizations")
            )
            if desc_localizations:
                slash_kwargs["description_localizations"] = desc_localizations

            self.client.add_application_command(discord.SlashCommand(**slash_kwargs))
            used_slash_names.add(slash_name)
            registered_commands.append(slash_name)

        if registered_commands:
            logger.info(
                f"[Discord] Ready to sync {len(registered_commands)} commands: "
                f"{', '.join(registered_commands)}",
            )
            if len(registered_commands) > 100 and not self.guild_id:
                logger.warning(
                    "[Discord] More than 100 global slash commands configured; Discord "
                    "caps global commands at 100. Disable unneeded ones via "
                    "'enabled': false in discord_command_schemas, or set a debug guild ID.",
                )
        else:
            logger.info("[Discord] No commands found for registration.")

        # 使用 Pycord 的方法同步指令
        # 注意：这可能需要一些时间，并且有频率限制
        try:
            await self.client.sync_commands()
            logger.info("[Discord] Command synchronization completed.")
        except discord.HTTPException as e:
            if self._is_daily_command_quota_error(e):
                logger.warning(
                    "[Discord] Daily application command create quota reached "
                    "(30034); command sync skipped. Existing commands should "
                    "continue to work until the quota resets.",
                )
                return
            logger.warning(f"[Discord] Sync commands failed: {e}")

    @staticmethod
    def _is_daily_command_quota_error(error: discord.HTTPException) -> bool:
        return getattr(error, "code", None) == 30034

    def _load_or_seed_command_schemas(self) -> dict | None:
        """读取指令注册表；为空则自动发现全部指令、播种并持久化。

        返回注册表 dict；JSON 非法或结构错误时返回 None（中止注册，不动用户数据）。
        """
        raw = self.config.get("discord_command_schemas") or ""
        if not raw.strip():
            seeded = self._discover_default_schemas()
            self._persist_seeded_schemas(seeded)
            return seeded

        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            logger.error(
                f"[Discord] Failed to parse discord_command_schemas JSON: {e}. "
                "Registration aborted; your config is left untouched.",
            )
            return None
        if not isinstance(data, dict):
            logger.error(
                "[Discord] discord_command_schemas must be a JSON object keyed by "
                "command name. Registration aborted.",
            )
            return None
        return data

    @staticmethod
    def _discover_default_schemas() -> dict:
        """发现全部已激活插件指令，生成默认注册表（每条=启用、slash 名=指令名、单 string 参数）。

        无实例状态依赖（只走全局 registry），声明为 staticmethod 便于 WebUI 路由在没有
        运行中适配器实例时也能调用（如平台被禁用）。
        """
        schemas: dict[str, dict] = {}
        for handler_md in star_handlers_registry:
            if not star_map[handler_md.handler_module_path].activated:
                continue
            if not handler_md.enabled:
                continue
            for event_filter in handler_md.event_filters:
                cmd_info = DiscordPlatformAdapter._extract_command_info(
                    event_filter, handler_md
                )
                if not cmd_info:
                    continue
                cmd_name, description, _ = cmd_info
                if cmd_name in schemas:
                    continue  # 同名指令多个 handler，首个为准
                schemas[cmd_name] = {
                    "enabled": True,
                    "slash_name": cmd_name,
                    "description": description,
                    "description_localizations": {},
                    "options": [
                        {
                            "name": "params",
                            "description": "指令的所有参数",
                            "type": "string",
                            "description_localizations": {},
                            "required": False,
                        },
                    ],
                    "ephemeral": False,
                }
        return schemas

    def _persist_seeded_schemas(self, schemas: dict) -> None:
        """把播种出的注册表写回配置并持久化到 data/cmd_config.json。"""
        self.config["discord_command_schemas"] = json.dumps(
            schemas, ensure_ascii=False, indent=2
        )
        astrbot_config = self._astrbot_config
        save_config = getattr(astrbot_config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
                logger.info(
                    f"[Discord] Seeded {len(schemas)} commands into "
                    "discord_command_schemas and saved config. Edit it in the WebUI to "
                    "customize slash names / descriptions / localizations.",
                )
            except Exception as e:
                logger.warning(
                    f"[Discord] Seeded commands but failed to persist config: {e}. "
                    "Edits this run are in-memory only.",
                )
        else:
            logger.warning(
                "[Discord] Seeded commands in-memory only (config object unavailable); "
                "they won't persist across restarts.",
            )

    def _build_options(self, raw_options: Any) -> list[discord.Option]:
        """从注册表条目的 options 构造 Discord 选项；缺省时给单个通用 params 选项。

        作为框架能力，支持 Discord 全部参数类型（见 ``type_map``）。触发时回调按值的实际类型
        通用地映射进 AstrBot 消息模型（见 ``_create_dynamic_callback``）：标量入 message_str
        文本、user→``Comp.At``、attachment→``Image``/``File``、channel/role→Discord mention 文本。
        未知 type 告警回退 string。
        """
        # type 字段 → Discord 选项类型（覆盖全部可作参数的类型，scope 约定局部定义）
        type_map = {
            "string": discord.SlashCommandOptionType.string,
            "integer": discord.SlashCommandOptionType.integer,
            "number": discord.SlashCommandOptionType.number,
            "boolean": discord.SlashCommandOptionType.boolean,
            "user": discord.SlashCommandOptionType.user,
            "channel": discord.SlashCommandOptionType.channel,
            "role": discord.SlashCommandOptionType.role,
            "mentionable": discord.SlashCommandOptionType.mentionable,
            "attachment": discord.SlashCommandOptionType.attachment,
        }
        specs = (
            raw_options
            if isinstance(raw_options, list) and raw_options
            else [
                {"name": "params", "description": "指令的所有参数", "required": False}
            ]
        )
        options: list[discord.Option] = []
        names: set[str] = set()
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name") or "").strip()
            if not re.match(r"^[a-z0-9_-]{1,32}$", name):
                logger.warning(
                    f"[Discord] Skipping option with invalid name: {spec.get('name')!r}"
                )
                continue
            if name in names:
                continue
            opt_type = str(spec.get("type") or "string").strip().lower()
            if opt_type not in type_map:
                logger.warning(
                    f"[Discord] Option '{name}': unknown type {opt_type!r}, "
                    "falling back to 'string'."
                )
                opt_type = "string"
            opt_kwargs: dict[str, Any] = {
                "name": name,
                "description": (str(spec.get("description") or name))[:100],
                "required": bool(spec.get("required", False)),
            }
            loc = self._filter_localizations(spec.get("description_localizations"))
            if loc:
                opt_kwargs["description_localizations"] = loc
            # input_type 是 discord.Option 的仅位置参数（签名 `/` 之前），必须位置传入；
            # 用 type=/input_type= 关键字会被丢进 **kwargs 忽略、退回默认 string。
            options.append(discord.Option(type_map[opt_type], **opt_kwargs))
            names.add(name)
        return options

    @staticmethod
    def _filter_localizations(raw: Any) -> dict[str, str]:
        """过滤本地化字典到合法 Discord locale 码，非法键告警并丢弃。"""
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in raw.items():
            if key not in _DISCORD_VALID_LOCALE_SET:
                logger.warning(
                    f"[Discord] Ignoring unknown Discord locale code in localizations: {key!r}"
                )
                continue
            if isinstance(value, str) and value.strip():
                out[key] = value[:100]
        return out

    def _create_dynamic_callback(
        self, command_name: str, option_count: int, ephemeral: bool = False
    ):
        """为每个指令动态创建一个异步回调函数。

        command_name 为底层 AstrBot 指令名（注册表键），用于构造 message_str
        之后交给CommandFilter，即使 Discord 上的 slash 名被自定义，也能路由回原指令。

        ephemeral 为 per-trigger 私密响应位（注册表静态声明）：True 时本次触发的 defer 与
        全部 followup 都仅触发者可见。Discord 在 defer 时刻锁定该状态，故必须随注册一起静态传入。

        Pycord 用 options kwarg 注册时，会把每个 Option 按位置匹配到回调的具名参数并以该名回传值
        没有具名参数，多 Option 会触发 "Too many arguments passed to the options kwarg"。
        因此给回调挂一个合成 __signature__：ctx + arg0..argN-1，让 Pycord 把 N 个 Option
        分别绑定到 arg0..；运行时仍由 **kwargs 接收，再按 arg{i} 顺序取值。
        """
        param_names = [f"arg{i}" for i in range(option_count)]

        async def dynamic_callback(
            ctx: discord.ApplicationContext, **kwargs: Any
        ) -> None:
            # 1. 尝试立即响应，防止超时（移到最前面）
            followup_webhook = None
            try:
                # 设定 2.5 秒超时，避免卡死整个 event loop。
                # ephemeral 在此刻锁定：True → 本次 defer + 全部 followup 仅触发者可见。
                await asyncio.wait_for(ctx.defer(ephemeral=ephemeral), timeout=2.5)
                followup_webhook = ctx.followup
            except asyncio.TimeoutError:
                logger.warning(
                    f"[Discord] Defer command '{command_name}' timeout. Network might be too slow."
                )
                return
            except Exception as e:
                logger.warning(
                    f"[Discord] Failed to defer command '{command_name}': {e}"
                )
                return

            # 按声明顺序处理各参数值，通用映射进 AstrBot 消息模型（不绑定具体插件用法）：
            #   - User/Member  → Comp.At（@ 提及，与 on_message 路径语义一致）
            #   - Attachment   → Image（图片）/ File（其他）
            #   - Role/Channel → Discord mention 文本（<@&id> / <#id>），信息保留进 message_str
            #   - 标量(str/int/float/bool) → 文本进 message_str；CommandFilter 再按 handler 签名转型
            # 非标量进消息链 component，标量进位置文本串。
            parts = [command_name]
            extra_components: list[Any] = []
            for pname in param_names:
                value = kwargs.get(pname)
                if value is None:
                    continue
                if isinstance(value, (discord.User, discord.Member)):
                    extra_components.append(
                        At(qq=str(value.id), name=value.display_name)
                    )
                elif isinstance(value, discord.Attachment):
                    if (value.content_type or "").startswith("image/"):
                        extra_components.append(
                            Image(file=value.url, filename=value.filename)
                        )
                    else:
                        extra_components.append(
                            File(name=value.filename, url=value.url)
                        )
                elif isinstance(value, discord.Role):
                    parts.append(f"<@&{value.id}>")
                elif isinstance(value, (GuildChannel, discord.Thread, PrivateChannel)):
                    parts.append(f"<#{value.id}>")
                elif isinstance(value, discord.Object):
                    parts.append(str(value.id))
                else:
                    # 标量：保留 0 / False，仅跳过空字符串
                    text = str(value)
                    if text != "":
                        parts.append(text)
            message_str_for_filter = " ".join(parts)

            logger.debug(
                f"[Discord] Slash command '{command_name}' triggered. "
                f"Options: {kwargs}. Built command string: '{message_str_for_filter}'"
                f"{f', +{len(extra_components)} component(s)' if extra_components else ''}",
            )

            # 2. 构建 AstrBotMessage
            channel = ctx.channel
            abm = AstrBotMessage()
            if channel is not None:
                abm.type = self._get_message_type(channel, ctx.guild_id)
                abm.group_id = self._get_channel_id(channel)
            else:
                # 防守式兜底：channel 取不到时，仍能根据 guild_id/channel_id 推断会话信息
                abm.type = (
                    MessageType.GROUP_MESSAGE
                    if ctx.guild_id is not None
                    else MessageType.FRIEND_MESSAGE
                )
                abm.group_id = str(ctx.channel_id)

            abm.message_str = message_str_for_filter
            abm.sender = MessageMember(
                user_id=str(ctx.author.id),
                nickname=ctx.author.display_name,
            )
            abm.message = [Plain(text=message_str_for_filter), *extra_components]
            abm.raw_message = ctx.interaction
            abm.self_id = cast(str, self.bot_self_id)
            abm.session_id = str(ctx.channel_id)
            abm.message_id = str(ctx.interaction.id)

            # 3. 将消息、webhook、用户 locale 交给 handle_msg 处理。
            # slash interaction 自带 ctx.locale，写入事件 extras；on_message 路径无此信息。
            user_locale = str(ctx.locale) if ctx.locale else None
            await self.handle_msg(
                abm, followup_webhook, user_locale=user_locale, ephemeral=ephemeral
            )

        # 合成签名：ctx + arg0..argN-1，供 Pycord 把 Option 绑定到具名参数。
        # 实际仍由上面的 **kwargs 接收，运行时按 arg{i} 顺序取值。
        sig_params = [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        for pname in param_names:
            sig_params.append(
                inspect.Parameter(
                    pname,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=None,
                )
            )
        dynamic_callback.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]

        return dynamic_callback

    @staticmethod
    def _extract_command_info(
        event_filter: Any,
        handler_metadata: StarHandlerMetadata,
    ) -> tuple[str, str, CommandFilter | None] | None:
        """从事件过滤器中提取指令信息"""
        cmd_name = None
        # is_group = False
        cmd_filter_instance = None

        if isinstance(event_filter, CommandFilter):
            # 暂不支持子指令注册为斜杠指令
            if (
                event_filter.parent_command_names
                and event_filter.parent_command_names != [""]
            ):
                return None
            cmd_name = event_filter.command_name
            cmd_filter_instance = event_filter

        elif isinstance(event_filter, CommandGroupFilter):
            # 暂不支持指令组直接注册为斜杠指令，因为它们没有 handle 方法
            return None

        if not cmd_name:
            return None

        # Discord 斜杠指令名称规范
        if not re.match(r"^[a-z0-9_-]{1,32}$", cmd_name):
            logger.debug(f"[Discord] Skipping invalid slash command format: {cmd_name}")
            return None

        description = handler_metadata.desc or f"Command: {cmd_name}"
        if len(description) > 100:
            description = f"{description[:97]}..."

        return cmd_name, description, cmd_filter_instance
