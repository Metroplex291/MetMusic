# -*- coding: utf-8 -*-
import asyncio
import json
import os
import re
import shutil
import sys
import traceback
from typing import Union, Optional
from zipfile import ZipFile

import disnake
import dotenv
import humanize
from aiohttp import ClientSession
from disnake.ext import commands
from disnake.http import Route

import wavelink
from config_loader import DEFAULT_CONFIG, load_config
from utils.client import BotCore
from utils.db import DBModel
from utils.music.checks import check_voice, check_requester_channel, can_connect
from utils.music.converters import URL_REG
from utils.music.errors import GenericError, NoVoice
from utils.music.interactions import SelectBotVoice
from utils.music.models import LavalinkPlayer
from utils.others import sync_message, CustomContext, string_to_file, token_regex, CommandArgparse, \
    select_bot_pool
from utils.owner_panel import panel_command, PanelView


def format_git_log(data_list: list):

    data = []

    for d in data_list:
        if not d:
            continue
        t = d.split("*****")
        data.append({"commit": t[0], "abbreviated_commit": t[1], "subject": t[2], "timestamp": t[3]})

    return data


async def run_command(cmd: str):

    p = await asyncio.create_subprocess_shell(
        cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ
    )
    stdout, stderr = await p.communicate()
    r = ShellResult(p.returncode, stdout, stderr)
    if r.status != 0:
        raise Exception(f"{r.stderr or r.stdout}\n\nStatus Code: {r.status}")
    return str(r.stdout)


class ShellResult:

    def __init__(self, status: int, stdout: Optional[bytes], stderr: Optional[bytes]):
        self.status = status
        self.stdout = stdout.decode(encoding="utf-8", errors="replace") if stdout is not None else None
        self.stderr = stderr.decode(encoding="utf-8", errors="replace") if stderr is not None else None


class Owner(commands.Cog):

    os_quote = "\"" if os.name == "nt" else "'"
    git_format = f"--pretty=format:{os_quote}%H*****%h*****%s*****%ct{os_quote}"

    extra_files = [
        "./playlist_cache.json",
    ]

    additional_files = [
        "./lavalink.ini",
        "./application.yml",
        "./squarecloud.config",
        "./squarecloud.app",
        "./discloud.config",
    ]

    extra_dirs = [
        "local_database",
        ".player_sessions"
    ]

    def __init__(self, bot: BotCore):
        self.bot = bot
        self.git_init_cmds = [
            "git init",
            f'git remote add origin {self.bot.config["SOURCE_REPO"]}',
            'git fetch origin',
            'git --work-tree=. checkout -b main -f --track origin/main'
        ]
        self.owner_view: Optional[PanelView] = None
        self.extra_hints = bot.config["EXTRA_HINTS"].split("||")

    def format_log(self, data: list):
        return "\n".join(f"[`{c['abbreviated_commit']}`]({self.bot.pool.remote_git_url}/commit/{c['commit']}) `- "
                         f"{(c['subject'][:40].replace('`', '') + '...') if len(c['subject']) > 39 else c['subject']}` "
                         f"(<t:{c['timestamp']}:R>)" for c in data)

    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.is_owner()
    @commands.command(
        hidden=True, aliases=["gls", "lavalink", "lllist", "lavalinkservers"],
        description="Download a file with a list of Lavalink servers to use them in the music system."
    )
    async def getlavaservers(self, ctx: CustomContext):

        await ctx.defer()

        await self.download_lavalink_serverlist()

        await ctx.send(
            embed=disnake.Embed(
                description="**The lavalink.ini file has been successfully downloaded!\n"
                            "I will need to restart to use the servers from this file.**"
            )
        )

    updatelavalink_flags = CommandArgparse()
    updatelavalink_flags.add_argument('-yml', '--yml', action='store_true',
                                    help="Download the application.yml file.")
    updatelavalink_flags.add_argument("-resetids", "-reset", "--resetids", "--reset",
                                    help="Reset music id info (useful to avoid problems with certain "
                                        "changes in lavaplayer/lavalink).", action="store_true")

    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.default)
    @commands.command(hidden=True, aliases=["restartll", "rtll", "rll"])
    async def restartlavalink(self, ctx: CustomContext):

        if not self.bot.pool.lavalink_instance:
            raise GenericError("**The LOCAL server is not being used!**")

        await self.bot.pool.start_lavalink()

        await ctx.send(
            embed=disnake.Embed(
                description="**Restarting the local Lavalink server.**",
                color=self.bot.get_color(ctx.guild.me)
            )
        )

    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.default)
    @commands.command(hidden=True, aliases=["ull", "updatell", "llupdate", "llu"], extras={"flags": updatelavalink_flags})
    async def updatelavalink(self, ctx: CustomContext, flags: str = ""):

        if not self.bot.pool.lavalink_instance:
            raise GenericError("**The LOCAL server is not being used!**")

        args, unknown = ctx.command.extras['flags'].parse_known_args(flags.split())

        try:
            self.bot.pool.lavalink_instance.kill()
        except:
            pass

        async with ctx.typing():

            await asyncio.sleep(1.5)

            if os.path.isfile("./Lavalink.jar"):
                os.remove("./Lavalink.jar")

            if args.yml and os.path.isfile("./application.yml"):
                os.remove("./application.yml")

            await self.bot.pool.start_lavalink()

        await ctx.send(
            embed=disnake.Embed(
                description="**The Lavalink.jar file will be updated "
                            "and the local Lavalink server will be restarted.**",
                color=self.bot.get_color(ctx.guild.me)
            )
        )

    @commands.is_owner()
    @panel_command(aliases=["rcfg"], description="Reload bot configurations.", emoji="⚙",
                   alt_name="Reload bot configurations.")
    async def reloadconfig(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        self.bot.pool.load_cfg()

        txt = "**The bot settings have been reloaded successfully!**"

        if isinstance(ctx, CustomContext):
            embed = disnake.Embed(colour=self.bot.get_color(ctx.me), description=txt)
            await ctx.send(embed=embed, view=self.owner_view)
        else:
            return txt

    @commands.is_owner()
    @panel_command(aliases=["rd"], description="Reload modules.", emoji="🔄",
                   alt_name="Load/Reload modules.")
    async def reload(self, ctx: Union[CustomContext, disnake.MessageInteraction], *modules):

        for m in list(sys.modules):
            if not m.startswith("utils.music.skins."):
                continue
            try:
                del sys.modules[m]
            except:
                continue

        modules = [f"{m}.py" for m in modules]

        data = self.bot.load_modules(modules)
        self.bot.load_skins()

        await self.bot.sync_app_commands(force=self.bot == self.bot.pool.controller_bot)

        for bot in self.bot.pool.bots:

            if bot.user.id != self.bot.user.id:
                bot.load_skins()
                bot.load_modules(modules)
                await bot.sync_app_commands(force=bot == self.bot.pool.controller_bot)

        self.bot.sync_command_cooldowns()

        txt = ""

        if data["loaded"]:
            txt += f'**Loaded modules:** ```ansi\n[0;34m{" [0;37m| [0;34m".join(data["loaded"])}```\n'

        if data["reloaded"]:
            txt += f'**Reloaded modules:** ```ansi\n[0;32m{" [0;37m| [0;32m".join(data["reloaded"])}```\n'

        if not txt:
            txt = "**No modules found...**"

        self.bot.pool.config = load_config()

        if isinstance(ctx, CustomContext):
            embed = disnake.Embed(colour=self.bot.get_color(ctx.me), description=txt)
            await ctx.send(embed=embed, view=self.owner_view)
        else:
            return txt

    update_flags = CommandArgparse()
    update_flags.add_argument("-force", "--force", action="store_true",
                              help="Force update ignoring the state of the local repository).")
    update_flags.add_argument("-pip", "--pip", action="store_true",
                              help="Install/update dependencies after the update.")

    

    @commands.Cog.listener("on_button_click")
    async def update_buttons(self, inter: disnake.MessageInteraction):

        if not inter.data.custom_id.startswith("updatecmd_"):
            return

        if inter.data.custom_id.startswith("updatecmd_requirements"):

            try:
                os.remove('./update_reqs.zip')
            except FileNotFoundError:
                pass

            with ZipFile('update_reqs.zip', 'w') as zipObj:
                zipObj.write("requirements.txt")

            await inter.send(
                embed=disnake.Embed(
                    description="**Download the attached file and send it to your hosting via commit etc.**",
                    color=self.bot.get_color(inter.guild.me)
                ),
                file=disnake.File("update_reqs.zip")
            )

            os.remove("update_reqs.zip")
            return

        # install installdeps

        if inter.data.custom_id.startswith("updatecmd_installdeps_force_"):
            await self.cleanup_git(force=True)

        await inter.message.delete()

        args, unknown = self.bot.get_command("update").extras['flags'].parse_known_args(["-pip"])

        await self.update_deps(inter, "", args, use_poetry=inter.data.custom_id.endswith("_poetry"))

    async def cleanup_git(self, force=False):

        if force:
            try:
                shutil.rmtree(os.environ["GIT_DIR"])
            except FileNotFoundError:
                pass

        out_git = ""

        for c in self.git_init_cmds:
            try:
                out_git += (await run_command(c)) + "\n"
            except Exception as e:
                out_git += f"{e}\n"

        self.bot.pool.commit = (await run_command("git rev-parse HEAD")).strip("\n")
        self.bot.pool.remote_git_url = self.bot.config["SOURCE_REPO"][:-4]

        return out_git

   

    @commands.is_owner()
    @commands.command(hidden=True, aliases=["menu"])
    async def panel(self, ctx: CustomContext):

        embed = disnake.Embed(
            title="CONTROL PANEL.",
            color=self.bot.get_color(ctx.guild.me)
        )
        embed.set_footer(text="Click on a task you want to execute.")
        await ctx.send(embed=embed, view=PanelView(self.bot))

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(description="Sync/Register slash commands in the server.", hidden=True)
    async def syncguild(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        embed = disnake.Embed(
            color=self.bot.get_color(ctx.guild.me),
            description="**This command is no longer necessary to use (Command synchronization is now "
                        f"automatic).**\n\n{sync_message(self.bot)}"
        )

        await ctx.send(embed=embed)

    @commands.is_owner()
    @panel_command(aliases=["sync"], description="Manually sync slash commands.",
                emoji="<:slash:944875586839527444>",
                alt_name="Manually sync commands.")
    async def synccmds(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        if self.bot.config["AUTO_SYNC_COMMANDS"] is True:
            raise GenericError(
                f"**This cannot be used with automatic synchronization enabled...**\n\n{sync_message(self.bot)}")

        await self.bot._sync_application_commands()

        txt = f"**Slash commands have been successfully synchronized!**\n\n{sync_message(self.bot)}"

        if isinstance(ctx, CustomContext):

            embed = disnake.Embed(
                color=self.bot.get_color(ctx.guild.me),
                description=txt
            )

            await ctx.send(embed=embed, view=self.owner_view)

        else:
            return txt

    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(
        aliases=["prefix", "changeprefix"],
        description="Change the server's prefix",
        usage="{prefix}{cmd} [prefix]\nEx: {prefix}{cmd} >>"
    )
    async def setprefix(self, ctx: CustomContext, prefix: str):

        prefix = prefix.strip()

        if not prefix or len(prefix) > 5:
            raise GenericError("**The prefix cannot contain spaces or exceed 5 characters.**")

        try:
            guild_data = ctx.global_guild_data
        except AttributeError:
            guild_data = await self.bot.get_global_data(ctx.guild.id, db_name=DBModel.guilds)
            ctx.global_guild_data = guild_data

        self.bot.pool.guild_prefix_cache[ctx.guild.id] = prefix
        guild_data["prefix"] = prefix
        await self.bot.update_global_data(ctx.guild.id, guild_data, db_name=DBModel.guilds)

        prefix = disnake.utils.escape_markdown(prefix)

        embed = disnake.Embed(
            description=f"**My prefix in the server is now:** `{prefix}`\n"
                        f"**If you want to restore the default prefix, use the command:** `{prefix}{self.resetprefix.name}`",
            color=self.bot.get_color(ctx.guild.me)
        )

        await ctx.send(embed=embed)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(
        description="Reset the server's prefix (Use the bot's default prefix)"
    )
    async def resetprefix(self, ctx: CustomContext):

        try:
            guild_data = ctx.global_guild_data
        except AttributeError:
            guild_data = await self.bot.get_global_data(ctx.guild.id, db_name=DBModel.guilds)
            ctx.global_guild_data = guild_data

        if not guild_data["prefix"]:
            raise GenericError("**No prefix configured in the server.**")

        guild_data["prefix"] = ""
        self.bot.pool.guild_prefix_cache[ctx.guild.id] = ""

        await self.bot.update_global_data(ctx.guild.id, guild_data, db_name=DBModel.guilds)

        embed = disnake.Embed(
            description=f"**The server's prefix has been successfully reset.\n"
                        f"The default prefix now is:** `{disnake.utils.escape_markdown(self.bot.default_prefix)}`",
            color=self.bot.get_color(ctx.guild.me)
        )

        await ctx.send(embed=embed)

    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(
        aliases=["uprefix", "spu", "setmyprefix", "spm", "setcustomprefix", "scp", "customprefix", "myprefix"],
        description="Change your user prefix (the prefix I will respond to you with regardless "
                    "of the prefix configured in the server).",
        usage="{prefix}{cmd} [prefix]\nEx: {prefix}{cmd} >>"
    )
    async def setuserprefix(self, ctx: CustomContext, prefix: str):

        prefix = prefix.strip()

        if not prefix or len(prefix) > 5:
            raise GenericError("**The prefix cannot contain spaces or exceed 5 characters.**")

        try:
            user_data = ctx.global_user_data
        except AttributeError:
            user_data = await self.bot.get_global_data(ctx.author.id, db_name=DBModel.users)
            ctx.global_user_data = user_data

        user_data["custom_prefix"] = prefix
        self.bot.pool.user_prefix_cache[ctx.author.id] = prefix
        await self.bot.update_global_data(ctx.author.id, user_data, db_name=DBModel.users)

        prefix = disnake.utils.escape_markdown(prefix)

        embed = disnake.Embed(
            description=f"**Your user prefix is now:** `{prefix}`\n"
                        f"**To remove your user prefix, use the command:** `{prefix}{self.resetuserprefix.name}`",
            color=self.bot.get_color(ctx.guild.me)
        )

        await ctx.send(embed=embed)

    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(description="Remove your user prefix")
    async def resetuserprefix(self, ctx: CustomContext):

        try:
            user_data = ctx.global_user_data
        except AttributeError:
            user_data = await self.bot.get_global_data(ctx.author.id, db_name=DBModel.users)
            ctx.global_user_data = user_data

        if not user_data["custom_prefix"]:
            raise GenericError("**You do not have a configured prefix.**")

        user_data["custom_prefix"] = ""
        self.bot.pool.user_prefix_cache[ctx.author.id] = ""
        await self.bot.update_global_data(ctx.author.id, user_data, db_name=DBModel.users)

        embed = disnake.Embed(
            description=f"**Your user prefix has been successfully removed.**",
            color=self.bot.get_color(ctx.guild.me)
        )

        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command(
        aliases=["guildprefix", "sgp", "gp"], hidden=True,
        description="Set a prefix manually for a server with the given ID (useful for botlists)",
        usage="{prefix}{cmd} [server id] <prefix>\nEx: {prefix}{cmd} 1155223334455667788 >>\nNote: Use the command without specifying a prefix to remove it."
    )
    async def setguildprefix(self, ctx: CustomContext, server_id: int, prefix: str = None):

        if not 17 < len(str(server_id)) < 24:
            raise GenericError("**The number of characters in the server ID must be between 18 to 23.**")

        guild_data = await self.bot.get_global_data(server_id, db_name=DBModel.guilds)

        embed = disnake.Embed(color=self.bot.get_color(ctx.guild.me))

        prefix = prefix.strip()

        if not prefix:
            guild_data["prefix"] = ""
            await ctx.bot.update_global_data(server_id, guild_data, db_name=DBModel.guilds)
            embed.description = "**The prefix for the specified server ID has been successfully reset.**"

        else:
            guild_data["prefix"] = prefix
            await self.bot.update_global_data(server_id, guild_data, db_name=DBModel.guilds)
            embed.description = f"**The prefix for the server with the specified ID is now:** {disnake.utils.escape_markdown(prefix)}"

        self.bot.pool.guild_prefix_cache[ctx.guild.id] = prefix

        await ctx.send(embed=embed)



    @commands.is_owner()
    @commands.command(hidden=True)
    async def cleardm(self, ctx: CustomContext, amount: int = 20):

        counter = 0

        async with ctx.typing():

            async for msg in ctx.author.history(limit=int(amount)):
                if msg.author.id == self.bot.user.id:
                    await msg.delete()
                    await asyncio.sleep(0.5)
                    counter += 1

        if not counter:
            raise GenericError(f"**No message was deleted from {amount} checked message(s)...**")

        if counter == 1:
            txt = "**One message was deleted from your DM.**"
        else:
            txt = f"**{counter} messages were deleted from your DM.**"

        await ctx.send(embed=disnake.Embed(description=txt, colour=self.bot.get_color(ctx.guild.me)))

    @commands.Cog.listener("on_button_click")
    async def close_shell_result(self, inter: disnake.MessageInteraction):

        if inter.data.custom_id != "close_shell_result":
            return

        if not await self.bot.is_owner(inter.author):
            return await inter.send("**Only my owner can use this button!**", ephemeral=True)

        await inter.response.edit_message(
            content="```ini\n🔒 - [Shell Closed!] - 🔒```",
            attachments=None,
            view=None,
            embed=None
        )

    @commands.is_owner()
    @commands.command(aliases=["sh"], hidden=True)
    async def shell(self, ctx: CustomContext, *, command: str):

        if command.startswith('```') and command.endswith('```'):
            if command[4] != "\n":
                command = f"```\n{command[3:]}"
            if command[:-4] != "\n":
                command = command[:-3] + "\n```"
            command = '\n'.join(command.split('\n')[1:-1])
        else:
            command = command.strip('` \n')

        try:
            async with ctx.typing():
                result = await run_command(command)
        except GenericError as e:
            kwargs = {}
            if len(e.text) > 2000:
                kwargs["file"] = string_to_file(e.text, filename="error.txt")
            else:
                kwargs["content"] = f"```py\n{e.text}```"

            try:
                await ctx.author.send(**kwargs)
                await ctx.message.add_reaction("⚠️")
            except disnake.Forbidden:
                traceback.print_exc()
                raise GenericError(
                    "**An error occurred (check logs/terminal or enable your DMs for the next "
                    "result to be sent directly to your DMs).**"
                )

        else:

            kwargs = {}
            if len(result) > 2000:
                kwargs["file"] = string_to_file(result, filename=f"shell_result_{ctx.message.id}.txt")
            else:
                kwargs["content"] = f"```py\n{result}```"

            await ctx.reply(
                components=[
                    disnake.ui.Button(label="Close Shell", custom_id="close_shell_result", emoji="♻️")
                ],
                mention_author=False, fail_if_not_exists=False,
                **kwargs
            )

    @check_voice()
    @commands.cooldown(1, 15, commands.BucketType.guild)
    @commands.command(description='Initialize a player on the server.', aliases=["spawn", "sp", "spw", "smn"])
    async def summon(self, ctx: CustomContext):

        try:
            ctx.bot.music.players[ctx.guild.id]  # type ignore
            raise GenericError("**A player is already initiated on the server.**")
        except KeyError:
            pass

        bot = ctx.bot
        guild = ctx.guild
        channel = ctx.channel
        msg = None

        if bot.user.id not in ctx.author.voice.channel.voice_states:

            free_bots = []

            for b in self.bot.pool.bots:

                if not b.bot_ready:
                    continue

                g = b.get_guild(ctx.guild_id)

                if not g:
                    continue

                p = b.music.players.get(ctx.guild_id)

                if p and ctx.author.id not in p.last_channel.voice_states:
                    continue

                free_bots.append(b)

            if len(free_bots) > 1:

                v = SelectBotVoice(ctx, guild, free_bots)

                msg = await ctx.send(
                    embed=disnake.Embed(
                        description=f"**Choose which bot you want to use in the channel {ctx.author.voice.channel.mention}**",
                        color=self.bot.get_color(guild.me)), view=v
                )

                ctx.store_message = msg

                await v.wait()

                if v.status is None:
                    await msg.edit(embed=disnake.Embed(description="### Time is up...", color=self.bot.get_color(guild.me)), view=None)
                    return

                if v.status is False:
                    await msg.edit(embed=disnake.Embed(description="### Operation canceled.",
                                                   color=self.bot.get_color(guild.me)), view=None)
                    return

                if not v.inter.author.voice:
                    await msg.edit(embed=disnake.Embed(description="### You are not connected to a voice channel...",
                                                   color=self.bot.get_color(guild.me)), view=None)
                    return

                if not v.inter.author.voice:
                    raise NoVoice()

                bot = v.bot
                ctx = v.inter
                guild = v.guild
                channel = bot.get_channel(ctx.channel.id)

        can_connect(channel=ctx.author.voice.channel, guild=guild)

        node: wavelink.Node = bot.music.get_best_node()

        if not node:
            raise GenericError("**No music servers available!**")

        player: LavalinkPlayer = await bot.get_cog("Music").create_player(
            inter=ctx, bot=bot, guild=guild, channel=channel
        )

        await player.connect(ctx.author.voice.channel.id)

        if msg:
            await msg.edit(
                f"Music session started on the channel {ctx.author.voice.channel.mention}\nBy: {bot.user.mention}{player.controller_link}",
                components=None, embed=None
            )
        else:
            self.bot.loop.create_task(ctx.message.add_reaction("👍"))

        while not ctx.guild.me.voice:
            await asyncio.sleep(1)

        if isinstance(ctx.author.voice.channel, disnake.StageChannel):

            stage_perms = ctx.author.voice.channel.permissions_for(guild.me)
            if stage_perms.manage_permissions:
                await guild.me.edit(suppress=False)

            await asyncio.sleep(1.5)

        await player.process_next()

    @commands.is_owner()
    @commands.command(hidden=True, aliases=["setbotavatar"], description="To change the bot's avatar, please provide an attachment or a direct link to a jpg or gif image.")
    async def setavatar(self, ctx: CustomContext, url: str = ""):

        use_hyperlink = False

        if re.match(r'^<.*>$', url):
            use_hyperlink = True
            url = url.strip("<>")

        if not url:

            if not ctx.message.attachments:
                raise GenericError("You should provide the link to an image or gif (or attach one) in the command.")

            url = ctx.message.attachments[0].url

            if not url.split("?ex=")[0].endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
                raise GenericError("You must attach a valid file: png, jpg, jpeg, webp, gif, bmp.")

        elif not URL_REG.match(url):
            raise GenericError("You provided an invalid link.")

        inter, bot = await select_bot_pool(ctx, return_new=True)

        if not bot:
            return

        await inter.response.defer(ephemeral=True)

        async with ctx.bot.session.get(url) as r:
            image_bytes = await r.read()

        await bot.user.edit(avatar=image_bytes)

        await bot.http.request(Route('PATCH', '/applications/@me'), json={
            "icon": disnake.utils._bytes_to_base64_data(image_bytes)
        })

        try:
            func = inter.edit_original_message
        except AttributeError:
            try:
                func = inter.response.edit_message
            except AttributeError:
                func = inter.send

        avatar_txt = "avatar" if not use_hyperlink else f"[avatar]({bot.user.display_avatar.with_static_format('png').url})"

        await func(f"The {avatar_txt} of the bot {bot.user.mention} has been successfully changed.", view=None, embed=None)

    async def cog_check(self, ctx: CustomContext) -> bool:
        return await check_requester_channel(ctx)

    async def cog_load(self) -> None:
        self.owner_view = PanelView(self.bot)

    async def download_lavalink_serverlist(self):
        async with ClientSession() as session:
            async with session.get(self.bot.config["LAVALINK_SERVER_LIST"]) as r:
                ini_file = await r.read()
                with open("lavalink.ini", "wb") as f:
                    f.write(ini_file)

def setup(bot: BotCore):
    bot.add_cog(Owner(bot))
