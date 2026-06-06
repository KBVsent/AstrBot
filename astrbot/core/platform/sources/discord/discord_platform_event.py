import asyncio
import base64
import binascii
from collections.abc import AsyncGenerator
from io import BytesIO
from pathlib import Path
from typing import cast

import discord
from discord.types.interactions import ComponentInteractionData

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    BaseMessageComponent,
    File,
    Image,
    Plain,
    Reply,
)
from astrbot.api.platform import AstrBotMessage, At, PlatformMetadata

from .client import DiscordBotClient
from .components import DiscordEmbed, DiscordView


# 自定义Discord视图组件（兼容旧版本）
class DiscordViewComponent(BaseMessageComponent):
    type: str = "discord_view"
    # discord.ui.View 是非 pydantic 类型，需 arbitrary_types_allowed；按字段声明（不能 self.view=）
    view: discord.ui.View

    class Config:
        arbitrary_types_allowed = True


class DiscordPlatformEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        client: DiscordBotClient,
        interaction_followup_webhook: discord.Webhook | None = None,
        is_ephemeral: bool = False,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client = client
        self.interaction_followup_webhook = interaction_followup_webhook
        # per-trigger 私密响应：True 时本次交互的全部 followup 仅触发者可见（与 defer 时锁定的状态一致）。
        self.is_ephemeral = is_ephemeral
        # 翻页糖：置位后 send() 改为编辑交互来源消息（防刷屏），见 prefer_edit_origin()。
        self._prefer_edit_origin = False

    async def send(self, message: MessageChain) -> str | None:
        """发送消息到 Discord 平台。

        返回所发送消息的 id（字符串）；发送失败、消息为空、或走编辑分支时返回 None。
        返回值供调用方后续 edit_message(message_id, ...) 编辑该消息（loading→结果模式）。
        """
        # 翻页糖：本事件来自交互且置了 prefer_edit_origin → 编辑交互来源消息，不发新消息。
        if self._prefer_edit_origin and isinstance(
            self.message_obj.raw_message, discord.Interaction
        ):
            await self.edit_message("origin", message)
            await super().send(message)
            return None

        # 解析消息链为 Discord 所需的对象
        try:
            (
                content,
                files,
                view,
                embeds,
                reference_message_id,
            ) = await self._parse_to_discord(message)
        except Exception as e:
            logger.error(f"[Discord] 解析消息链时失败: {e}", exc_info=True)
            return None

        # 判断为主动或者被动
        is_interaction = isinstance(self.message_obj.raw_message, discord.Interaction)

        kwargs = {}
        if content:
            kwargs["content"] = content
        if files:
            kwargs["files"] = files
        if view:
            kwargs["view"] = view
        if embeds:
            kwargs["embeds"] = embeds
        # 交互 token 的发送不接受 reference kwarg；仅普通消息回复带引用。
        if reference_message_id and not is_interaction:
            kwargs["reference"] = self.client.get_message(int(reference_message_id))
        if not kwargs:
            logger.debug("[Discord] 尝试发送空消息，已忽略。")
            return None

        sent_message = None
        # 根据上下文执行发送/回复操作
        try:
            # -- 交互上下文（slash / 按钮 / Modal）：被动通道，走交互 token --
            if is_interaction:
                interaction = cast(discord.Interaction, self.message_obj.raw_message)
                # ephemeral 须逐条传入；defer 时已锁定该状态，多段结果保持一致私密。
                if self.is_ephemeral:
                    kwargs["ephemeral"] = True
                if interaction.response.is_done():
                    # 已 ack（defer/响应）→ followup；wait=True 才返回消息对象（供取 id）。
                    sent_message = await interaction.followup.send(wait=True, **kwargs)
                else:
                    # 尚未响应 → 用首响应发出，再尽力取回消息 id（send_message 无返回值）。
                    await interaction.response.send_message(**kwargs)
                    try:
                        sent_message = await interaction.original_response()
                    except Exception:
                        sent_message = None

            # -- 常规消息上下文 / 主动推送：channel.send --
            else:
                channel = await self._get_channel()
                if not channel:
                    return None
                if not isinstance(channel, discord.abc.Messageable):
                    logger.error(f"[Discord] 频道 {channel.id} 不是可发送消息的类型")
                    return None
                sent_message = await channel.send(**kwargs)

        except discord.Forbidden as e:
            # 主动通道无权（Bot 非成员 / 频道无发言权）：明确暴露，避免静默吞掉致"副作用已生效但反馈丢失"。
            logger.error(
                f"[Discord] 发送被拒绝（缺少频道权限，主动消息需 Bot 在该频道有发言权）: {e}"
            )
        except Exception as e:
            logger.error(f"[Discord] 发送消息时发生未知错误: {e}", exc_info=True)

        await super().send(message)
        return str(sent_message.id) if sent_message else None

    async def send_ephemeral(self, message: MessageChain) -> str | None:
        """发送仅触发者可见的 ephemeral 消息，仅交互事件可用。

        交互未响应时作为首响应发送；已 defer/响应时走 followup。
        成功后，本事件后续 ``send()`` 会自动改走 ephemeral followup，避免私密确认后又公开发频道。

        返回 Discord 暴露的消息 id；ephemeral 不是普通频道消息，不能用 ``edit_message(id)`` 编辑。
        如需改写私密气泡，应在气泡内组件再次触发时调用 ``edit_message("origin")``。
        """
        interaction = self.message_obj.raw_message
        if not isinstance(interaction, discord.Interaction):
            logger.warning("[Discord] send_ephemeral 需事件来自交互，已忽略。")
            return None

        try:
            content, files, view, embeds, _ = await self._parse_to_discord(message)
        except Exception as e:
            logger.error(f"[Discord] 解析 ephemeral 消息链失败: {e}", exc_info=True)
            return None

        kwargs: dict = {"ephemeral": True}
        if content:
            kwargs["content"] = content
        if files:
            kwargs["files"] = files
        if view:
            kwargs["view"] = view
        if embeds:
            kwargs["embeds"] = embeds
        if len(kwargs) == 1:
            logger.debug("[Discord] 尝试发送空 ephemeral 消息，已忽略。")
            return None

        sent_message = None
        ok = False
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(**kwargs)
                ok = True
                try:
                    sent_message = await interaction.original_response()
                except Exception:
                    sent_message = None
            else:
                sent_message = await interaction.followup.send(wait=True, **kwargs)
                ok = True
        except Exception as e:
            logger.error(f"[Discord] 发送 ephemeral 消息失败: {e}", exc_info=True)

        if not ok:
            # 发送失败时不标记已发送，让 pipeline 兜底响应继续接管。
            return None

        # 后续 send() 继续走私密 followup，避免泄漏到频道。
        if self.interaction_followup_webhook is None:
            self.interaction_followup_webhook = interaction.followup
        self.is_ephemeral = True

        await super().send(message)
        return str(sent_message.id) if sent_message else None

    def prefer_edit_origin(self) -> None:
        """置位后本事件的 send() 改为编辑交互来源消息（按钮所在那条），用于翻页防刷屏。

        仅对来自交互（按钮/select）的事件有效；普通消息事件置位无副作用（send() 仍发新）。
        """
        self._prefer_edit_origin = True

    async def edit_message(
        self,
        target: "str | int | discord.Message",
        message: MessageChain,
    ) -> None:
        """编辑一条已存在的消息（通用能力）。

        Args:
            target:
                - ``"origin"``：编辑交互来源消息（按钮所在那条），需本事件来自交互。
                - ``discord.Message``：直接编辑该消息对象
                - 消息 id（str/int）：在当前会话频道内 fetch 后编辑（配合 send() 返回的 id）。
            message: 新内容消息链。
        """
        try:
            content, files, view, embeds, _ = await self._parse_to_discord(message)
        except Exception as e:
            logger.error(f"[Discord] 解析待编辑消息链失败: {e}", exc_info=True)
            return

        # content 为空传 None（保留原文案语义由调用方决定）；有新附件则替换旧附件（翻页换图）。
        edit_kwargs: dict = {"content": content or None, "embeds": embeds, "view": view}
        if files:
            edit_kwargs["files"] = files
            edit_kwargs["attachments"] = []

        try:
            if target == "origin":
                interaction = self.message_obj.raw_message
                if not isinstance(interaction, discord.Interaction):
                    logger.warning(
                        "[Discord] edit_message('origin') 需事件来自交互，已忽略。"
                    )
                    return
                # 编辑前必须先 ack（defer），否则 edit_original_response 无原始响应可编辑。
                if not interaction.response.is_done():
                    await interaction.response.defer(invisible=True)
                await interaction.edit_original_response(**edit_kwargs)
            elif isinstance(target, discord.Message):
                await target.edit(**edit_kwargs)
            else:
                channel = await self._get_channel()
                if not channel or not isinstance(channel, discord.abc.Messageable):
                    logger.error(
                        f"[Discord] edit_message 无法获取频道以编辑消息 {target}"
                    )
                    return
                msg = await channel.fetch_message(int(target))
                await msg.edit(**edit_kwargs)
        except Exception as e:
            logger.error(f"[Discord] 编辑消息失败: {e}", exc_info=True)

    async def send_streaming(
        self, generator: AsyncGenerator[MessageChain, None], use_fallback: bool = False
    ):
        buffer = None
        async for chain in generator:
            if not buffer:
                buffer = chain
            else:
                buffer.chain.extend(chain.chain)
        if not buffer:
            return None
        buffer.squash_plain()
        await self.send(buffer)
        return await super().send_streaming(generator, use_fallback)

    async def _get_channel(
        self,
    ) -> discord.Thread | discord.abc.GuildChannel | discord.abc.PrivateChannel | None:
        """获取当前事件对应的频道对象"""
        try:
            channel_id = int(self.session_id)
            return self.client.get_channel(
                channel_id,
            ) or await self.client.fetch_channel(channel_id)
        except (ValueError, discord.errors.NotFound, discord.errors.Forbidden):
            logger.error(f"[Discord] 无法获取频道 {self.session_id}")
            return None

    async def _parse_to_discord(
        self,
        message: MessageChain,
    ) -> tuple[
        str,
        list[discord.File],
        discord.ui.View | None,
        list[discord.Embed],
        str | int | None,
    ]:
        """将 MessageChain 解析为 Discord 发送所需的内容"""
        content_parts = []
        files = []
        view = None
        embeds = []
        reference_message_id = None
        for i in message.chain:  # 遍历消息链
            if isinstance(i, Plain):  # 如果是文字类型的
                content_parts.append(i.text)
            elif isinstance(i, Reply):
                reference_message_id = i.id
            elif isinstance(i, At):
                content_parts.append(f"<@{i.qq}>")
            elif isinstance(i, Image):
                logger.debug(f"[Discord] 开始处理 Image 组件: {i}")
                try:
                    filename = getattr(i, "filename", None)
                    file_content = getattr(i, "file", None)

                    if not file_content:
                        logger.warning(f"[Discord] Image 组件没有 file 属性: {i}")
                        continue

                    discord_file = None

                    # 1. URL
                    if file_content.startswith("http"):
                        logger.debug(f"[Discord] 处理 URL 图片: {file_content}")
                        embed = discord.Embed().set_image(url=file_content)
                        embeds.append(embed)
                        continue

                    # 2. File URI
                    if file_content.startswith("file:///"):
                        logger.debug(f"[Discord] 处理 File URI: {file_content}")
                        path = Path(file_content[8:])
                        if await asyncio.to_thread(path.exists):
                            file_bytes = await asyncio.to_thread(path.read_bytes)
                            discord_file = discord.File(
                                BytesIO(file_bytes),
                                filename=filename or path.name,
                            )
                        else:
                            logger.warning(f"[Discord] 图片文件不存在: {path}")

                    # 3. Base64 URI
                    elif file_content.startswith("base64://"):
                        logger.debug("[Discord] 处理 Base64 URI")
                        b64_data = file_content.split("base64://", 1)[1]
                        missing_padding = len(b64_data) % 4
                        if missing_padding:
                            b64_data += "=" * (4 - missing_padding)
                        img_bytes = base64.b64decode(b64_data)
                        discord_file = discord.File(
                            BytesIO(img_bytes),
                            filename=filename or "image.png",
                        )

                    # 4. 裸 Base64 或本地路径
                    else:
                        try:
                            logger.debug("[Discord] 尝试作为裸 Base64 处理")
                            b64_data = file_content
                            missing_padding = len(b64_data) % 4
                            if missing_padding:
                                b64_data += "=" * (4 - missing_padding)
                            img_bytes = base64.b64decode(b64_data)
                            discord_file = discord.File(
                                BytesIO(img_bytes),
                                filename=filename or "image.png",
                            )
                        except (ValueError, TypeError, binascii.Error):
                            logger.debug(
                                f"[Discord] 裸 Base64 解码失败，作为本地路径处理: {file_content}",
                            )
                            path = Path(file_content)
                            if await asyncio.to_thread(path.exists):
                                file_bytes = await asyncio.to_thread(path.read_bytes)
                                discord_file = discord.File(
                                    BytesIO(file_bytes),
                                    filename=filename or path.name,
                                )
                            else:
                                logger.warning(f"[Discord] 图片文件不存在: {path}")

                    if discord_file:
                        files.append(discord_file)

                except Exception:
                    # 使用 getattr 来安全地访问 i.file，以防 i 本身就是问题
                    file_info = getattr(i, "file", "未知")
                    logger.error(
                        f"[Discord] 处理图片时发生未知严重错误: {file_info}",
                        exc_info=True,
                    )
            elif isinstance(i, File):
                try:
                    file_path_str = await i.get_file()
                    if file_path_str:
                        path = Path(file_path_str)
                        if await asyncio.to_thread(path.exists):
                            file_bytes = await asyncio.to_thread(path.read_bytes)
                            files.append(
                                discord.File(BytesIO(file_bytes), filename=i.name),
                            )
                        else:
                            logger.warning(
                                f"[Discord] 获取文件失败，路径不存在: {file_path_str}",
                            )
                    else:
                        logger.warning(f"[Discord] 获取文件失败: {i.name}")
                except Exception as e:
                    logger.warning(f"[Discord] 处理文件失败: {i.name}, 错误: {e}")
            elif isinstance(i, DiscordEmbed):
                # Discord Embed消息
                embeds.append(i.to_discord_embed())
            elif isinstance(i, DiscordView):
                # Discord视图组件（按钮、选择菜单等）
                view = i.to_discord_view()
            elif isinstance(i, DiscordViewComponent):
                # 如果消息链中包含Discord视图组件（兼容旧版本）
                if isinstance(i.view, discord.ui.View):
                    view = i.view
            else:
                logger.debug(f"[Discord] 忽略了不支持的消息组件: {i.type}")

        content = "".join(content_parts)
        if len(content) > 2000:
            logger.warning("[Discord] 消息内容超过2000字符，将被截断。")
            content = content[:2000]
        return content, files, view, embeds, reference_message_id

    async def react(self, emoji: str) -> None:
        """对原消息添加反应"""
        try:
            if hasattr(self.message_obj, "raw_message") and hasattr(
                self.message_obj.raw_message,
                "add_reaction",
            ):
                await cast(discord.Message, self.message_obj.raw_message).add_reaction(
                    emoji
                )
        except Exception as e:
            logger.error(f"[Discord] 添加反应失败: {e}")

    def is_slash_command(self) -> bool:
        """判断是否为斜杠命令"""
        return (
            hasattr(self.message_obj, "raw_message")
            and hasattr(self.message_obj.raw_message, "type")
            and cast(discord.Interaction, self.message_obj.raw_message).type
            == discord.InteractionType.application_command
        )

    def is_button_interaction(self) -> bool:
        """判断是否为按钮交互"""
        return (
            hasattr(self.message_obj, "raw_message")
            and hasattr(self.message_obj.raw_message, "type")
            and cast(discord.Interaction, self.message_obj.raw_message).type
            == discord.InteractionType.component
        )

    def get_interaction_custom_id(self) -> str:
        """获取交互组件的custom_id"""
        if self.is_button_interaction():
            try:
                return cast(
                    ComponentInteractionData,
                    cast(discord.Interaction, self.message_obj.raw_message).data,
                ).get("custom_id", "")
            except Exception:
                pass
        return ""

    def get_message_outline(self) -> str:
        """交互事件无消息链，按交互类型构造摘要供日志使用"""
        raw = self.message_obj.raw_message
        if not isinstance(raw, discord.Interaction):
            return super().get_message_outline()
        data = getattr(raw, "data", {}) or {}
        custom_id = data.get("custom_id", "") or "?"
        if raw.type == discord.InteractionType.modal_submit:
            values = self.get_modal_values()
            return (
                f"[表单提交] {custom_id} = {values}"
                if values
                else f"[表单提交] {custom_id}"
            )
        if raw.type == discord.InteractionType.component:
            values = data.get("values") or []
            if values:
                return f"[选择交互] {custom_id} = {', '.join(map(str, values))}"
            return f"[按钮交互] {custom_id}"
        return super().get_message_outline()

    async def defer(self, invisible: bool = True) -> None:
        """确认（defer）当前交互——须在 3s 内调用，之后可 edit_message('origin') 或发 followup。

        invisible=True（component 默认）：仅确认、无可见变化。重复调用安全（已响应则跳过）。
        注意：要弹 Modal 必须用 send_modal 作为交互首个响应，不能先 defer。
        """
        interaction = self.message_obj.raw_message
        if not isinstance(interaction, discord.Interaction):
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(invisible=invisible)
        except Exception as e:
            logger.debug(f"[Discord] defer 跳过（可能已响应）: {e}")

    # ---- Modal 弹窗 ----

    async def send_modal(self, custom_id: str, title: str, fields: list[dict]) -> None:
        """对当前交互弹出 Modal 表单。

        **必须是该交互的首个响应**（不能先 ack/defer），故处理器须在 ack 前调用本方法。
        提交后走 on_interaction → pipeline，靠 modal 的 custom_id 路由（同按钮）。

        Args:
            custom_id: modal 标识，提交时用 get_modal_custom_id() 取回以路由。
            title: 弹窗标题（Discord 上限 45 字符）。
            fields: 每项 ``{custom_id, label, style?('short'|'long'), placeholder?,
                value?, required?, min_length?, max_length?}``。
        """
        interaction = self.message_obj.raw_message
        if not isinstance(interaction, discord.Interaction):
            logger.warning("[Discord] send_modal 需事件来自交互，已忽略。")
            return

        modal = discord.ui.Modal(title=title[:45], custom_id=custom_id)
        for f in fields:
            is_long = str(f.get("style", "short")).lower() in (
                "long",
                "paragraph",
                "multiline",
            )
            modal.add_item(
                discord.ui.InputText(
                    style=discord.InputTextStyle.long
                    if is_long
                    else discord.InputTextStyle.short,
                    custom_id=str(f.get("custom_id") or f.get("label") or "field"),
                    label=str(f.get("label") or f.get("custom_id") or "")[:45],
                    placeholder=(
                        str(f["placeholder"])[:100] if f.get("placeholder") else None
                    ),
                    value=(str(f["value"]) if f.get("value") is not None else None),
                    required=bool(f.get("required", True)),
                    min_length=f.get("min_length"),
                    max_length=f.get("max_length"),
                )
            )
        try:
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"[Discord] 发送 Modal 失败: {e}", exc_info=True)

    def is_modal_submit(self) -> bool:
        """判断是否为 Modal 提交交互。"""
        raw = self.message_obj.raw_message
        return (
            isinstance(raw, discord.Interaction)
            and raw.type == discord.InteractionType.modal_submit
        )

    def get_modal_custom_id(self) -> str:
        """获取 Modal 的 custom_id（用于路由）。"""
        raw = self.message_obj.raw_message
        if isinstance(raw, discord.Interaction):
            return (getattr(raw, "data", {}) or {}).get("custom_id", "")
        return ""

    def get_modal_values(self) -> dict[str, str]:
        """获取 Modal 各输入框的值，扁平成 {input_custom_id: value}。"""
        raw = self.message_obj.raw_message
        if not isinstance(raw, discord.Interaction):
            return {}
        # 复用 client 的解析（单一实现，非 modal 提交时其内部返回 {}）
        return DiscordBotClient._extract_modal_values(raw)

    # ---- Select 菜单 ----

    def get_interaction_values(self) -> list[str]:
        """获取 select 菜单交互选中的值列表（非 select 交互返回空列表）。"""
        raw = self.message_obj.raw_message
        if isinstance(raw, discord.Interaction):
            return list((getattr(raw, "data", {}) or {}).get("values", []) or [])
        return []

    def is_mentioned(self) -> bool:
        """判断机器人是否被@"""
        if hasattr(self.message_obj, "raw_message") and hasattr(
            self.message_obj.raw_message,
            "mentions",
        ):
            return any(
                mention.id == int(self.message_obj.self_id)
                for mention in cast(
                    discord.Message, self.message_obj.raw_message
                ).mentions
            )
        return False

    def get_mention_clean_content(self) -> str:
        """获取去除@后的清洁内容"""
        if hasattr(self.message_obj, "raw_message") and hasattr(
            self.message_obj.raw_message,
            "clean_content",
        ):
            return cast(discord.Message, self.message_obj.raw_message).clean_content
        return self.message_str
