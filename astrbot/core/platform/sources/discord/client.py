import sys
from collections.abc import Awaitable, Callable

import discord

from astrbot import logger

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


# Discord Bot客户端
class DiscordBotClient(discord.Bot):
    """Discord客户端封装"""

    def __init__(
        self,
        token: str,
        proxy: str | None = None,
        allow_bot_messages: bool = False,
        message_mode: str = "mention_and_dm",
    ) -> None:
        self.token = token
        self.proxy = proxy
        self.allow_bot_messages = allow_bot_messages
        self.message_mode = message_mode

        # 设置Intent权限，遵循权限最小化原则。
        # default() 本身已关闭 message_content / members 两个特权 intent，
        # 仅 full_message 模式才需要订阅频道全部消息正文与成员事件。
        intents = discord.Intents.default()
        if message_mode == "full_message":
            intents.message_content = True  # 订阅消息内容事件 (Privileged)
            intents.members = True  # 订阅成员事件 (Privileged)

        # 初始化Bot
        # 指令同步完全由适配器的 _sync_commands_by_mode 接管，故必须关掉这个自动同步。
        super().__init__(intents=intents, proxy=proxy, auto_sync_commands=False)

        # 回调函数
        self.on_message_received: Callable[[dict], Awaitable[None]] | None = None
        # 组件交互（按钮 / select / modal 提交）回调；slash 仍走 Pycord 原生通道。
        self.on_interaction_received: Callable[[dict], Awaitable[None]] | None = None
        self.on_ready_once_callback: Callable[[], Awaitable[None]] | None = None
        self._ready_once_fired = False

    async def on_ready(self) -> None:
        """当机器人成功连接并准备就绪时触发"""
        if self.user is None:
            logger.error("[Discord] Bot user not loaded correctly (self.user is None)")
            return

        logger.info(f"[Discord] Logged in as {self.user} (ID: {self.user.id})")
        logger.info("[Discord] Client is ready.")

        if self.on_ready_once_callback and not self._ready_once_fired:
            self._ready_once_fired = True
            try:
                await self.on_ready_once_callback()
            except Exception as e:
                logger.error(
                    f"[Discord] Failed to execute on_ready_once_callback: {e}",
                    exc_info=True,
                )

    def _create_message_data(self, message: discord.Message) -> dict:
        """从 discord.Message 创建数据字典"""
        if self.user is None:
            raise RuntimeError("Bot is not ready: self.user is None")

        is_mentioned = self.user in message.mentions
        return {
            "message": message,
            "bot_id": str(self.user.id),
            "content": message.content,
            "username": message.author.display_name,
            "userid": str(message.author.id),
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "guild_id": str(message.guild.id) if message.guild else None,
            "type": "message",
            "is_mentioned": is_mentioned,
            "clean_content": message.clean_content,
        }

    def _create_interaction_data(self, interaction: discord.Interaction) -> dict:
        """从 discord.Interaction 创建数据字典"""
        if self.user is None:
            raise RuntimeError("Bot is not ready: self.user is None")

        if interaction.user is None:
            raise ValueError("Interaction received without a valid user")

        raw_data = getattr(interaction, "data", {}) or {}
        return {
            "interaction": interaction,
            "bot_id": str(self.user.id),
            "content": self._extract_interaction_content(interaction),
            "username": interaction.user.display_name,
            "userid": str(interaction.user.id),
            "message_id": str(interaction.id),
            "channel_id": str(interaction.channel_id)
            if interaction.channel_id
            else None,
            "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
            "type": "interaction",
            # custom_id：按钮 / select / modal 提交统一靠它路由
            "custom_id": raw_data.get("custom_id", ""),
            # select 菜单选中值
            "values": raw_data.get("values", []),
            # modal 提交：把各 InputText 扁平成 {input_custom_id: value}
            "modal_values": self._extract_modal_values(interaction),
        }

    @staticmethod
    def _extract_modal_values(interaction: discord.Interaction) -> dict[str, str]:
        """从 modal_submit 交互里提取各输入框的值，扁平成 {custom_id: value}。

        Discord modal 的 data.components 是一组 action row，每个 row 内含一个 InputText 组件。
        非 modal 交互返回空 dict。
        """
        if interaction.type != discord.InteractionType.modal_submit:
            return {}
        out: dict[str, str] = {}
        raw_data = getattr(interaction, "data", {}) or {}
        for row in raw_data.get("components", []) or []:
            for comp in row.get("components", []) or []:
                cid = comp.get("custom_id")
                if cid is not None:
                    out[cid] = comp.get("value", "")
        return out

    async def on_message(self, message: discord.Message) -> None:
        """当接收到消息时触发。

        slash command 不经此处（走 Pycord interaction 通道），故两种模式下都可用。
        mention_and_dm 模式仅处理 @bot 或私信（无特权 intent 时 Discord 也会下发这两类消息的正文）。
        """
        if message.author.bot and not self.allow_bot_messages:
            return

        if self.message_mode != "full_message":
            is_dm = message.guild is None
            is_mention = bool(self.user and self.user in message.mentions)
            if not (is_dm or is_mention):
                return

        logger.debug(
            f"[Discord] Received raw message from {message.author.name}: {message.content}",
        )

        if self.on_message_received:
            message_data = self._create_message_data(message)
            await self.on_message_received(message_data)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """处理交互。

        - component（按钮 / select）/ modal_submit → 路由进 pipeline（统一靠 custom_id）。
        - application_command（slash）/ autocomplete → 交回 Pycord 原生处理，不破坏现有 slash。

        关键事实：Pycord 收到 component 交互时既派发内部 View store 又触发本事件，故无需注册
        持久 View 回调，统一在此路由即可。
        """
        if interaction.type in (
            discord.InteractionType.component,
            discord.InteractionType.modal_submit,
        ):
            if self.on_interaction_received:
                try:
                    interaction_data = self._create_interaction_data(interaction)
                except Exception as e:
                    logger.error(
                        f"[Discord] Failed to build interaction data: {e}",
                        exc_info=True,
                    )
                    return
                await self.on_interaction_received(interaction_data)
            return

        # slash / autocomplete 等仍走 Pycord 原生命令处理
        await self.process_application_commands(interaction)

    def _extract_interaction_content(self, interaction: discord.Interaction) -> str:
        """从交互中提取内容"""
        interaction_type = interaction.type
        interaction_data = getattr(interaction, "data", {})

        if not interaction_data:
            return ""

        if interaction_type == discord.InteractionType.application_command:
            command_name = interaction_data.get("name", "")
            if options := interaction_data.get("options", []):
                params = " ".join(
                    [f"{opt['name']}:{opt.get('value', '')}" for opt in options],
                )
                return f"/{command_name} {params}"
            return f"/{command_name}"

        if interaction_type == discord.InteractionType.component:
            custom_id = interaction_data.get("custom_id", "")
            component_type = interaction_data.get("component_type", "")
            return f"component:{custom_id}:{component_type}"

        return str(interaction_data)

    async def start_polling(self) -> None:
        """开始轮询消息，这是个阻塞方法"""
        await self.start(self.token)

    @override
    async def close(self) -> None:
        """关闭客户端"""
        if not self.is_closed():
            await super().close()
