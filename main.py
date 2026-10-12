import os
import re
import io
import html
import base64
import asyncio
import random
import time
import logging
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from google import genai
from flask import Flask
from threading import Thread

import discord
from discord.ext import commands
from dotenv import load_dotenv
from pathlib import Path

# =========================================================
# KONFIGURACJA
# =========================================================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

CHANNEL_ID = 1557785212056768534
ROLE_ID = 1557785211066917050

WELCOME_CHANNEL_ID = 1557785212056768535
RULES_CHANNEL_ID = 1557785212056768538

TICKET_CHANNEL_ID = 1557785212514078729
TICKET_CATEGORY_ID = 1557785212514078728
SUPPORT_ROLE_ID = 1557785211096530959

# Ranga, która może wysyłać linki (na wszystkich kanałach).
LINK_ALLOWED_ROLE_ID = 1557785211066917047

IMAGE_URL = (
    "https://cdn.discordapp.com/attachments/"
    "1141834695831736381/1557768564361142312/photo.png"
)
TICKET_IMAGE_URL = IMAGE_URL

LOGO_URL = (
    "https://cdn.discordapp.com/attachments/"
    "1141834695831736381/1558244009116311633/content.png"
    "?ex=6ace0712&is=6accb592"
    "&hm=153f27941af11fa389f14453fdae08ca2acdc44b2f55f2fe56371192496fd070&"
)

VERIFY_BUTTON_ID = "drift_code_verify"
TICKET_SELECT_ID = "drift_code_ticket_category"
TICKET_MANAGEMENT_SELECT_ID = "drift_code_ticket_management"

# Limity osadzania zdjęć w transkrypcji (plik HTML musi zmieścić się
# w limicie wysyłania plików na Discordzie).
TRANSCRIPT_MAX_IMAGE_BYTES = 4 * 1024 * 1024
TRANSCRIPT_MAX_TOTAL_IMAGE_BYTES = 5 * 1024 * 1024

TICKET_CATEGORIES = [
    {
        "label": "Pomoc",
        "description": "Uzyskaj pomoc od administracji.",
        "value": "pomoc",
    },
    {
        "label": "Inne",
        "description": "Pozostałe sprawy.",
        "value": "inne",
    },
    {
        "label": "Boty Discord",
        "description": "Pomoc dotycząca botów Discord.",
        "value": "boty_discord",
    },
    {
        "label": "Pluginy Minecraft",
        "description": "Pomoc dotycząca pluginów Minecraft.",
        "value": "pluginy_minecraft",
    },
    {
        "label": "Strefa premium",
        "description": "Sprawy związane ze strefą premium.",
        "value": "strefa_premium",
    },
    {
        "label": "Zgłoś użytkownika",
        "description": "Zgłoś użytkownika administracji.",
        "value": "zglos_uzytkownika",
    },
]

# =========================================================
# INTENTS
# =========================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


# =========================================================
# FUNKCJE POMOCNICZE
# =========================================================

def is_support(member: discord.Member) -> bool:
    return (
            member.guild_permissions.administrator
            or any(role.id == SUPPORT_ROLE_ID for role in member.roles)
    )


def get_category_label(value: str) -> str:
    for category in TICKET_CATEGORIES:
        if category["value"] == value:
            return category["label"]

    return value.replace("_", " ").title()


def make_channel_name(category: str, nick: str) -> str:
    category_part = re.sub(
        r"[^a-z0-9-]",
        "",
        category.lower().replace("_", "-").replace(" ", "-"),
    )

    nick_part = re.sub(
        r"[^a-z0-9-]",
        "",
        nick.lower().replace("_", "-").replace(" ", "-"),
    )

    name = re.sub(
        r"-+",
        "-",
        f"{category_part}-{nick_part}",
    ).strip("-")

    return name[:100] or "ticket-zdc"


def get_ticket_metadata(channel: discord.TextChannel):
    topic = channel.topic or ""

    if not topic.startswith("drift_ticket;"):
        return None

    data = {}

    for part in topic.split(";")[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            data[key] = value

    try:
        return {
            "owner": int(data["owner"]),
            "category": data["category"],
            "created": int(data["created"]),
            "description": data.get("description", ""),
        }
    except (KeyError, ValueError):
        return None


async def find_open_ticket(
        guild: discord.Guild,
        member_id: int,
):
    category = guild.get_channel(TICKET_CATEGORY_ID)

    if not isinstance(category, discord.CategoryChannel):
        return None

    for channel in category.text_channels:
        metadata = get_ticket_metadata(channel)

        if metadata and metadata["owner"] == member_id:
            return channel

    return None


async def interaction_reply(
        interaction: discord.Interaction,
        content: str,
):
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


def component_has_custom_id(components, custom_id: str) -> bool:
    for component in components:
        if getattr(component, "custom_id", None) == custom_id:
            return True

        children = getattr(component, "children", None)

        if children and component_has_custom_id(children, custom_id):
            return True

    return False


# =========================================================
# BLOKADA LINKÓW
# =========================================================

# Wykrywa linki z http(s)://, www. oraz zaproszenia Discord bez schematu.
LINK_REGEX = re.compile(
    r"(?:https?://|www\.)[^\s<>]+"
    r"|(?:discord(?:app)?\.(?:gg|com/invite)|dsc\.gg|dc\.gg)/[^\s<>]+",
    re.IGNORECASE,
)

# Dozwolone domeny (YouTube i serwisy z GIF-ami).
ALLOWED_LINK_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "tenor.com",
    "tenor.co",
    "giphy.com",
    "gph.is",
    "klipy.com",
)

# Bezpośrednie linki do animacji z dowolnego hostingu.
GIF_EXTENSIONS = (".gif", ".gifv", ".apng")

# Znaki doklejane do linków przez markdown / interpunkcję.
TRAILING_LINK_CHARS = ".,;:!?)]}>'\"*_~|"

INVITE_HOSTS = (
    "discord.gg",
    "dsc.gg",
    "dc.gg",
)


def split_link(url: str):
    """Zwraca (host, ścieżka) z adresu URL."""

    # Usuwa znaki doklejone przez markdown/interpunkcję,
    # np. "link.gif)" albo "**link**".
    url = url.rstrip(TRAILING_LINK_CHARS)

    cleaned = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    cleaned = cleaned.split("#", 1)[0].split("?", 1)[0]

    host, _, path = cleaned.partition("/")
    host = host.lower().split("@")[-1].split(":", 1)[0]

    if host.startswith("www."):
        host = host[4:]

    return host, "/" + path.lower().rstrip("/")


def is_invite_link(host: str, path: str) -> bool:
    if host in INVITE_HOSTS:
        return True

    if host in ("discord.com", "discordapp.com") and path.startswith("/invite"):
        return True

    return False


def is_link_allowed(url: str) -> bool:
    host, path = split_link(url)

    # Zaproszenia na inne serwery są zawsze zablokowane.
    if is_invite_link(host, path):
        return False

    # YouTube i serwisy z GIF-ami (razem z subdomenami, np. media.tenor.com).
    for allowed in ALLOWED_LINK_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return True

    # Bezpośrednie linki do GIF-ów z dowolnego hostingu.
    if path.endswith(GIF_EXTENSIONS):
        return True

    return False


def contains_forbidden_link(text: str) -> bool:
    for match in LINK_REGEX.finditer(text or ""):
        if not is_link_allowed(match.group(0)):
            return True

    return False


async def delete_if_forbidden_link(message: discord.Message) -> bool:
    """Usuwa wiadomość z zabronionym linkiem. Zwraca True, jeśli usunięto."""

    if message.guild is None or message.author.bot:
        return False

    author = message.author

    if (
            isinstance(author, discord.Member)
            and any(role.id == LINK_ALLOWED_ROLE_ID for role in author.roles)
    ):
        return False

    if not contains_forbidden_link(message.content):
        return False

    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False

    return True


# =========================================================
# CAPTCHA
# =========================================================

def generate_math():
    a = random.randint(2, 20)
    b = random.randint(1, 15)
    operation = random.choice(["+", "-"])

    if operation == "-" and b > a:
        a, b = b, a

    answer = a + b if operation == "+" else a - b

    return f"{a} {operation} {b}", answer


class VerificationModal(discord.ui.Modal, title="Weryfikacja DriftCode"):
    def __init__(self):
        super().__init__(timeout=300)

        self.question, self.answer = generate_math()

        self.captcha = discord.ui.TextInput(
            label=f"Ile to {self.question}?",
            placeholder="Wpisz wynik działania",
            required=True,
            max_length=10,
        )

        self.add_item(self.captcha)

    async def on_submit(self, interaction: discord.Interaction):
        if (
                not interaction.guild
                or not isinstance(interaction.user, discord.Member)
        ):
            await interaction.response.send_message(
                "Nie udało się odczytać serwera.",
                ephemeral=True,
            )
            return

        try:
            answer = int(str(self.captcha.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Wpisz poprawny wynik liczbowy.",
                ephemeral=True,
            )
            return

        if answer != self.answer:
            await interaction.response.send_message(
                "Niepoprawny wynik. Spróbuj ponownie.",
                ephemeral=True,
            )
            return

        role = interaction.guild.get_role(ROLE_ID)

        if role is None:
            await interaction.response.send_message(
                "Nie znaleziono roli weryfikacyjnej.",
                ephemeral=True,
            )
            return

        bot_member = interaction.guild.me

        if bot_member is None or role >= bot_member.top_role:
            await interaction.response.send_message(
                "Rola weryfikacyjna musi być niżej niż najwyższa rola bota.",
                ephemeral=True,
            )
            return

        if role in interaction.user.roles:
            await interaction.response.send_message(
                "Jesteś już zweryfikowany.",
                ephemeral=True,
            )
            return

        try:
            await interaction.user.add_roles(
                role,
                reason="Pomyślna weryfikacja DriftCode",
            )

            await interaction.response.send_message(
                "Weryfikacja zakończona pomyślnie.",
                ephemeral=True,
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "Bot nie ma uprawnień do nadania tej roli.",
                ephemeral=True,
            )

        except discord.HTTPException:
            await interaction.response.send_message(
                "Wystąpił błąd podczas nadawania roli.",
                ephemeral=True,
            )


class VerifyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="ㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤZweryfikuj sięㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤ",
            style=discord.ButtonStyle.secondary,
            custom_id=VERIFY_BUTTON_ID,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VerificationModal())


class VerificationView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "# ```🏎️ DRIFT CODE × WERYFIKACJA```"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "> Aby uzyskać dostęp do serwera, przejdź weryfikację\n"
                    "> - **__Kliknij przycisk poniżej, aby uzyskać dostęp do serwera.__**"
                ),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media=IMAGE_URL)
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(VerifyButton()),
                accent_colour=discord.Colour.from_rgb(220, 35, 35),
            )
        )


# =========================================================
# PANEL INFORMACJI O TICKECIE
# =========================================================

def make_ticket_view(
        channel: discord.TextChannel,
        metadata: dict | None,
) -> discord.ui.LayoutView:
    if not metadata:
        return TicketManagementView()

    owner_id = metadata["owner"]
    category_value = metadata["category"]
    created_timestamp = metadata["created"]
    description_text = metadata.get("description", "")

    owner = channel.guild.get_member(owner_id)
    owner_nick = (
        owner.display_name if owner else f"Użytkownik {owner_id}"
    )
    owner_mention = owner.mention if owner else f"<@{owner_id}>"

    category_label = get_category_label(category_value)

    elapsed_seconds = max(0, int(time.time()) - created_timestamp)

    days, remainder = divmod(elapsed_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    if days:
        elapsed_text = f"{days}d {hours}h {minutes}min"
    elif hours:
        elapsed_text = f"{hours}h {minutes}min"
    elif minutes:
        elapsed_text = f"{minutes}min {seconds}s"
    else:
        elapsed_text = f"{seconds}s"

    description = (
        f"<:czlonek:1557791722933719070> 〃"
        f"Nick: **{owner_mention}**\n"
        f"<:pytanie:1558161356254093372> 〃"
        f"Opis: **{description_text or 'Nie podano opisu.'}**\n"
        f"<:hash12:1557779133885587567> 〃"
        f"Kategoria: `{category_label}`\n"
        f"<:dzwon:1558155080463683727> 〃"
        f"Czas: <t:{created_timestamp}:F> {elapsed_text}"
    )

    view = discord.ui.LayoutView(timeout=None)

    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(
                f"# ```🏎️ DRIFT CODE × {category_label.upper()}```"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(description),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=TICKET_IMAGE_URL)
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(TicketManagementSelect()),
            accent_colour=discord.Colour.from_rgb(220, 35, 35),
        )
    )

    return view


async def update_ticket_message(
        channel: discord.TextChannel,
        metadata: dict,
):
    view = make_ticket_view(channel, metadata)
    bot_member = channel.guild.me

    if bot_member is None:
        return

    async for message in channel.history(limit=50, oldest_first=True):
        if (
                message.author.id == bot_member.id
                and component_has_custom_id(
            message.components,
            TICKET_MANAGEMENT_SELECT_ID,
        )
        ):
            await message.edit(view=view)
            return

    await channel.send(view=view)


# =========================================================
# TRANSKRYPCJA TICKETU (HTML)
# =========================================================

def _load_transcript_timezone():
    try:
        return ZoneInfo("Europe/Warsaw")
    except Exception:
        return timezone.utc


TRANSCRIPT_TZ = _load_transcript_timezone()


def format_transcript_time(
        value: datetime,
        pattern: str = "%d.%m.%Y %H:%M",
) -> str:
    return value.astimezone(TRANSCRIPT_TZ).strftime(pattern)


def format_file_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"

    if size >= 1024:
        return f"{size / 1024:.0f} KB"

    return f"{size} B"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)

    if days:
        return f"{days}d {hours}h {minutes}min"
    if hours:
        return f"{hours}h {minutes}min"
    if minutes:
        return f"{minutes}min {secs}s"
    return f"{secs}s"


def get_avatar_url(user) -> str:
    try:
        return user.display_avatar.replace(
            size=64,
            static_format="png",
        ).url
    except Exception:
        return user.display_avatar.url


def format_discord_text(text: str, guild: discord.Guild) -> str:
    """Zamienia tekst z Discorda (markdown, wzmianki, emoji) na HTML."""

    if not text:
        return ""

    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = html.escape(text, quote=False)

    # Bloki kodu ``` ```
    def code_block(match):
        content = match.group(1).strip("\n")

        if "\n" in content:
            return keep(f"<pre>{content}</pre>")

        return keep(f'<code class="inline">{content}</code>')

    text = re.sub(
        r"```(?:[a-zA-Z0-9_+-]+\n)?(.*?)```",
        code_block,
        text,
        flags=re.DOTALL,
    )

    # Kod w linii `kod`
    text = re.sub(
        r"`([^`\n]+)`",
        lambda m: keep(f'<code class="inline">{m.group(1)}</code>'),
        text,
    )

    # Wzmianki użytkowników
    def user_mention(match):
        member = guild.get_member(int(match.group(1)))
        name = member.display_name if member else f"Użytkownik {match.group(1)}"
        return keep(f'<span class="mention">@{html.escape(name)}</span>')

    text = re.sub(r"&lt;@!?(\d+)&gt;", user_mention, text)

    # Wzmianki ról
    def role_mention(match):
        role = guild.get_role(int(match.group(1)))
        name = role.name if role else "rola"
        return keep(f'<span class="mention">@{html.escape(name)}</span>')

    text = re.sub(r"&lt;@&amp;(\d+)&gt;", role_mention, text)

    # Wzmianki kanałów
    def channel_mention(match):
        target = guild.get_channel(int(match.group(1)))
        name = target.name if target else "kanał"
        return keep(f'<span class="mention">#{html.escape(name)}</span>')

    text = re.sub(r"&lt;#(\d+)&gt;", channel_mention, text)

    # Niestandardowe emoji
    def custom_emoji(match):
        extension = "gif" if match.group(1) else "png"
        return keep(
            f'<img class="emoji" alt=":{match.group(2)}:" '
            f'src="https://cdn.discordapp.com/emojis/'
            f'{match.group(3)}.{extension}?size=48">'
        )

    text = re.sub(
        r"&lt;(a?):(\w+):(\d+)&gt;",
        custom_emoji,
        text,
    )

    # Znaczniki czasu <t:123:F>
    def timestamp_tag(match):
        moment = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
        return keep(
            f'<span class="mention">'
            f'{format_transcript_time(moment, "%d.%m.%Y %H:%M")}</span>'
        )

    text = re.sub(
        r"&lt;t:(\d+)(?::[tTdDfFR])?&gt;",
        timestamp_tag,
        text,
    )

    # Linki
    def link(match):
        url = match.group(1)
        trailing = ""

        while url and url[-1] in ".,;:!?)":
            trailing = url[-1] + trailing
            url = url[:-1]

        return keep(
            f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'
        ) + trailing

    text = re.sub(
        r'(https?://(?:(?!&gt;|&lt;)[^\s<>"\x00])+)',
        link,
        text,
    )

    # Formatowanie
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(
        r"\|\|(.+?)\|\|",
        r'<span class="spoiler">\1</span>',
        text,
    )
    text = re.sub(
        r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])",
        r"<em>\1</em>",
        text,
    )

    # Nagłówki, cytaty, listy
    result: list[str] = []
    quote_lines: list[str] = []

    def flush_quote():
        if quote_lines:
            result.append(
                "<blockquote>" + "<br>".join(quote_lines) + "</blockquote>"
            )
            quote_lines.clear()

    for line in text.split("\n"):
        if line.startswith("&gt; "):
            quote_lines.append(line[5:])
            continue

        if line == "&gt;":
            quote_lines.append("")
            continue

        flush_quote()

        if line.startswith("### "):
            result.append(f"<h4>{line[4:]}</h4>")
        elif line.startswith("## "):
            result.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("# "):
            result.append(f"<h2>{line[2:]}</h2>")
        elif line.startswith("- "):
            result.append(f'<div class="line">• {line[2:]}</div>')
        elif line.strip() == "":
            result.append('<div class="line">&nbsp;</div>')
        else:
            result.append(f'<div class="line">{line}</div>')

    flush_quote()

    output = "".join(result)

    return re.sub(
        r"\x00(\d+)\x00",
        lambda m: stash[int(m.group(1))],
        output,
    )


def collect_component_content(components):
    """Wyciąga teksty i zdjęcia z wiadomości z komponentami (np. panel ticketu)."""

    texts: list[str] = []
    images: list[str] = []

    for component in components or []:
        content = getattr(component, "content", None)

        if isinstance(content, str) and content:
            texts.append(content)

        for item in getattr(component, "items", None) or []:
            media = getattr(item, "media", None)
            url = getattr(media, "url", None)

            if isinstance(url, str) and url:
                images.append(url)

        children = list(getattr(component, "children", None) or [])
        accessory = getattr(component, "accessory", None)

        if accessory is not None:
            children.append(accessory)

        if children:
            child_texts, child_images = collect_component_content(children)
            texts.extend(child_texts)
            images.extend(child_images)

    return texts, images


def render_embed(embed: discord.Embed, guild: discord.Guild) -> str:
    # GIF-y z Tenora/Giphy: odtwarzane jako zapętlone wideo (animowane).
    if embed.type == "gifv" and embed.video and embed.video.url:
        poster = (
            f' poster="{html.escape(embed.thumbnail.url, quote=True)}"'
            if embed.thumbnail and embed.thumbnail.url
            else ""
        )
        return (
            f'<video class="attachment-video" autoplay loop muted playsinline'
            f'{poster} src="{html.escape(embed.video.url, quote=True)}"></video>'
        )

    if embed.type in ("image", "gifv", "video") and embed.thumbnail.url:
        return (
            f'<img class="attachment-image" alt="" loading="lazy" '
            f'src="{html.escape(embed.thumbnail.url, quote=True)}">'
        )

    colour = embed.colour.value if embed.colour else 0xDC2323
    parts: list[str] = []

    if embed.author and embed.author.name:
        parts.append(
            f'<div class="embed-author">{html.escape(embed.author.name)}</div>'
        )

    if embed.title:
        parts.append(
            f'<div class="embed-title">{html.escape(embed.title)}</div>'
        )

    if embed.description:
        parts.append(
            f'<div class="embed-description">'
            f"{format_discord_text(embed.description, guild)}</div>"
        )

    for field in embed.fields:
        parts.append(
            f'<div class="embed-field"><strong>'
            f"{html.escape(field.name or '')}</strong><br>"
            f"{format_discord_text(field.value or '', guild)}</div>"
        )

    if embed.image.url:
        parts.append(
            f'<img class="attachment-image" alt="" loading="lazy" '
            f'src="{html.escape(embed.image.url, quote=True)}">'
        )
    elif embed.thumbnail.url:
        parts.append(
            f'<img class="embed-thumb" alt="" loading="lazy" '
            f'src="{html.escape(embed.thumbnail.url, quote=True)}">'
        )

    if embed.footer and embed.footer.text:
        parts.append(
            f'<div class="embed-footer">{html.escape(embed.footer.text)}</div>'
        )

    if not parts:
        return ""

    return (
            f'<div class="embed" style="border-left-color:#{colour:06x}">'
            + "".join(parts)
            + "</div>"
    )


async def render_message_body(
        message: discord.Message,
        guild: discord.Guild,
        image_budget: dict,
) -> str:
    parts: list[str] = []

    # Jeśli wiadomość to sam link zamieniony przez Discord w GIF/obraz,
    # pokazujemy tylko GIF, bez gołego adresu nad nim.
    link_only = bool(
        re.fullmatch(r"\s*https?://\S+\s*", message.content or "")
    )
    has_media_embed = any(
        embed.type in ("gifv", "image") for embed in message.embeds
    )

    if message.content and not (link_only and has_media_embed):
        parts.append(
            f'<div class="text">'
            f"{format_discord_text(message.content, guild)}</div>"
        )

    component_texts, component_images = collect_component_content(
        message.components
    )

    if component_texts or component_images:
        panel_parts = [
            f'<div class="text">{format_discord_text(item, guild)}</div>'
            for item in component_texts
        ]

        panel_parts.extend(
            f'<img class="attachment-image" alt="" loading="lazy" '
            f'src="{html.escape(url, quote=True)}">'
            for url in component_images
        )

        parts.append(f'<div class="panel">{"".join(panel_parts)}</div>')

    for attachment in message.attachments:
        content_type = (attachment.content_type or "").split(";")[0].lower()
        name = html.escape(attachment.filename)
        embedded = False

        if (
                content_type.startswith("image/")
                and attachment.size <= TRANSCRIPT_MAX_IMAGE_BYTES
                and attachment.size <= image_budget["left"]
        ):
            try:
                data = await attachment.read()
            except discord.HTTPException:
                data = None

            if data:
                image_budget["left"] -= len(data)
                encoded = base64.b64encode(data).decode("ascii")

                parts.append(
                    f'<img class="attachment-image" alt="{name}" '
                    f'src="data:{content_type};base64,{encoded}">'
                )
                embedded = True

        if not embedded:
            parts.append(
                f'<a class="file" href="{html.escape(attachment.url, quote=True)}" '
                f'target="_blank" rel="noopener">📎 {name} '
                f"<span>{format_file_size(attachment.size)}</span></a>"
            )

    for embed in message.embeds:
        rendered = render_embed(embed, guild)

        if rendered:
            parts.append(rendered)

    for sticker in message.stickers:
        parts.append(
            f'<img class="sticker" alt="{html.escape(sticker.name)}" '
            f'src="{html.escape(sticker.url, quote=True)}">'
        )

    return "".join(parts)


TRANSCRIPT_CSS = """
:root {
    --bg: #07070a;
    --surface: #101014;
    --surface-2: #17171d;
    --border: #26262e;
    --red: #dc2323;
    --red-soft: #ff5a5a;
    --text: #ececf1;
    --muted: #8b8b96;
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }

body {
    margin: 0;
    min-height: 100vh;
    background:
        radial-gradient(900px 500px at 12% -10%, rgba(220, 35, 35, 0.22), transparent 60%),
        radial-gradient(700px 500px at 100% 10%, rgba(120, 10, 10, 0.25), transparent 60%),
        var(--bg);
    background-attachment: fixed;
    color: var(--text);
    font-family: "Inter", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.5;
}

::selection { background: rgba(220, 35, 35, 0.45); }

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: #3a1517; border-radius: 10px; }
::-webkit-scrollbar-thumb:hover { background: var(--red); }

/* Pasek postępu przewijania */
#progress {
    position: fixed;
    top: 0; left: 0;
    height: 3px;
    width: 0;
    background: linear-gradient(90deg, #ff5a5a, #dc2323);
    box-shadow: 0 0 12px var(--red);
    z-index: 100;
}

/* Górny pasek */
.topbar {
    position: relative;
    overflow: hidden;
    display: flex;
    align-items: center;
    gap: 18px;
    padding: 22px 32px;
    background: linear-gradient(110deg, #b01c1c, #5e0d0d 55%, #2a0607);
    box-shadow: 0 6px 30px rgba(0, 0, 0, 0.65);
    animation: dropIn 0.7s cubic-bezier(.2,.8,.2,1) both;
}

.topbar::after {
    content: "";
    position: absolute;
    inset: 0;
    background: linear-gradient(105deg, transparent 35%, rgba(255,255,255,0.12) 50%, transparent 65%);
    transform: translateX(-100%);
    animation: shine 5s ease-in-out infinite 1.2s;
    pointer-events: none;
}

.topbar .logo {
    width: 64px; height: 64px;
    border-radius: 16px;
    object-fit: cover;
    background: #141416;
    border: 2px solid rgba(255, 255, 255, 0.35);
    box-shadow: 0 0 0 0 rgba(220, 35, 35, 0.7);
    animation: pulse 2.6s infinite;
}

.topbar h1 {
    margin: 0;
    font-size: 28px;
    font-weight: 800;
    letter-spacing: 4px;
    text-shadow: 0 2px 14px rgba(0, 0, 0, 0.5);
}

.topbar .sub { opacity: 0.85; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; }

.page { max-width: 980px; margin: 28px auto 0; padding: 0 16px; }

/* Karty informacyjne */
.info {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 12px;
    margin-bottom: 22px;
}

.card {
    position: relative;
    background: linear-gradient(160deg, var(--surface-2), var(--surface));
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px 16px 14px 56px;
    transition: transform 0.25s, border-color 0.25s, box-shadow 0.25s;
    animation: riseIn 0.6s cubic-bezier(.2,.8,.2,1) both;
    animation-delay: calc(var(--i, 0) * 60ms);
}

.card:hover {
    transform: translateY(-3px);
    border-color: rgba(220, 35, 35, 0.6);
    box-shadow: 0 10px 28px rgba(220, 35, 35, 0.15);
}

.card .ico {
    position: absolute;
    left: 14px; top: 50%;
    transform: translateY(-50%);
    width: 32px; height: 32px;
    display: grid; place-items: center;
    background: rgba(220, 35, 35, 0.15);
    border-radius: 10px;
    font-size: 16px;
}

.card .label {
    display: block;
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: var(--red-soft);
    font-weight: 700;
}

.card .value { font-weight: 600; overflow-wrap: anywhere; }
.card.wide { grid-column: 1 / -1; }

/* Pasek narzędzi */
.toolbar {
    position: sticky;
    top: 10px;
    z-index: 50;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    margin-bottom: 14px;
    background: rgba(16, 16, 20, 0.8);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.45);
}

.toolbar input {
    flex: 1;
    min-width: 0;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 9px 14px;
    color: var(--text);
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s;
}

.toolbar input:focus {
    border-color: var(--red);
    box-shadow: 0 0 0 3px rgba(220, 35, 35, 0.2);
}

.toolbar .count { color: var(--muted); font-size: 13px; white-space: nowrap; }

/* Wiadomości */
.messages {
    background: rgba(16, 16, 20, 0.85);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 12px 0;
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.4);
}

.message {
    display: flex;
    gap: 14px;
    padding: 7px 22px;
    border-left: 3px solid transparent;
    transition: background 0.2s, border-color 0.2s;
}

.message:hover { background: rgba(220, 35, 35, 0.06); border-left-color: var(--red); }

.js .message, .js .system {
    opacity: 0;
    transform: translateY(14px);
    transition: opacity 0.5s ease, transform 0.5s cubic-bezier(.2,.8,.2,1), background 0.2s, border-color 0.2s;
}

.js .message.in, .js .system.in { opacity: 1; transform: none; }

.message.hidden, .system.hidden { display: none; }

.message .avatar {
    width: 42px; height: 42px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 2px;
    background: #2a2a2e;
    transition: transform 0.25s, box-shadow 0.25s;
}

.message:hover .avatar { transform: scale(1.08); box-shadow: 0 0 0 2px var(--red); }

.message.grouped { padding-top: 0; padding-bottom: 2px; }

.message .gutter {
    width: 42px;
    flex-shrink: 0;
    font-size: 10px;
    color: transparent;
    text-align: right;
    padding-top: 5px;
}

.message.grouped:hover .gutter { color: var(--muted); }
.message .content { min-width: 0; flex: 1; }

.meta { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.author { font-weight: 700; color: #f4f4f6; }

.badge {
    background: var(--red);
    color: #fff;
    font-size: 10px;
    font-weight: 700;
    padding: 1px 6px;
    border-radius: 5px;
    letter-spacing: 0.5px;
}

.time, .edited { font-size: 12px; color: var(--muted); }

.text { overflow-wrap: anywhere; }
.text h2, .text h3, .text h4 { margin: 6px 0 4px; }
.text h2 { font-size: 22px; }
.text h3 { font-size: 18px; }
.text h4 { font-size: 16px; }

.text a, .embed a {
    color: var(--red-soft);
    text-decoration: none;
    border-bottom: 1px solid transparent;
    transition: border-color 0.2s;
}
.text a:hover, .embed a:hover { border-bottom-color: var(--red-soft); }

.mention {
    background: rgba(220, 35, 35, 0.22);
    color: #ff9a9a;
    border-radius: 5px;
    padding: 0 4px;
    font-weight: 600;
}

code.inline {
    background: #1e1e24;
    border-radius: 5px;
    padding: 1px 6px;
    font-family: "JetBrains Mono", Consolas, "Courier New", monospace;
    font-size: 0.92em;
}

pre {
    background: #121216;
    border: 1px solid var(--border);
    border-left: 3px solid var(--red);
    border-radius: 8px;
    padding: 12px 14px;
    overflow-x: auto;
    font-family: "JetBrains Mono", Consolas, "Courier New", monospace;
    font-size: 13px;
    margin: 6px 0;
}

blockquote {
    margin: 4px 0;
    padding: 3px 14px;
    border-left: 4px solid var(--red);
    background: var(--surface-2);
    border-radius: 0 8px 8px 0;
}

.spoiler {
    background: #000;
    color: transparent;
    border-radius: 4px;
    padding: 0 4px;
    cursor: pointer;
    transition: background 0.25s, color 0.25s;
}
.spoiler.open { background: #26262e; color: #fff; }

.emoji { width: 22px; height: 22px; vertical-align: -5px; }

.attachment-image,
.attachment-video {
    display: block;
    max-width: min(100%, 480px);
    max-height: 360px;
    border-radius: 12px;
    margin: 8px 0;
    border: 1px solid var(--border);
    transition: transform 0.25s, box-shadow 0.25s, border-color 0.25s;
}

.attachment-image { cursor: zoom-in; }

.attachment-image:hover,
.attachment-video:hover {
    transform: scale(1.015);
    border-color: var(--red);
    box-shadow: 0 10px 30px rgba(220, 35, 35, 0.25);
}

.sticker { width: 120px; height: 120px; margin: 6px 0; }

.file {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin: 4px 0;
    padding: 9px 14px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--red-soft);
    text-decoration: none;
    font-weight: 600;
    transition: transform 0.2s, border-color 0.2s;
}
.file:hover { transform: translateX(3px); border-color: var(--red); }
.file span { color: var(--muted); font-weight: 400; }

.panel {
    margin: 6px 0;
    padding: 12px 16px;
    background: linear-gradient(160deg, #1b1315, #131012);
    border: 1px solid #2c1c1f;
    border-left: 4px solid var(--red);
    border-radius: 12px;
    max-width: 640px;
}

.embed {
    margin: 8px 0;
    padding: 10px 14px;
    background: var(--surface-2);
    border-left: 4px solid var(--red);
    border-radius: 0 10px 10px 0;
    max-width: 520px;
}

.embed-title { font-weight: 700; }
.embed-author, .embed-footer { font-size: 12px; color: var(--muted); }
.embed-field { margin-top: 6px; }
.embed-thumb { max-width: 80px; border-radius: 8px; margin-top: 6px; }

.system {
    padding: 6px 22px;
    color: var(--muted);
    font-size: 13px;
    font-style: italic;
}

.empty { padding: 28px; text-align: center; color: var(--muted); }
.no-results { display: none; padding: 28px; text-align: center; color: var(--muted); }
.no-results.show { display: block; }

footer {
    text-align: center;
    color: var(--muted);
    font-size: 12px;
    padding: 26px 0 40px;
}
footer b { color: var(--red); }

/* Przycisk powrotu na górę */
#top {
    position: fixed;
    right: 22px; bottom: 22px;
    width: 46px; height: 46px;
    border: 0;
    border-radius: 50%;
    background: var(--red);
    color: #fff;
    font-size: 20px;
    cursor: pointer;
    opacity: 0;
    transform: translateY(20px);
    pointer-events: none;
    box-shadow: 0 8px 24px rgba(220, 35, 35, 0.5);
    transition: opacity 0.3s, transform 0.3s;
    z-index: 60;
}
#top.show { opacity: 1; transform: none; pointer-events: auto; }
#top:hover { filter: brightness(1.15); }

/* Powiększanie zdjęć */
#lightbox {
    position: fixed;
    inset: 0;
    display: none;
    place-items: center;
    background: rgba(0, 0, 0, 0.88);
    backdrop-filter: blur(6px);
    z-index: 200;
    cursor: zoom-out;
    animation: fadeIn 0.2s both;
}
#lightbox.show { display: grid; }
#lightbox img {
    max-width: 92vw;
    max-height: 90vh;
    border-radius: 12px;
    box-shadow: 0 20px 80px rgba(0, 0, 0, 0.8);
    animation: zoomIn 0.3s cubic-bezier(.2,.8,.2,1) both;
}

/* Animacje */
@keyframes dropIn { from { opacity: 0; transform: translateY(-30px); } to { opacity: 1; transform: none; } }
@keyframes riseIn { from { opacity: 0; transform: translateY(18px); } to { opacity: 1; transform: none; } }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes zoomIn { from { opacity: 0; transform: scale(0.9); } to { opacity: 1; transform: none; } }
@keyframes shine { 0%, 60% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
@keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(255, 90, 90, 0.55); }
    70% { box-shadow: 0 0 0 16px rgba(255, 90, 90, 0); }
    100% { box-shadow: 0 0 0 0 rgba(255, 90, 90, 0); }
}

@media (max-width: 640px) {
    .topbar { padding: 16px; }
    .topbar h1 { font-size: 20px; letter-spacing: 2px; }
    .topbar .logo { width: 48px; height: 48px; }
    .message { padding: 6px 12px; gap: 10px; }
    .system { padding: 6px 12px; }
}

@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; }
    .js .message, .js .system { opacity: 1; transform: none; }
}

@media print {
    body { background: #fff; color: #000; }
    .toolbar, #top, #progress, #lightbox { display: none !important; }
    .js .message, .js .system { opacity: 1; transform: none; }
}
"""

TRANSCRIPT_JS = """
(function () {
    var $ = function (s) { return document.querySelector(s); };
    var items = [].slice.call(document.querySelectorAll('.message, .system'));

    // Pojawianie się wiadomości przy przewijaniu
    if ('IntersectionObserver' in window) {
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
            });
        }, { threshold: 0.05 });
        items.forEach(function (el) { io.observe(el); });
    } else {
        items.forEach(function (el) { el.classList.add('in'); });
    }

    // Wyszukiwarka
    var input = $('#search'), count = $('#count'), none = $('#none');
    var total = document.querySelectorAll('.message').length;

    if (input) {
        input.addEventListener('input', function () {
            var q = input.value.trim().toLowerCase(), shown = 0;
            items.forEach(function (el) {
                var match = !q || el.textContent.toLowerCase().indexOf(q) !== -1;
                el.classList.toggle('hidden', !match);
                if (match) { el.classList.add('in'); if (el.classList.contains('message')) shown++; }
            });
            count.textContent = q ? ('Znaleziono: ' + shown) : (total + ' wiadomości');
            none.classList.toggle('show', !!q && shown === 0);
        });
    }

    // Spoilery
    document.addEventListener('click', function (e) {
        if (e.target.classList.contains('spoiler')) e.target.classList.toggle('open');
    });

    // Powiększanie zdjęć
    var box = $('#lightbox'), big = $('#lightbox img');
    document.addEventListener('click', function (e) {
        if (e.target.classList.contains('attachment-image')) {
            big.src = e.target.src; box.classList.add('show');
        } else if (box.contains(e.target)) {
            box.classList.remove('show');
        }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') box.classList.remove('show');
        if (e.key === '/' && input && document.activeElement !== input) {
            e.preventDefault();
            input.focus();
        }
    });

    // Pasek postępu i przycisk "na górę"
    var bar = $('#progress'), topBtn = $('#top');
    window.addEventListener('scroll', function () {
        var h = document.documentElement;
        var max = h.scrollHeight - h.clientHeight;
        bar.style.width = (max > 0 ? (h.scrollTop / max) * 100 : 0) + '%';
        topBtn.classList.toggle('show', h.scrollTop > 500);
    }, { passive: true });
    topBtn.addEventListener('click', function () { window.scrollTo({ top: 0 }); });
})();
"""


async def build_ticket_transcript(
        channel: discord.TextChannel,
        metadata: dict,
        closed_by,
) -> bytes:
    guild = channel.guild
    image_budget = {"left": TRANSCRIPT_MAX_TOTAL_IMAGE_BYTES}

    rows: list[str] = []
    participants: set[int] = set()
    previous = None
    message_count = 0

    async for message in channel.history(limit=None, oldest_first=True):
        created = format_transcript_time(message.created_at)

        if message.type not in (
                discord.MessageType.default,
                discord.MessageType.reply,
        ):
            system_text = html.escape(message.system_content or "")

            if system_text:
                rows.append(
                    f'<div class="system">{system_text} '
                    f'<span class="time">{created}</span></div>'
                )

            previous = None
            continue

        body = await render_message_body(message, guild, image_budget)

        if not body:
            continue

        message_count += 1
        author = message.author
        participants.add(author.id)

        grouped = (
                previous is not None
                and previous.author.id == author.id
                and (message.created_at - previous.created_at).total_seconds() < 420
        )

        previous = message

        if grouped:
            rows.append(
                f'<div class="message grouped">'
                f'<div class="gutter">'
                f'{format_transcript_time(message.created_at, "%H:%M")}</div>'
                f'<div class="content">{body}</div></div>'
            )
            continue

        colour_value = 0

        if isinstance(author, discord.Member):
            colour_value = author.colour.value

        name_style = f' style="color:#{colour_value:06x}"' if colour_value else ""
        badge = '<span class="badge">BOT</span>' if author.bot else ""
        edited = (
            '<span class="edited">(edytowano)</span>'
            if message.edited_at
            else ""
        )

        rows.append(
            f'<div class="message">'
            f'<img class="avatar" alt="" loading="lazy" '
            f'src="{html.escape(get_avatar_url(author), quote=True)}">'
            f'<div class="content">'
            f'<div class="meta">'
            f'<span class="author"{name_style}>'
            f"{html.escape(author.display_name)}</span>{badge}"
            f'<span class="time">{created}</span>{edited}</div>'
            f"{body}</div></div>"
        )

    if not rows:
        rows.append(
            '<div class="empty">Brak wiadomości w tym tickecie.</div>'
        )

    owner = guild.get_member(metadata["owner"])
    owner_name = (
        owner.display_name if owner else f"Użytkownik {metadata['owner']}"
    )

    created_at = datetime.fromtimestamp(metadata["created"], tz=timezone.utc)
    closed_at = datetime.now(timezone.utc)
    duration = format_duration((closed_at - created_at).total_seconds())

    category_label = get_category_label(metadata["category"])
    description_text = metadata.get("description") or "Nie podano opisu."

    cards = [
        ("🎫", "Ticket", f"#{channel.name}"),
        ("🏷️", "Kategoria", category_label),
        ("👤", "Właściciel", owner_name),
        ("🔒", "Zamknął", closed_by.display_name),
        ("📅", "Utworzono", format_transcript_time(created_at)),
        ("⏱️", "Zamknięto", format_transcript_time(closed_at)),
        ("⌛", "Czas trwania", duration),
        ("💬", "Wiadomości", str(message_count)),
        ("👥", "Uczestnicy", str(len(participants))),
    ]

    cards_html = "".join(
        f'<div class="card" style="--i:{index}">'
        f'<div class="ico">{icon}</div>'
        f'<span class="label">{label}</span>'
        f'<div class="value">{html.escape(value)}</div></div>'
        for index, (icon, label, value) in enumerate(cards)
    )

    cards_html += (
        f'<div class="card wide" style="--i:{len(cards)}">'
        f'<div class="ico">📝</div>'
        f'<span class="label">Opis</span>'
        f'<div class="value">{html.escape(description_text)}</div></div>'
    )

    rows_html = "".join(rows)

    document = f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transkrypcja Drift Code – {html.escape(channel.name)}</title>
<script>document.documentElement.classList.add('js')</script>
<style>{TRANSCRIPT_CSS}</style>
</head>
<body>
<div id="progress"></div>
<header class="topbar">
    <img class="logo" src="{html.escape(LOGO_URL, quote=True)}" alt="Drift Code">
    <div>
        <h1>DRIFT CODE</h1>
        <div class="sub">Transkrypcja ticketu</div>
    </div>
</header>
<main class="page">
    <section class="info">{cards_html}</section>
    <div class="toolbar">
        <input id="search" type="search" placeholder="🔎 Szukaj w wiadomościach...  (naciśnij / )" autocomplete="off">
        <span class="count" id="count">{message_count} wiadomości</span>
    </div>
    <section class="messages">
        {rows_html}
        <div class="no-results" id="none">Nic nie znaleziono 😕</div>
    </section>
</main>
<footer>Transkrypcja wygenerowana przez <b>Drift Code</b></footer>
<button id="top" title="Na górę">↑</button>
<div id="lightbox"><img alt=""></div>
<script>{TRANSCRIPT_JS}</script>
</body>
</html>
"""

    return document.encode("utf-8")


def make_transcript_view(
        filename: str | None = None,
        download_url: str | None = None,
) -> discord.ui.LayoutView:
    items = [
        discord.ui.TextDisplay(
            "# ```🏎️ DRIFT CODE × TRANSKRYPCJA```"
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "> Twoja transkrypcja z ticketu została zapisana, "
            "możesz ją pobrać poniżej."
        ),
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=IMAGE_URL)
        ),
        discord.ui.Separator(),
    ]

    if download_url:
        items.append(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Kliknij tutaj, aby pobrać",
                    style=discord.ButtonStyle.link,
                    url=download_url,
                )
            )
        )
    elif filename:
        items.append(discord.ui.File(f"attachment://{filename}"))

    view = discord.ui.LayoutView(timeout=None)

    view.add_item(
        discord.ui.Container(
            *items,
            accent_colour=discord.Colour.from_rgb(220, 35, 35),
        )
    )

    return view


async def send_transcript_dm(
        user,
        transcript_bytes: bytes,
        filename: str,
):
    try:
        message = await user.send(
            file=discord.File(io.BytesIO(transcript_bytes), filename=filename),
            view=make_transcript_view(filename=filename),
        )
    except discord.HTTPException as error:
        print(f"Nie udało się wysłać transkrypcji do {user}: {error}")
        return

    try:
        download_url = message.attachments[0].url

        await message.edit(
            view=make_transcript_view(download_url=download_url)
        )
    except (IndexError, discord.HTTPException) as error:
        print(f"Nie udało się dodać przycisku pobierania dla {user}: {error}")


async def deliver_transcript(
        guild: discord.Guild,
        metadata: dict,
        closed_by,
        transcript_bytes: bytes,
        filename: str,
):
    recipients = [closed_by]

    owner = guild.get_member(metadata["owner"])

    if owner is None:
        try:
            owner = await bot.fetch_user(metadata["owner"])
        except discord.HTTPException:
            owner = None

    if owner is not None and owner.id != closed_by.id:
        recipients.append(owner)

    for recipient in recipients:
        await send_transcript_dm(recipient, transcript_bytes, filename)


# =========================================================
# MODAL OPISU TICKETU
# =========================================================

class TicketDescriptionModal(
    discord.ui.Modal,
    title="Utwórz ticket",
):
    def __init__(self, category_value: str):
        super().__init__(timeout=300)

        self.category_value = category_value

        self.description_input = discord.ui.TextInput(
            label="Opisz, w czym potrzebujesz pomocy",
            placeholder="Podaj szczegóły zgłoszenia...",
            style=discord.TextStyle.paragraph,
            min_length=5,
            max_length=800,
            required=True,
        )

        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction):
        if (
                not interaction.guild
                or not isinstance(interaction.user, discord.Member)
        ):
            await interaction.response.send_message(
                "Nie udało się odczytać serwera.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        member = interaction.user

        category_value = self.category_value
        category_label = get_category_label(category_value)

        ticket_category = guild.get_channel(TICKET_CATEGORY_ID)
        support_role = guild.get_role(SUPPORT_ROLE_ID)
        bot_member = guild.me

        if not isinstance(ticket_category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Kategoria ticketów nie istnieje.",
                ephemeral=True,
            )
            return

        if support_role is None:
            await interaction.response.send_message(
                "Nie znaleziono roli Support.",
                ephemeral=True,
            )
            return

        if bot_member is None:
            await interaction.response.send_message(
                "Nie udało się odczytać uprawnień bota.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        async with bot.ticket_creation_lock:
            existing_ticket = await find_open_ticket(guild, member.id)

            if existing_ticket:
                await interaction.followup.send(
                    f"Masz już otwarty ticket: {existing_ticket.mention}",
                    ephemeral=True,
                )
                return

            description_value = (
                str(self.description_input.value)
                .replace(";", ",")
                .replace("\n", " ")
                .replace("\r", " ")
                .strip()
            )

            created_timestamp = int(time.time())

            topic = (
                f"drift_ticket;"
                f"owner={member.id};"
                f"category={category_value};"
                f"created={created_timestamp};"
                f"description={description_value}"
            )

            if len(topic) > 1024:
                description_value = description_value[
                    :max(0, 1024 - len(topic) - 3)
                ]

                topic = (
                    f"drift_ticket;"
                    f"owner={member.id};"
                    f"category={category_value};"
                    f"created={created_timestamp};"
                    f"description={description_value}"
                )

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=False,
                ),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                ),
                support_role: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                    attach_files=True,
                    embed_links=True,
                ),
                bot_member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                    manage_messages=True,
                ),
            }

            channel_name = make_channel_name(
                category_value,
                member.name,
            )

            ticket_channel = None

            try:
                ticket_channel = await guild.create_text_channel(
                    name=channel_name,
                    category=ticket_category,
                    overwrites=overwrites,
                    topic=topic,
                    reason=(
                        f"Ticket {category_label} "
                        f"utworzony przez {member}"
                    ),
                )

                metadata = get_ticket_metadata(ticket_channel)

                await ticket_channel.send(
                    view=make_ticket_view(ticket_channel, metadata)
                )

                await interaction.followup.send(
                    f"Ticket został utworzony: {ticket_channel.mention}",
                    ephemeral=True,
                )

            except discord.Forbidden:
                if ticket_channel is not None:
                    try:
                        await ticket_channel.delete(
                            reason="Sprzątanie po błędzie tworzenia ticketu"
                        )
                    except discord.HTTPException:
                        pass

                await interaction.followup.send(
                    "Bot nie ma uprawnień do tworzenia kanałów "
                    "lub zarządzania uprawnieniami.",
                    ephemeral=True,
                )

            except discord.HTTPException as error:
                if ticket_channel is not None:
                    try:
                        await ticket_channel.delete(
                            reason="Sprzątanie po błędzie tworzenia ticketu"
                        )
                    except discord.HTTPException:
                        pass

                await interaction.followup.send(
                    f"Nie udało się utworzyć ticketu: {error}",
                    ephemeral=True,
                )


# =========================================================
# PANEL WYBORU KATEGORII TICKETU
# =========================================================

class TicketCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=item["label"],
                description=item["description"],
                value=item["value"],
            )
            for item in TICKET_CATEGORIES
        ]

        super().__init__(
            placeholder="Wybierz kategorię ticketu...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=TICKET_SELECT_ID,
        )

    async def callback(self, interaction: discord.Interaction):
        if (
                not interaction.guild
                or not isinstance(interaction.user, discord.Member)
        ):
            await interaction_reply(
                interaction,
                "Nie udało się odczytać serwera.",
            )
            return

        ticket_category = interaction.guild.get_channel(
            TICKET_CATEGORY_ID
        )

        if not isinstance(ticket_category, discord.CategoryChannel):
            await interaction_reply(
                interaction,
                "Kategoria ticketów nie istnieje.",
            )
            return

        existing_ticket = await find_open_ticket(
            interaction.guild,
            interaction.user.id,
        )

        if existing_ticket:
            await interaction_reply(
                interaction,
                f"Masz już otwarty ticket: {existing_ticket.mention}",
            )
            return

        if interaction.guild.get_role(SUPPORT_ROLE_ID) is None:
            await interaction_reply(
                interaction,
                "Nie znaleziono roli Support.",
            )
            return

        await interaction.response.send_modal(
            TicketDescriptionModal(self.values[0])
        )


class TicketView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "# ```🏎️ DRIFT CODE × TICKET```"
                ),
                discord.ui.TextDisplay(
                    "> - Potrzebujesz pomocy? Otwórz odpowiednią kategorię.\n"
                    "> - Wybierz z listy poniżej."
                ),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media=TICKET_IMAGE_URL)
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(TicketCategorySelect()),
                accent_colour=discord.Colour.from_rgb(220, 35, 35),
            )
        )


# =========================================================
# DODAWANIE / USUWANIE UŻYTKOWNIKA Z TICKETU
# =========================================================

class ManageTicketUserModal(discord.ui.Modal):
    def __init__(self, action: str):
        title = (
            "Dodaj użytkownika do ticketu"
            if action == "add"
            else "Usuń użytkownika z ticketu"
        )

        super().__init__(title=title, timeout=300)
        self.action = action

        self.user_id_input = discord.ui.TextInput(
            label="ID użytkownika Discord",
            placeholder="Wpisz ID użytkownika",
            required=True,
            min_length=17,
            max_length=20,
        )

        self.add_item(self.user_id_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "Ta funkcja działa tylko na serwerze.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Nie udało się odczytać użytkownika.",
                ephemeral=True,
            )
            return

        if not is_support(interaction.user):
            await interaction.response.send_message(
                "Tylko Support może zarządzać użytkownikami ticketu.",
                ephemeral=True,
            )
            return

        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Ta funkcja działa wyłącznie na kanale ticketu.",
                ephemeral=True,
            )
            return

        metadata = get_ticket_metadata(channel)

        if not metadata:
            await interaction.response.send_message(
                "Ten kanał nie jest prawidłowym ticketem.",
                ephemeral=True,
            )
            return

        raw_id = str(self.user_id_input.value).strip()

        if not raw_id.isdigit():
            await interaction.response.send_message(
                "ID musi zawierać wyłącznie cyfry.",
                ephemeral=True,
            )
            return

        user_id = int(raw_id)
        member = interaction.guild.get_member(user_id)

        if member is None:
            try:
                member = await interaction.guild.fetch_member(user_id)
            except (discord.NotFound, discord.HTTPException):
                member = None

        if member is None:
            await interaction.response.send_message(
                "Nie znaleziono tego użytkownika na serwerze.",
                ephemeral=True,
            )
            return

        if member.id == metadata["owner"]:
            await interaction.response.send_message(
                "Nie możesz w ten sposób zmieniać dostępu właściciela ticketu.",
                ephemeral=True,
            )
            return

        bot_member = interaction.guild.me

        if bot_member and member.id == bot_member.id:
            await interaction.response.send_message(
                "Nie możesz zmieniać dostępu bota.",
                ephemeral=True,
            )
            return

        try:
            if self.action == "add":
                await channel.set_permissions(
                    member,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    reason=f"Użytkownik dodany przez Support: {interaction.user}",
                )

                await interaction.response.send_message(
                    f"Dodano {member.mention} do ticketu.",
                    ephemeral=True,
                )

                await channel.send(
                    f"{member.mention} otrzymał dostęp do tego ticketu "
                    f"od {interaction.user.mention}."
                )

            else:
                await channel.set_permissions(
                    member,
                    overwrite=None,
                    reason=f"Użytkownik usunięty przez Support: {interaction.user}",
                )

                await interaction.response.send_message(
                    f"Usunięto {member.mention} z ticketu.",
                    ephemeral=True,
                )

                await channel.send(
                    f"Użytkownik {member.mention} został usunięty z ticketu "
                    f"przez {interaction.user.mention}."
                )

        except discord.Forbidden:
            await interaction_reply(
                interaction,
                "Bot nie ma uprawnień do zmiany uprawnień kanału.",
            )

        except discord.HTTPException:
            await interaction_reply(
                interaction,
                "Wystąpił błąd podczas zmiany uprawnień.",
            )


# =========================================================
# MENU ZARZĄDZANIA TICKETEM
# =========================================================

class TicketManagementSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Dodaj użytkownika",
                description="Przyznaj użytkownikowi dostęp do ticketu.",
                value="add_user",
                emoji="<:logo:1558134566475005983>",
            ),
            discord.SelectOption(
                label="Usuń użytkownika",
                description="Odbierz użytkownikowi dostęp do ticketu.",
                value="remove_user",
                emoji="<:logo:1558134566475005983>",
            ),
            discord.SelectOption(
                label="Zamknij ticket",
                description="Zamknij i usuń ticket po 10 sekundach.",
                value="close_ticket",
                emoji="<:logo:1558134566475005983>",
            ),
        ]

        super().__init__(
            placeholder="Zarządzanie ticketem — Support",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=TICKET_MANAGEMENT_SELECT_ID,
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            await interaction_reply(
                interaction,
                "Nie udało się odczytać użytkownika.",
            )
            return

        if not is_support(interaction.user):
            await interaction_reply(
                interaction,
                "Tylko ranga Support może używać",
            )
            return

        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction_reply(
                interaction,
                "Ta opcja działa tylko na kanale ticketu.",
            )
            return

        metadata = get_ticket_metadata(channel)

        if not metadata:
            await interaction_reply(
                interaction,
                "Ten kanał nie jest prawidłowym ticketem.",
            )
            return

        selected = self.values[0]

        if selected == "add_user":
            await interaction.response.send_modal(
                ManageTicketUserModal("add")
            )
            return

        if selected == "remove_user":
            await interaction.response.send_modal(
                ManageTicketUserModal("remove")
            )
            return

        if selected == "close_ticket":
            guild = channel.guild
            closer = interaction.user
            transcript_filename = f"transkrypcja-{channel.name}.html"

            await interaction.response.send_message(
                "Ticket zostanie zamknięty za **10 sekund**. "
                "Kanał zostanie usunięty.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

            # Transkrypcja tworzy się w trakcie odliczania.
            transcript_task = asyncio.create_task(
                build_ticket_transcript(channel, metadata, closer)
            )

            await asyncio.sleep(10)

            try:
                transcript_bytes = await transcript_task
            except Exception as error:
                print(f"Błąd tworzenia transkrypcji: {error}")
                transcript_bytes = None

            try:
                await channel.delete(
                    reason=f"Ticket zamknięty przez Support: {interaction.user}"
                )
            except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
            ):
                return

            # Ticket zamknięty poprawnie — wysyłamy transkrypcję.
            if transcript_bytes:
                await deliver_transcript(
                    guild,
                    metadata,
                    closer,
                    transcript_bytes,
                    transcript_filename,
                )


class TicketManagementView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "# ```🏎️ DRIFT CODE × TICKET```"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "Informacje o zgłoszeniu zostaną wyświetlone poniżej."
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(TicketManagementSelect()),
                accent_colour=discord.Colour.from_rgb(220, 35, 35),
            )
        )


# =========================================================
# POWITANIA
# =========================================================

def make_welcome_embed(member: discord.Member) -> discord.ui.LayoutView:
    member_count = member.guild.member_count or 0

    view = discord.ui.LayoutView(timeout=None)

    container = discord.ui.Container(
        discord.ui.Section(
            discord.ui.TextDisplay(
                f"# ```🏎️ DRIFT CODE × POWITANIA```\n\n"
                f"> <:czlonek:1557791722933719070> "
                f"**Witaj {member.mention}** jesteś "
                f"**{member_count} osobą na serwerze**\n\n"
                f"> Miło Cię widzieć na DriftCode!\n"
                f"> Miłej zabawy na serwerze i mamy nadzieję,że zostaniesz z nami na dłużej!\n\n"
                f"> <:rule:1557791903787786356> "
                f"Zapoznaj się z regulaminem "
                f"**<#{RULES_CHANNEL_ID}>**"
            ),
            accessory=discord.ui.Thumbnail(
                member.display_avatar.url
            ),
        ),
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(IMAGE_URL)
        ),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="ᯓ★ ㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤWitaj Na DriftCode ㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤ★ᯓ",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                custom_id="drift_code_welcome_disabled",
            )
        ),
        accent_colour=discord.Colour.from_rgb(220, 35, 35),
    )

    view.add_item(container)
    return view


class WelcomeView(discord.ui.LayoutView):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=None)
        self.add_item(make_welcome_embed(member))

        self.add_item(
            discord.ui.Button(
                label="ᯓ★                Witaj Na DriftCode                   ★ᯓ",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                custom_id="drift_code_welcome_disabled",
            )
        )


# =========================================================
# BOT
# =========================================================

class DriftCodeBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
        )

        self.panel_initialized = False
        self.ticket_panel_initialized = False
        self.status_task = None
        self.ticket_timer_task = None
        self.ticket_creation_lock = asyncio.Lock()

    async def setup_hook(self):
        self.add_view(VerificationView())
        self.add_view(TicketView())
        self.add_view(TicketManagementView())

    async def on_ready(self):
        print(f"Zalogowano jako: {self.user} (ID: {self.user.id})")

        if self.status_task is None or self.status_task.done():
            self.status_task = asyncio.create_task(self.status_loop())

        if not self.panel_initialized:
            await self.send_or_update_panel()
            self.panel_initialized = True

        if not self.ticket_panel_initialized:
            await self.send_or_update_ticket_panel()
            self.ticket_panel_initialized = True

        if self.ticket_timer_task is None or self.ticket_timer_task.done():
            self.ticket_timer_task = asyncio.create_task(
                self.refresh_ticket_messages()
            )

    async def on_message(self, message: discord.Message):
        # Linki są usuwane natychmiast, bez żadnych przerw.
        if await delete_if_forbidden_link(message):
            return

        await self.process_commands(message)

    async def on_message_edit(
            self,
            before: discord.Message,
            after: discord.Message,
    ):
        # Zabezpieczenie przed dodaniem linku przez edycję wiadomości.
        await delete_if_forbidden_link(after)

    async def status_loop(self):
        await self.wait_until_ready()

        # --- USTAWIENIA TEMPA (możesz zmieniać) ---
        TYPING_DELAY = 1  # sekundy między "dopisaniem" kolejnych liter
        CHARS_PER_STEP = 150  # ile liter dopisuje się naraz
        HOLD_TIME = 3  # jak długo gotowy tekst wisi w statusie

        while not self.is_closed():
            member_count = sum(
                guild.member_count or 0 for guild in self.guilds
            )

            texts = [
                "Aver Dev",
                "Dc.gg/DriftCode",
                f"{member_count} osób na dc | Dziękujemy",
            ]

            for text in texts:
                position = 0

                while position < len(text):
                    position = min(position + CHARS_PER_STEP, len(text))

                    try:
                        await self.change_presence(
                            status=discord.Status.dnd,  # Nie przeszkadzać
                            activity=discord.CustomActivity(
                                name=text[:position]
                            ),
                        )
                    except discord.HTTPException:
                        pass

                    if position < len(text):
                        await asyncio.sleep(TYPING_DELAY)

                await asyncio.sleep(HOLD_TIME)

    async def find_existing_panel(self, channel):
        async for message in channel.history(limit=100):
            if message.author.id != self.user.id:
                continue

            if component_has_custom_id(
                    message.components,
                    VERIFY_BUTTON_ID,
            ):
                return message

        return None

    async def send_or_update_panel(self):
        channel = self.get_channel(CHANNEL_ID)

        if not isinstance(channel, discord.TextChannel):
            print("Nie znaleziono kanału weryfikacji.")
            return

        existing = await self.find_existing_panel(channel)

        try:
            if existing:
                await existing.edit(view=VerificationView())
            else:
                await channel.send(view=VerificationView())

        except discord.HTTPException as error:
            print(f"Błąd panelu weryfikacji: {error}")

    async def find_existing_ticket_panel(self, channel):
        async for message in channel.history(limit=100):
            if message.author.id != self.user.id:
                continue

            if component_has_custom_id(
                    message.components,
                    TICKET_SELECT_ID,
            ):
                return message

        return None

    async def send_or_update_ticket_panel(self):
        channel = self.get_channel(TICKET_CHANNEL_ID)

        if not isinstance(channel, discord.TextChannel):
            print("Nie znaleziono kanału panelu ticketów.")
            return

        existing = await self.find_existing_ticket_panel(channel)

        try:
            if existing:
                await existing.edit(view=TicketView())
            else:
                await channel.send(view=TicketView())

        except discord.HTTPException as error:
            print(f"Błąd panelu ticketów: {error}")

    async def refresh_ticket_messages(self):
        await self.wait_until_ready()

        while not self.is_closed():
            category = self.get_channel(TICKET_CATEGORY_ID)

            if isinstance(category, discord.CategoryChannel):
                for channel in category.text_channels:
                    metadata = get_ticket_metadata(channel)

                    if not metadata:
                        continue

                    try:
                        await update_ticket_message(channel, metadata)

                    except discord.HTTPException as error:
                        print(
                            f"Nie udało się odświeżyć ticketu "
                            f"{channel.name}: {error}"
                        )

            await asyncio.sleep(60)

    async def on_member_join(self, member: discord.Member):
        channel = self.get_channel(WELCOME_CHANNEL_ID)

        if channel is None:
            try:
                channel = await self.fetch_channel(WELCOME_CHANNEL_ID)
            except discord.HTTPException as error:
                print(f"Błąd pobierania kanału powitań: {error}")
                return

        if not isinstance(channel, discord.TextChannel):
            print("Błąd: kanał powitań nie jest kanałem tekstowym.")
            return

        try:
            await channel.send(
                view=make_welcome_embed(member),
            )
            print(f"Wysłano powitanie dla {member}.")

        except discord.HTTPException as error:
            print(f"Błąd wiadomości powitalnej: {error}")


# =========================================================
# KOMENDA TESTOWEGO POWITANIA
# =========================================================

@commands.command(name="test")
@commands.has_permissions(administrator=True)
async def test_welcome(ctx):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        await ctx.reply("Nie znaleziono kanału powitań.")
        return

    try:
        await channel.send(view=WelcomeView(ctx.author))
        await ctx.reply("Testowe powitanie zostało wysłane!")
    except discord.HTTPException as error:
        await ctx.reply(f"Nie udało się wysłać powitania: {error}")


# =========================================================
# START
# =========================================================

bot = DriftCodeBot()
bot.add_command(test_welcome)


@test_welcome.error
async def test_welcome_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("Nie masz uprawnień do używania tej komendy.")


if not TOKEN:
    raise RuntimeError(
        "Brak tokenu bota. Ustaw DISCORD_TOKEN w pliku .env."
    )

from google.genai import types

# =========================================================
# KONFIGURACJA
# =========================================================

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID", "0"))
SUBMISSION_CHANNEL_ID = int(os.getenv("SUBMISSION_CHANNEL_ID", "0"))

SUBSCRIBER_ROLE_ID = int(os.getenv("SUBSCRIBER_ROLE_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))

AUTO_APPROVE_THRESHOLD = float(
    os.getenv("AUTO_APPROVE_THRESHOLD", "0.90")
)

MAX_IMAGE_SIZE = 10 * 1024 * 1024

PENDING_FILE = Path(__file__).resolve().parent / "pending_reviews.json"

PANEL_IMAGE_URL = (
    "https://cdn.discordapp.com/attachments/"
    "1141834695831736381/1557768564361142312/photo.png"
    "?ex=6aca5207&is=6ac90087"
    "&hm=a07a0d2ecfed32ab123e570d34d8113ef56207345f8eeef70457a9e28d42f0bc"
)

ACCEPTED_CHANNEL_ID = 1557785212916736023

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("verification-bot")

if not DISCORD_TOKEN:
    raise RuntimeError("Brak DISCORD_TOKEN w pliku .env")

if not GEMINI_API_KEY:
    raise RuntimeError("Brak GEMINI_API_KEY w pliku .env")

if not GUILD_ID:
    raise RuntimeError("Ustaw GUILD_ID w pliku .env")

if not REVIEW_CHANNEL_ID:
    raise RuntimeError("Ustaw REVIEW_CHANNEL_ID w pliku .env")

if not SUBMISSION_CHANNEL_ID:
    raise RuntimeError("Ustaw SUBMISSION_CHANNEL_ID w pliku .env")


# =========================================================
# ZAPIS OCZEKUJĄCYCH ZGŁOSZEŃ
# =========================================================

def load_pending_reviews() -> dict:
    try:
        with PENDING_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}

    except FileNotFoundError:
        return {}

    except (json.JSONDecodeError, OSError):
        logger.exception("Nie udało się odczytać pending_reviews.json")
        return {}


pending_reviews = load_pending_reviews()

pending_users = {
    int(record["user_id"])
    for record in pending_reviews.values()
    if isinstance(record, dict) and "user_id" in record
}

review_locks = set()
persistent_view_registered = False


def save_pending_reviews():
    temp_file = PENDING_FILE.with_suffix(".tmp")

    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(
            pending_reviews,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temp_file.replace(PENDING_FILE)


def remove_pending_review(review_id: str):
    record = pending_reviews.pop(review_id, None)

    if record is not None:
        user_id = int(record["user_id"])

        # Nie usuwaj blokady użytkownika, jeśli istnieje
        # inne oczekujące zgłoszenie tego samego użytkownika.
        if not any(
                int(item.get("user_id", 0)) == user_id
                for item in pending_reviews.values()
        ):
            pending_users.discard(user_id)

        save_pending_reviews()


# =========================================================
# DISCORD I GEMINI
# =========================================================

gemini = genai.Client(api_key=GEMINI_API_KEY)


# =========================================================
# PANEL DM — COMPONENTS V2
# =========================================================

def make_panel(
        member: discord.abc.User,
        title: str,
        description: str,
        status: str,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)

    container = discord.ui.Container()

    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(
                f"# {title}\n{description}"
            ),
            accessory=discord.ui.Thumbnail(
                member.display_avatar.url
            ),
        )
    )

    container.add_item(discord.ui.Separator())

    container.add_item(
        discord.ui.TextDisplay(
            f"**Status zgłoszenia:** {status}"
        )
    )

    container.add_item(discord.ui.Separator())

    container.add_item(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=PANEL_IMAGE_URL)
        )
    )

    view.add_item(container)
    return view


async def send_temporary_status_notification(
        member: discord.abc.User,
        status: str,
):
    try:
        notification = await member.send(
            f"🔔 Status Twojego zgłoszenia: **{status}**"
        )

        await asyncio.sleep(3)

        try:
            await notification.delete()
        except discord.HTTPException:
            pass

    except discord.HTTPException:
        logger.warning(
            "Nie udało się wysłać powiadomienia DM do %s",
            member.id,
        )


async def update_user_status(
        status_message: discord.Message,
        member: discord.abc.User,
        title: str,
        description: str,
        status: str,
):
    # Edytujemy ten sam panel DM.
    await status_message.edit(
        view=make_panel(
            member=member,
            title=title,
            description=description,
            status=status,
        )
    )

    asyncio.create_task(
        send_temporary_status_notification(member, status)
    )


async def show_accepted_panel(
        status_message: discord.Message,
        member: discord.abc.User,
):
    await update_user_status(
        status_message=status_message,
        member=member,
        title="Weryfikacja zakończona",
        description=(
            "Twoje zgłoszenie zostało zaakceptowane. "
            "Rola została przyznana.\n\n"
            "Dziękujemy za zaufanie i miłego korzystania "
            "ze strefy darmowej.\n\n"
            f"<#{ACCEPTED_CHANNEL_ID}>"
        ),
        status="Zaakceptowano ✅",
    )


# =========================================================
# ANALIZA SCREENA PRZEZ GEMINI
# =========================================================

async def analyze_screenshot(image_bytes: bytes) -> dict:
    prompt = """
Przeanalizuj zrzut ekranu dotyczący subskrypcji kanału YouTube.

Zwróć wyłącznie poprawny JSON:
{
  "channel": "nazwa kanału albo Nie można odczytać",
  "subscribed": false,
  "bell_enabled": false,
  "readable": false,
  "time_visible": "godzina albo Nie można odczytać",
  "reason": "krótki opis",
  "confidence": 0.0
}

Zasady:
- subscribed=true tylko przy czytelnym dowodzie subskrypcji.
- bell_enabled=true tylko przy czytelnym dowodzie włączonych
  powiadomień dla kanału.
- readable=true tylko jeśli istotne elementy można odczytać.
- Nie zgaduj nazwy kanału ani godziny.
- reason powinien krótko opisywać widoczne elementy.
- confidence to pewność oceny w zakresie 0.0–1.0.
- Nie wymyślaj niewidocznych informacji.
"""

    fallback = {
        "channel": "Nie można odczytać",
        "subscribed": False,
        "bell_enabled": False,
        "readable": False,
        "time_visible": "Nie można odczytać",
        "reason": "Nie udało się przeanalizować zdjęcia.",
        "confidence": 0.0,
    }

    try:
        response = await asyncio.to_thread(
            gemini.models.generate_content,
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg",
                ),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )

        data = json.loads(response.text)

        try:
            confidence = max(
                0.0,
                min(1.0, float(data.get("confidence", 0.0))),
            )
        except (TypeError, ValueError):
            confidence = 0.0

        return {
            "channel": str(
                data.get("channel", "Nie można odczytać")
            ),
            "subscribed": data.get("subscribed") is True,
            "bell_enabled": data.get("bell_enabled") is True,
            "readable": data.get("readable") is True,
            "time_visible": str(
                data.get("time_visible", "Nie można odczytać")
            ),
            "reason": str(data.get("reason", "Brak opisu.")),
            "confidence": confidence,
        }

    except Exception:
        logger.exception("Błąd analizy Gemini")
        return fallback


# =========================================================
# KANAŁ ADMINISTRACYJNY I SCREEN
# =========================================================

async def get_review_channel() -> discord.TextChannel:
    channel = bot.get_channel(REVIEW_CHANNEL_ID)

    if channel is None:
        channel = await bot.fetch_channel(REVIEW_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(
            "REVIEW_CHANNEL_ID nie wskazuje na kanał tekstowy."
        )

    return channel


async def upload_review_screenshot(
        channel: discord.TextChannel,
        member: discord.abc.User,
        image_bytes: bytes,
        filename: str,
) -> str:
    file = discord.File(
        io.BytesIO(image_bytes),
        filename=filename,
    )

    message = await channel.send(
        content=f"📎 Screen zgłoszenia użytkownika {member.mention}",
        file=file,
    )

    return message.attachments[0].url


# =========================================================
# EMBED ADMINISTRACYJNY
# =========================================================

def make_review_embed(
        member: discord.abc.User,
        analysis: dict,
        screenshot_url: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="📩 SS oczekuje na weryfikację",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )

    embed.add_field(
        name="Od kogo",
        value=member.mention,
        inline=True,
    )

    embed.add_field(
        name="ID",
        value=f"`{member.id}`",
        inline=True,
    )

    embed.add_field(
        name="Opis zdjęcia",
        value=analysis["reason"][:1024],
        inline=False,
    )

    embed.add_field(
        name="Kanał",
        value=analysis["channel"][:1024],
        inline=True,
    )

    embed.add_field(
        name="Subskrypcja",
        value="Tak ✅" if analysis["subscribed"] else "Nie ❌",
        inline=True,
    )

    embed.add_field(
        name="Dzwonek",
        value=(
            "Włączony ✅"
            if analysis["bell_enabled"]
            else "Nie ❌"
        ),
        inline=True,
    )

    embed.add_field(
        name="Czytelność",
        value=(
            "Czytelny ✅"
            if analysis["readable"]
            else "Nieczytelny ❌"
        ),
        inline=True,
    )

    embed.add_field(
        name="Godzina",
        value=analysis["time_visible"][:1024],
        inline=True,
    )

    embed.add_field(
        name="Pewność AI",
        value=f"{analysis['confidence']:.0%}",
        inline=True,
    )

    embed.set_image(url=screenshot_url)
    embed.set_footer(text="Weryfikacja subskrypcji YouTube")

    return embed


# =========================================================
# TRWAŁE PRZYCISKI ADMINISTRATORÓW
# =========================================================

class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)


async def interaction_check(
        self,
        interaction: discord.Interaction,
) -> bool:
    async def deny(message: str) -> bool:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass

        return False

    if not isinstance(interaction.user, discord.Member):
        return await deny(
            "Nie można sprawdzić Twoich uprawnień."
        )

    if not any(
            role.id == ADMIN_ROLE_ID
            for role in interaction.user.roles
    ):
        return await deny(
            "Nie masz uprawnień do rozpatrywania zgłoszeń."
        )

    if (
            interaction.message is None
            or str(interaction.message.id) not in pending_reviews
    ):
        return await deny(
            "To zgłoszenie nie jest już aktywne."
        )

    return True

    async def finish_review(
            self,
            interaction: discord.Interaction,
            approved: bool,
    ):
        review_message = interaction.message

        if review_message is None:
            await interaction.response.send_message(
                "Nie znaleziono wiadomości zgłoszenia.",
                ephemeral=True,
            )
            return

        review_id = str(review_message.id)
        record = pending_reviews.get(review_id)

        if record is None:
            await interaction.response.send_message(
                "To zgłoszenie zostało już rozpatrzone.",
                ephemeral=True,
            )
            return

        # Zarezerwuj zgłoszenie, aby dwa kliknięcia nie nadały
        # roli dwukrotnie ani nie rozpatrzyły go równolegle.
        pending_reviews.pop(review_id)
        save_pending_reviews()

        await interaction.response.defer(ephemeral=True)

        try:
            user_id = int(record["user_id"])
            guild_id = int(record["guild_id"])
            dm_channel_id = int(record["dm_channel_id"])
            status_message_id = int(record["status_message_id"])

            guild = bot.get_guild(guild_id)

            if guild is None:
                guild = await bot.fetch_guild(guild_id)

            member = guild.get_member(user_id)

            if member is None:
                member = await guild.fetch_member(user_id)

            user = bot.get_user(user_id)

            if user is None:
                user = await bot.fetch_user(user_id)

            dm_channel = user.dm_channel

            if dm_channel is None or dm_channel.id != dm_channel_id:
                dm_channel = await user.create_dm()

            status_message = await dm_channel.fetch_message(
                status_message_id
            )

            if approved:
                role = guild.get_role(SUBSCRIBER_ROLE_ID)

                if role is None:
                    raise RuntimeError(
                        "Nie znaleziono roli subskrybenta."
                    )

                await member.add_roles(
                    role,
                    reason=f"Zgłoszenie zatwierdzone przez {interaction.user}",
                )

                await show_accepted_panel(status_message, user)

                decision = "Zaakceptowano ✅"
                color = discord.Color.green()

            else:
                await update_user_status(
                    status_message=status_message,
                    member=user,
                    title="Weryfikacja zakończona",
                    description=(
                        "Twoje zgłoszenie zostało odrzucone.\n\n"
                        "Sprawdź, czy screen wyraźnie pokazuje "
                        "subskrypcję oraz włączony dzwonek, "
                        "a następnie wyślij poprawne zgłoszenie."
                    ),
                    status="Odrzucono ❌",
                )

                decision = "Odrzucono ❌"
                color = discord.Color.red()

            embed = (
                review_message.embeds[0]
                if review_message.embeds
                else discord.Embed(
                    title="Zgłoszenie rozpatrzone"
                )
            )

            embed.color = color

            embed.add_field(
                name="Decyzja moderatora",
                value=(
                    f"{decision}\n"
                    f"Moderator: {interaction.user.mention}"
                ),
                inline=False,
            )

            await review_message.edit(
                embed=embed,
                view=None,
            )

            remove_pending_review(review_id)

            await interaction.followup.send(
                f"Zgłoszenie rozpatrzone: {decision}",
                ephemeral=True,
            )

        except Exception:
            logger.exception("Nie udało się rozpatrzyć zgłoszenia")

            # Przy błędzie przywracamy zapis, aby można było ponowić.
            pending_reviews[review_id] = record
            save_pending_reviews()

            await interaction.followup.send(
                "Wystąpił błąd podczas rozpatrywania zgłoszenia. "
                "Zgłoszenie pozostawiono do ponownej próby.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Nadaj",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="verification:approve",
    )
    async def approve(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
    ):
        await self.finish_review(interaction, approved=True)

    @discord.ui.button(
        label="Odrzuć",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="verification:reject",
    )
    async def reject(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
    ):
        await self.finish_review(interaction, approved=False)


# =========================================================
# START I REJESTRACJA TRWAŁEGO WIDOKU
# =========================================================
# Uwaga: używamy @bot.listen(...) zamiast @bot.event, żeby NIE
# nadpisywać on_ready / on_message z klasy DriftCodeBot
# (inaczej status, panele i blokada linków przestałyby działać).

@bot.listen("on_ready")
async def verification_on_ready():
    global persistent_view_registered

    logger.info("Zalogowano jako %s (%s)", bot.user, bot.user.id)
    logger.info("Kanał zgłoszeń: %s", SUBMISSION_CHANNEL_ID)
    logger.info("Kanał administracyjny: %s", REVIEW_CHANNEL_ID)
    logger.info("Oczekujące zgłoszenia: %s", len(pending_reviews))

    if not persistent_view_registered:
        bot.add_view(ReviewView())
        persistent_view_registered = True


# =========================================================
# OBSŁUGA WIADOMOŚCI I ZGŁOSZEŃ
# =========================================================
# Komendy (!test) przetwarza już on_message z DriftCodeBot,
# więc tutaj nie wywołujemy process_commands.


@bot.listen("on_message")
async def verification_on_message(message: discord.Message):
    if message.author.bot:
        return

    # Bot analizuje screeny wyłącznie na wybranym kanale.
    if (
        message.guild is None
        or message.guild.id != GUILD_ID
        or message.channel.id != SUBMISSION_CHANNEL_ID
    ):
        return

    if not message.attachments:
        return

    image_attachment = next(
        (
            attachment
            for attachment in message.attachments
            if (
                attachment.content_type
                and attachment.content_type.startswith("image/")
            )
        ),
        None,
    )

    if image_attachment is None:
        await message.reply(
            "❌ Wyślij zgłoszenie jako plik graficzny.",
            delete_after=5,
            mention_author=False,
        )
        return

    user_id = message.author.id

    if user_id in review_locks or user_id in pending_users:
        await message.reply(
            "⏳ Masz już zgłoszenie w trakcie weryfikacji.",
            delete_after=5,
            mention_author=False,
        )
        return

    review_locks.add(user_id)

    try:
        if image_attachment.size > MAX_IMAGE_SIZE:
            await message.reply(
                "❌ Zdjęcie jest za duże. Maksymalny rozmiar to 10 MB.",
                delete_after=7,
                mention_author=False,
            )
            return

        try:
            image_bytes = await image_attachment.read()
        except discord.HTTPException:
            await message.reply(
                "❌ Nie udało się pobrać zdjęcia. Spróbuj ponownie.",
                delete_after=5,
                mention_author=False,
            )
            return

        member = message.author

        # Tworzymy jeden panel DM; później tylko go edytujemy.
        try:
            status_message = await member.send(
                view=make_panel(
                    member=member,
                    title="Weryfikacja subskrypcji",
                    description=(
                        "Otrzymaliśmy Twoje zgłoszenie. "
                        "Trwa analiza przesłanego zrzutu ekranu."
                    ),
                    status="Analizowanie zdjęcia ⏳",
                )
            )
        except discord.Forbidden:
            await message.reply(
                "❌ Nie mogę wysłać Ci wiadomości prywatnej. "
                "Włącz DM od członków serwera i wyślij screen ponownie.",
                delete_after=10,
                mention_author=False,
            )
            return

        analysis = await analyze_screenshot(image_bytes)

        passed_checks = (
            analysis["subscribed"]
            and analysis["bell_enabled"]
            and analysis["readable"]
            and analysis["confidence"] >= AUTO_APPROVE_THRESHOLD
        )

        # Automatyczna akceptacja.
        if passed_checks:
            guild = bot.get_guild(GUILD_ID)

            if guild is not None:
                guild_member = guild.get_member(user_id)

                if guild_member is None:
                    try:
                        guild_member = await guild.fetch_member(user_id)
                    except discord.HTTPException:
                        guild_member = None

                role = guild.get_role(SUBSCRIBER_ROLE_ID)

                if guild_member is not None and role is not None:
                    try:
                        await guild_member.add_roles(
                            role,
                            reason="Automatyczna weryfikacja subskrypcji",
                        )

                        await show_accepted_panel(
                            status_message,
                            member,
                        )

                        await message.delete()
                        return

                    except discord.HTTPException:
                        logger.exception(
                            "Automatyczne nadanie roli nie powiodło się"
                        )

        # Ręczna weryfikacja przez administratora.
        await update_user_status(
            status_message=status_message,
            member=member,
            title="Zgłoszenie przyjęte",
            description=(
                "Twoje zgłoszenie zostało przyjęte i oczekuje "
                "na sprawdzenie przez administratora."
            ),
            status="Oczekuje na weryfikację ⏳",
        )

    except discord.Forbidden:
        logger.exception("Brak uprawnień Discord")

        try:
            await message.reply(
                "❌ Bot nie ma wymaganych uprawnień Discord.",
                delete_after=8,
                mention_author=False,
            )
        except discord.HTTPException:
            pass

    except Exception:
        logger.exception("Błąd podczas obsługi zgłoszenia")

        try:
            await message.reply(
                "❌ Wystąpił błąd podczas przetwarzania zgłoszenia. "
                "Spróbuj ponownie później.",
                delete_after=8,
                mention_author=False,
            )
        except discord.HTTPException:
            pass

    finally:
        review_locks.discard(user_id)



# =========================================================
# URUCHOMIENIE
# =========================================================


app = Flask(__name__)


@app.route("/")
def home():
    return "Bot Discord działa!", 200


def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    server = Thread(target=run_web, daemon=True)
    server.start()


keep_alive()

bot.run(DISCORD_TOKEN)
