import discord

from astrbot.api.message_components import BaseMessageComponent


class _DiscordComponent(BaseMessageComponent):
    """Discord 专用组件基类"""

    class Config:
        arbitrary_types_allowed = True


# Discord专用组件
class DiscordEmbed(_DiscordComponent):
    """Discord Embed消息组件"""

    type: str = "discord_embed"
    title: str | None = None
    description: str | None = None
    color: int | None = None
    url: str | None = None
    thumbnail: str | None = None
    image: str | None = None
    footer: str | None = None
    fields: list[dict] = []

    def to_discord_embed(self) -> discord.Embed:
        """转换为Discord Embed对象"""
        embed = discord.Embed()

        if self.title:
            embed.title = self.title
        if self.description:
            embed.description = self.description
        if self.color:
            embed.color = self.color
        if self.url:
            embed.url = self.url
        if self.thumbnail:
            embed.set_thumbnail(url=self.thumbnail)
        if self.image:
            embed.set_image(url=self.image)
        if self.footer:
            embed.set_footer(text=self.footer)

        for field in self.fields:
            embed.add_field(
                name=field.get("name", ""),
                value=field.get("value", ""),
                inline=field.get("inline", False),
            )

        return embed


class DiscordButton(_DiscordComponent):
    """Discord按钮组件"""

    type: str = "discord_button"
    label: str = ""
    custom_id: str | None = None
    style: str = "primary"
    emoji: str | None = None
    url: str | None = None
    disabled: bool = False
    # 多行布局：同一 row 的按钮排在一行（Discord 每行最多 5 个、每条消息最多 5 行）。
    row: int | None = None


class DiscordSelect(_DiscordComponent):
    """Discord 下拉选择菜单组件。

    select_type:
        - ``"string"``：自定义选项（必须给 ``options``，每项 {label, value?, description?,
          emoji?, default?}）。
        - ``"user"`` / ``"role"`` / ``"channel"`` / ``"mentionable"``：由 Discord 填充对应实体。
    选中后走 component 交互，event.get_interaction_values() 取选中值。
    """

    type: str = "discord_select"
    custom_id: str = ""
    placeholder: str | None = None
    options: list[dict] = []
    select_type: str = "string"
    min_values: int = 1
    max_values: int = 1
    disabled: bool = False
    row: int | None = None


class DiscordReference(_DiscordComponent):
    """Discord引用组件"""

    type: str = "discord_reference"
    message_id: str = ""
    channel_id: str = ""


class DiscordView(_DiscordComponent):
    """Discord视图组件，包含按钮和选择菜单"""

    type: str = "discord_view"
    # 用裸 list 注解，避免 pydantic 对元素做模型强制/复制而丢失 DiscordButton/DiscordSelect 子类
    components: list = []
    timeout: float | None = None

    def to_discord_view(self) -> discord.ui.View:
        """转换为Discord View对象"""
        # select_type 字符串 → Discord ComponentType（仅本方法用，就近定义）
        select_type_map = {
            "string": discord.ComponentType.string_select,
            "user": discord.ComponentType.user_select,
            "role": discord.ComponentType.role_select,
            "channel": discord.ComponentType.channel_select,
            "mentionable": discord.ComponentType.mentionable_select,
        }

        view = discord.ui.View(timeout=self.timeout)

        for component in self.components:
            if isinstance(component, DiscordButton):
                button_style = getattr(
                    discord.ButtonStyle,
                    component.style,
                    discord.ButtonStyle.primary,
                )

                if component.url:
                    # URL（link）按钮：无 custom_id，点击仅跳转、不产生交互
                    button = discord.ui.Button(
                        label=component.label,
                        style=discord.ButtonStyle.link,
                        url=component.url,
                        emoji=component.emoji,
                        disabled=component.disabled,
                        row=component.row,
                    )
                else:
                    # custom_id 回调按钮：点击产生 component 交互，靠 custom_id 路由
                    button = discord.ui.Button(
                        label=component.label,
                        style=button_style,
                        custom_id=component.custom_id,
                        emoji=component.emoji,
                        disabled=component.disabled,
                        row=component.row,
                    )

                view.add_item(button)

            elif isinstance(component, DiscordSelect):
                select_type = select_type_map.get(
                    component.select_type,
                    discord.ComponentType.string_select,
                )
                select_kwargs: dict = {
                    "select_type": select_type,
                    "custom_id": component.custom_id,
                    "placeholder": component.placeholder,
                    "min_values": component.min_values,
                    "max_values": component.max_values,
                    "disabled": component.disabled,
                    "row": component.row,
                }
                # 仅 string select 接受自定义 options
                if select_type == discord.ComponentType.string_select:
                    select_kwargs["options"] = [
                        discord.SelectOption(
                            label=str(opt.get("label", "")),
                            value=str(opt.get("value", opt.get("label", ""))),
                            description=opt.get("description"),
                            emoji=opt.get("emoji"),
                            default=bool(opt.get("default", False)),
                        )
                        for opt in component.options
                    ]
                view.add_item(discord.ui.Select(**select_kwargs))

        return view
