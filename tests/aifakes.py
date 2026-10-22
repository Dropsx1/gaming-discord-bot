"""Shared fakes for the AI upgrade tests (not a test module itself)."""

import contextlib
import itertools
import types

import discord

_ids = itertools.count(500_000)


def new_id() -> int:
    return next(_ids)


class Sent:
    def __init__(self, content=None, **kw):
        self.content, self.kw = content, kw
        self.embed = kw.get("embed")
        self.view = kw.get("view")
        self.id = new_id()
        self.threads = []
        self.jump_url = f"https://discord.com/channels/x/{self.id}"

    async def create_thread(self, name, **kw):
        t = FakeThread(self.channel.guild, name, parent=self.channel)
        self.threads.append(t)
        return t


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeText(discord.TextChannel):  # passes isinstance(..., TextChannel)
    __hash__ = object.__hash__
    mention = property(lambda self: f"<#{self.id}>")

    def __init__(self, guild, name, public=True, category_id=None):
        self.id, self.guild, self.name, self.public, self.category_id = new_id(), guild, name, public, category_id
        self.sent: list[Sent] = []
        self.history_msgs = []  # oldest first
        self.topic = None

    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=self.public, send_messages=True, embed_links=True)

    async def send(self, content=None, **kw):
        msg = Sent(content, **kw)
        msg.channel = self
        self.sent.append(msg)
        return msg

    def typing(self):
        return _Typing()

    def history(self, limit=100, before=None, oldest_first=None):
        msgs = list(self.history_msgs)
        if before is not None:
            msgs = [m for m in msgs if m is not before and m.id < before.id]
        msgs = msgs[::-1][:limit]  # newest first, like Discord

        async def gen():
            for m in msgs:
                yield m
        return gen()


class FakeThread(discord.Thread):
    __hash__ = object.__hash__
    mention = property(lambda self: f"<#{self.id}>")
    parent = property(lambda self: self._parent)

    def __init__(self, guild, name, parent):
        self.id, self.guild, self.name, self._parent = new_id(), guild, name, parent
        self.parent_id = parent.id
        self.sent = []

    def is_private(self):
        return False

    async def send(self, content=None, **kw):
        msg = Sent(content, **kw)
        msg.channel = self
        self.sent.append(msg)
        return msg

    def typing(self):
        return _Typing()


class Guild:
    def __init__(self, name="Test Server"):
        self.id, self.name = new_id(), name
        self.default_role = object()
        self._channels = {}
        self.roles = []
        self.member_count = 42
        self.me = types.SimpleNamespace(guild_permissions=discord.Permissions(manage_messages=True, manage_channels=True))
        self.created_categories = []

    def add(self, name, **kw):
        ch = FakeText(self, name, **kw)
        self._channels[ch.id] = ch
        return ch

    @property
    def text_channels(self):
        return [c for c in self._channels.values() if isinstance(c, FakeText)]

    @property
    def categories(self):
        return [c for c in self._channels.values() if isinstance(c, discord.CategoryChannel)]

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_channel_or_thread(self, cid):
        return self._channels.get(cid)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, uid):
        return None


class Member(discord.Member):
    def __init__(self, guild, staff=False, name=None):
        self.guild, self._staff = guild, staff
        self._user = types.SimpleNamespace(id=new_id(), bot=False)
        self._name = name
        self.sent = []

    id = property(lambda self: self._user.id)
    bot = property(lambda self: False)
    guild_permissions = property(lambda self: discord.Permissions(manage_guild=self._staff))
    mention = property(lambda self: f"<@{self.id}>")
    display_name = property(lambda self: self._name or f"user{self.id}")
    name = property(lambda self: self._name or f"user{self.id}")
    display_avatar = property(lambda self: types.SimpleNamespace(url="https://cdn.example/a.png"))

    def get_role(self, rid):
        return None

    def __str__(self):
        return self.display_name

    async def send(self, text=None, **kw):
        self.sent.append(text)


class BotAuthor:
    def __init__(self):
        self.id, self.bot, self.display_name = new_id(), True, "SomeBot"


class Attachment:
    def __init__(self, url, content_type="image/png", size=1000):
        self.url, self.proxy_url, self.content_type, self.size = url, url, content_type, size
        self.filename = url.rsplit("/", 1)[-1]


class Msg:
    def __init__(self, guild, channel, author, content, mentions=(), attachments=()):
        self.id = new_id()
        self.guild, self.channel, self.author, self.content = guild, channel, author, content
        self.webhook_id, self.raw_mentions, self.raw_role_mentions = None, [], []
        self.mentions = list(mentions)
        self.attachments = list(attachments)
        self.deleted = False
        self.replies = []
        self.created_at = discord.utils.utcnow()

    async def delete(self):
        self.deleted = True

    async def reply(self, content=None, **kw):
        self.replies.append(content)


class Ctx:
    def __init__(self, guild, channel, author):
        self.guild, self.channel, self.author, self.replies, self.kw = guild, channel, author, [], []
        self.interaction = None

    async def send(self, content=None, **kw):
        self.replies.append(content)
        self.kw.append(kw)
        msg = Sent(content, **kw)
        msg.channel = self.channel
        return msg

    async def defer(self, **kw):
        pass


class Response:
    def __init__(self):
        self.messages, self.done, self.modal = [], False, None

    async def send_message(self, content=None, **kw):
        self.messages.append((content, kw))
        self.done = True

    async def defer(self, **kw):
        self.done = True

    async def send_modal(self, modal):
        self.modal = modal
        self.done = True

    async def edit_message(self, **kw):
        self.messages.append((kw.get("content"), kw))
        self.done = True

    def is_done(self):
        return self.done


class Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kw):
        self.messages.append((content, kw))


class Interaction:
    def __init__(self, guild, channel, user, message=None, client=None):
        self.guild, self.channel, self.user, self.message, self.client = guild, channel, user, message, client
        self.guild_id = guild.id
        self.response, self.followup = Response(), Followup()


@contextlib.contextmanager
def patched(module, **fakes):
    """Temporarily replace attributes (e.g. ai functions); always restored."""
    old = {k: getattr(module, k) for k in fakes}
    for k, v in fakes.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(module, k, v)
